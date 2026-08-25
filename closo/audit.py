"""SQLite audit log (ARCHITECTURE 9.1).

Four tables. ``events`` is the interesting one: it is **append-only, enforced
by triggers rather than by convention**. An audit log you can quietly edit is
not an audit log, and the replay demo reads straight out of this table - if a
row could be rewritten after the fact, a replay would no longer be evidence of
what actually happened.

The triggers matter more than the discipline. A rule everyone agrees to follow
is a rule that gets broken during a demo-day scramble; a rule the database
refuses to break is not.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from closo.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    seed          INTEGER NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    records_total INTEGER NOT NULL DEFAULT 0,
    config_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    ts           TEXT NOT NULL,
    layer        TEXT NOT NULL,
    record_ref   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, event_id);

CREATE TABLE IF NOT EXISTS resolutions (
    run_id               TEXT NOT NULL REFERENCES runs(run_id),
    bank_txn_id          TEXT NOT NULL,
    final_status         TEXT NOT NULL,
    pass_or_verdict_json TEXT NOT NULL DEFAULT '{}',
    verifier_result_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, bank_txn_id)
);

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key     TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);
"""

# Append-only, enforced by the database. Deliberately not a soft warning:
# a rejected UPDATE is a bug someone fixes, a permitted one is a demo that
# silently stops being evidence.
APPEND_ONLY_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: DELETE is not permitted');
END;
"""


def _json_default(value: Any) -> str:
    """Serialize Decimal as a string; never as a float (11.1)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def dumps(payload: Any) -> str:
    """Compact, key-sorted JSON. Sorted so stored rows diff cleanly."""
    return json.dumps(payload, default=_json_default, sort_keys=True)


class AuditLog:
    """Append-only run log backed by SQLite.

    Pass ``":memory:"`` for tests. The connection is held open for the life
    of the object because an in-memory database vanishes when it closes.
    """

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = str(path)
        # check_same_thread=False because Streamlit re-executes the script on a
        # different thread for every interaction, so a connection opened during
        # one rerun is used from another. Without this the first click of "Run
        # reconciliation" raises ProgrammingError - which a boot check that only
        # asserts HTTP 200 will not catch, because the page loads fine and the
        # failure waits for the button.
        #
        # Dropping that guard makes the caller responsible for serializing
        # access, hence the lock below and the _write/_read helpers. Every
        # statement in this class goes through one of them.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.executescript(APPEND_ONLY_TRIGGERS)
            self.conn.commit()

    def _write(self, sql: str, params: tuple = (), commit: bool = True) -> None:
        with self._lock:
            self.conn.execute(sql, params)
            if commit:
                self.conn.commit()

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def start_run(
        self, run_id: str, seed: int, records_total: int, config: dict | None = None
    ) -> None:
        """Open a run. Idempotent so a replay can reuse an existing id."""
        self._write(
            "INSERT OR REPLACE INTO runs "
            "(run_id, seed, started_at, records_total, config_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, seed, _now(), records_total, dumps(config or {})),
        )

    def finish_run(self, run_id: str, summary: dict | None = None) -> None:
        """Close a run, optionally merging a summary into its config.

        The summary rides in ``config_json`` rather than in a column of its
        own so the schema does not have to change under a database that
        already exists - and a replay needs it: a replayed run that could
        not say what the original cost would report a free reconciliation
        that was never free (9.2).
        """
        if summary:
            merged = {**self.run_config(run_id), **summary}
            self._write(
                "UPDATE runs SET finished_at = ?, config_json = ? WHERE run_id = ?",
                (_now(), dumps(merged), run_id),
            )
            return
        self._write(
            "UPDATE runs SET finished_at = ? WHERE run_id = ?", (_now(), run_id)
        )

    def run_config(self, run_id: str) -> dict:
        """The run's stored config, or an empty dict for an unknown run."""
        meta = self.get_run(run_id)
        return json.loads(meta["config_json"]) if meta else {}

    def record_event(
        self,
        run_id: str,
        layer: str,
        record_ref: str,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        """Append one event. There is no corresponding update or delete."""
        self._write(
            "INSERT INTO events (run_id, ts, layer, record_ref, event_type, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, _now(), layer, record_ref, event_type, dumps(payload or {})),
            commit=False,
        )

    def record_events(self, run_id: str, decisions: list) -> None:
        """Append a batch of :class:`closo.layer1_matcher.Decision` objects."""
        for decision in decisions:
            self.record_event(
                run_id, decision.layer, decision.record_ref,
                decision.event_type, decision.payload,
            )
        self.commit()

    def record_resolution(
        self,
        run_id: str,
        bank_txn_id: str,
        final_status: str,
        detail: dict | None = None,
        verifier_result: dict | None = None,
    ) -> None:
        self._write(
            "INSERT OR REPLACE INTO resolutions "
            "(run_id, bank_txn_id, final_status, pass_or_verdict_json, "
            "verifier_result_json) VALUES (?, ?, ?, ?, ?)",
            (
                run_id, bank_txn_id, final_status,
                dumps(detail or {}), dumps(verifier_result or {}),
            ),
            commit=False,
        )

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    # -- reads -------------------------------------------------------------

    def latest_run_id(self) -> str | None:
        """The most recently started run. Backs the Replay button (10.1)."""
        rows = self._read(
            "SELECT run_id FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
        )
        return rows[0]["run_id"] if rows else None

    def get_run(self, run_id: str) -> dict | None:
        rows = self._read("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return dict(rows[0]) if rows else None

    def read_events(self, run_id: str) -> list[dict]:
        """Every event for a run, in the order it happened.

        Ordered by event_id rather than timestamp: several events can share a
        millisecond, and a replay that reorders them tells a different story
        from the one that occurred.
        """
        rows = self._read(
            "SELECT * FROM events WHERE run_id = ? ORDER BY event_id", (run_id,)
        )
        events = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json"))
            events.append(event)
        return events

    def read_resolutions(self, run_id: str) -> list[dict]:
        rows = self._read(
            "SELECT * FROM resolutions WHERE run_id = ? ORDER BY bank_txn_id",
            (run_id,),
        )
        out = []
        for row in rows:
            record = dict(row)
            record["detail"] = json.loads(record.pop("pass_or_verdict_json"))
            record["verifier_result"] = json.loads(record.pop("verifier_result_json"))
            out.append(record)
        return out

    # -- API response cache ------------------------------------------------

    def cache_get(self, cache_key: str) -> dict | None:
        rows = self._read(
            "SELECT response_json FROM api_cache WHERE cache_key = ?", (cache_key,)
        )
        return json.loads(rows[0]["response_json"]) if rows else None

    def cache_put(self, cache_key: str, response: dict) -> None:
        self._write(
            "INSERT OR REPLACE INTO api_cache (cache_key, response_json, fetched_at)"
            " VALUES (?, ?, ?)",
            (cache_key, dumps(response), _now()),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def audit_log(path: Path | str = DB_PATH) -> Iterator[AuditLog]:
    """Open an audit log for the duration of a block."""
    log = AuditLog(path)
    try:
        yield log
    finally:
        log.close()
