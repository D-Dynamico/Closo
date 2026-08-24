"""Run orchestration: data in, terminal states out.

Currently Layer 1 only. Everything the cascade could not match becomes
``ESCALATED`` with a note saying the investigator has not been built yet -
**not** silently dropped, and not optimistically counted as resolved. A
pipeline that hides its unfinished half produces a scorecard that flatters
itself, which is the exact failure this project is arguing against.

**This module must never read ground truth.** It has no import path to it:
``dataset_io.load_batch()`` offers no parameter for it, and only
``metrics.py`` may call ``load_ground_truth()``, after the run completes
(11.4). A test monkeypatches file reads during a run to prove it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from closo.audit import AuditLog
from closo.config import DEMO_DIR, DEMO_SEED
from closo.dataset_io import load_batch
from closo.layer1_matcher import Layer1Result, run_layer1
from closo.schemas import FinalStatus, MatchRecord
from closo.taxonomy import GeneratedBatch

#: Note attached to exceptions parked pending Layer 2. Kept explicit so the
#: Scorecard can show them as pending rather than as a verdict about them.
PENDING_NOTE = "awaiting Layer 2 investigator (not yet built)"


@dataclass
class RunOutcome:
    """One pipeline run: the terminal state of every bank transaction."""

    run_id: str
    seed: int
    started_at: datetime
    finished_at: datetime | None = None
    layer1: Layer1Result | None = None
    statuses: dict[str, FinalStatus] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    def count(self, status: FinalStatus) -> int:
        return sum(1 for value in self.statuses.values() if value is status)

    @property
    def total(self) -> int:
        return len(self.statuses)


def run(
    batch: GeneratedBatch,
    seed: int = DEMO_SEED,
    audit: AuditLog | None = None,
    run_id: str | None = None,
) -> RunOutcome:
    """Reconcile ``batch`` and return every transaction's terminal state.

    Args:
        batch: loaded source records. Ground truth is not present and cannot be.
        seed: recorded on the run for reproducibility.
        audit: optional log. Events are written whether or not one is given -
            the matcher records them regardless; this only persists them.
        run_id: supply to make a run reproducible by id; otherwise generated.

    Returns:
        The outcome, with every bank transaction in exactly one terminal state.
    """
    identifier = run_id or f"run_{uuid.uuid4().hex[:12]}"
    started = datetime.now(timezone.utc)
    clock = time.perf_counter()

    if audit is not None:
        audit.start_run(
            identifier, seed, len(batch.bank_txns),
            {"layers_enabled": ["layer1"], "pending": ["layer2", "layer3"]},
        )

    result = run_layer1(batch)

    outcome = RunOutcome(
        run_id=identifier, seed=seed, started_at=started, layer1=result
    )

    for match in result.matches:
        outcome.statuses[match.bank_txn_id] = FinalStatus.AUTO_MATCHED

    for item in result.exceptions:
        if item.bank_txn_id is None:
            continue  # settlement-side (E9); no bank transaction to key on
        outcome.statuses[item.bank_txn_id] = FinalStatus.ESCALATED
        outcome.notes[item.bank_txn_id] = f"{item.reason}: {PENDING_NOTE}"

    # Every transaction must land somewhere. A credit the cascade neither
    # matched nor excepted would otherwise vanish from the scorecard, making
    # the totals look tidy while quietly under-reporting the work left.
    for txn in batch.bank_txns:
        outcome.statuses.setdefault(txn.bank_txn_id, FinalStatus.ESCALATED)
        outcome.notes.setdefault(txn.bank_txn_id, f"unhandled by Layer 1: {PENDING_NOTE}")

    outcome.finished_at = datetime.now(timezone.utc)
    outcome.elapsed_seconds = time.perf_counter() - clock

    if audit is not None:
        audit.record_events(identifier, result.events)
        for txn_id, status in sorted(outcome.statuses.items()):
            audit.record_resolution(
                identifier, txn_id, status.value,
                _detail_for(txn_id, result, outcome),
            )
        audit.commit()
        audit.finish_run(identifier)

    return outcome


def _detail_for(txn_id: str, result: Layer1Result, outcome: RunOutcome) -> dict:
    """What is known about one transaction's disposition."""
    for match in result.matches:
        if match.bank_txn_id == txn_id:
            return {
                "pass_used": match.pass_used,
                "settlement_id": match.settlement_id,
                "payment_ids": match.payment_ids,
                "tolerance_applied": str(match.tolerance_applied),
            }
    for item in result.exceptions:
        if item.bank_txn_id == txn_id:
            return {
                "exception_id": item.exception_id,
                "reason": item.reason,
                "detail": item.detail,
                "note": PENDING_NOTE,
            }
    return {"note": outcome.notes.get(txn_id, PENDING_NOTE)}


def run_demo(
    audit: AuditLog | None = None, data_dir: Path = DEMO_DIR, run_id: str | None = None
) -> RunOutcome:
    """Load the frozen demo set and reconcile it. No network, no API key."""
    return run(load_batch(data_dir), seed=DEMO_SEED, audit=audit, run_id=run_id)


def replay(run_id: str, audit: AuditLog) -> RunOutcome:
    """Rebuild a past run's outcome from the audit log alone.

    Reads only ``runs`` and ``resolutions`` - it does not re-execute the
    cascade. That is the point (10.1): if the network or the LLM API dies
    on stage, replaying the last good run has to be indistinguishable from
    having run it, and re-running the pipeline would not be a replay, it
    would be a second run that might disagree.

    Raises:
        KeyError: if the run is not in the log. Better a clear failure than
            an empty scorecard that looks like a run resolving nothing.
    """
    meta = audit.get_run(run_id)
    if meta is None:
        raise KeyError(f"no run {run_id!r} in the audit log")

    outcome = RunOutcome(
        run_id=run_id,
        seed=meta["seed"],
        started_at=datetime.fromisoformat(meta["started_at"]),
    )
    if meta["finished_at"]:
        outcome.finished_at = datetime.fromisoformat(meta["finished_at"])
        outcome.elapsed_seconds = (
            outcome.finished_at - outcome.started_at
        ).total_seconds()

    replayed_matches = []
    for record in audit.read_resolutions(run_id):
        status = FinalStatus(record["final_status"])
        txn_id = record["bank_txn_id"]
        detail = record["detail"]
        outcome.statuses[txn_id] = status
        if status is FinalStatus.AUTO_MATCHED and detail.get("settlement_id"):
            replayed_matches.append(
                MatchRecord(
                    bank_txn_id=txn_id,
                    settlement_id=detail["settlement_id"],
                    payment_ids=detail.get("payment_ids", []),
                    pass_used=detail["pass_used"],
                    tolerance_applied=detail.get("tolerance_applied", "0.00"),
                )
            )
        elif "note" in detail:
            outcome.notes[txn_id] = f"{detail.get('reason', 'escalated')}: {detail['note']}"

    # A replayed outcome carries the matches so metrics.score can check the
    # cited payment ids against ground truth exactly as it would live.
    outcome.layer1 = Layer1Result(
        matches=replayed_matches, total_bank_txns=len(outcome.statuses)
    )
    return outcome
