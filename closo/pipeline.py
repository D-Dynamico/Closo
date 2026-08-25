"""Run orchestration: data in, terminal states out.

Layer 1 runs always. Layers 2 and 3 run when an investigator is supplied -
and only ever as a pair. A verdict that no verifier checked is not a
resolution here, it is prose, so there is no code path that records one
without a :class:`closo.layer3_verifier.Verifier` having passed it first.

**The investigator is injected, never constructed.** This module must not
import ``closo.llm_client``, and a test asserts it (11.3). The caller -
``scripts/real_api_run.py`` live, ``app/streamlit_app.py`` from the cached
demo responses, the tests with a mock - decides what a model even is. What
arrives here is an object with ``investigate(item)``.

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
from typing import Any, Protocol

from closo.audit import AuditLog
from closo.config import DEMO_DIR, DEMO_SEED
from closo.dataset_io import load_batch
from closo.errors import QuotaExhausted
from closo.layer1_matcher import Layer1Result, run_layer1
from closo.layer3_verifier import Verifier
from closo.schemas import ExceptionItem, FinalStatus, MatchRecord, Verdict, VerifierResult
from closo.taxonomy import GeneratedBatch

#: Note attached to exceptions parked because no investigator was supplied.
#: Kept explicit so the Scorecard can show them as pending rather than as a
#: verdict about them.
PENDING_NOTE = "awaiting Layer 2 investigator (not run)"

#: Note for exceptions the run never reached because the day's requests ran
#: out. Says "quota", not "budget": this string is what a human reads in the
#: escalation queue while working out why half a batch has no verdict.
QUOTA_NOTE = "unresolvable - daily request quota exhausted"


class Investigation(Protocol):
    """The only thing the pipeline assumes about Layer 2.

    One method, mirroring :class:`closo.layer2_investigator.Investigator`.
    Narrow on purpose: anything wider would drag provider concepts into
    the orchestrator and give the import invariant something to catch.
    """

    def investigate(self, item: ExceptionItem) -> Any: ...


@dataclass
class RunCost:
    """What the run spent. Surfaces as cost-per-record on the Scorecard (9.2).

    Requests rather than tokens are the scarce resource on this quota
    (7.4), so both are counted and requests are the one to read.
    """

    tokens_used: int = 0
    requests_made: int = 0
    cache_hits: int = 0
    exceptions_investigated: int = 0
    exceptions_skipped: int = 0
    layer2_seconds: float = 0.0
    quota_exhausted: bool = False

    def as_dict(self) -> dict:
        return {
            "tokens_used": self.tokens_used,
            "requests_made": self.requests_made,
            "cache_hits": self.cache_hits,
            "exceptions_investigated": self.exceptions_investigated,
            "exceptions_skipped": self.exceptions_skipped,
            "quota_exhausted": self.quota_exhausted,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> RunCost:
        """Rebuild from the audit log, so a replay reports what it cost."""
        return cls(**{k: v for k, v in payload.items() if k in cls.__annotations__})


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

    #: Payments a verified verdict attributed to each credit. Metrics needs
    #: these to grade an agent resolution against ground truth exactly as it
    #: grades a Layer 1 match - otherwise Layer 2's work would be counted as
    #: resolved without ever being checked for correctness.
    agent_matches: dict[str, list[str]] = field(default_factory=dict)

    #: Credits resolved on verified math but unverified intent (8.1). Still
    #: AGENT_RESOLVED_VERIFIED - sign-off is a flag, not a fourth state -
    #: but reported separately so nobody reads "resolved" as "settled".
    needs_signoff: set[str] = field(default_factory=set)

    verdicts: dict[str, Verdict] = field(default_factory=dict)
    verifications: dict[str, VerifierResult] = field(default_factory=dict)
    cost: RunCost = field(default_factory=RunCost)

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
    investigator: Investigation | None = None,
) -> RunOutcome:
    """Reconcile ``batch`` and return every transaction's terminal state.

    Args:
        batch: loaded source records. Ground truth is not present and cannot be.
        seed: recorded on the run for reproducibility.
        audit: optional log. Events are written whether or not one is given -
            the matcher records them regardless; this only persists them.
        run_id: supply to make a run reproducible by id; otherwise generated.
        investigator: Layer 2. Omit and the residue is escalated as pending,
            which is honest; it is never counted as resolved.

    Returns:
        The outcome, with every bank transaction in exactly one terminal state.
    """
    identifier = run_id or f"run_{uuid.uuid4().hex[:12]}"
    started = datetime.now(timezone.utc)
    clock = time.perf_counter()
    layers = ["layer1"] + (["layer2", "layer3"] if investigator else [])

    if audit is not None:
        audit.start_run(
            identifier, seed, len(batch.bank_txns),
            {"layers_enabled": layers,
             "pending": [] if investigator else ["layer2", "layer3"]},
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
    #
    # The note is written only for credits that actually fell through. An
    # earlier version noted every credit, so all 39 auto-matched rows
    # carried the words "unhandled by Layer 1" - false about each of them,
    # and waiting for the first screen that read a note without checking
    # the status beside it.
    for txn in batch.bank_txns:
        if txn.bank_txn_id not in outcome.statuses:
            outcome.statuses[txn.bank_txn_id] = FinalStatus.ESCALATED
            outcome.notes[txn.bank_txn_id] = f"unhandled by Layer 1: {PENDING_NOTE}"

    events: list[dict] = []
    if investigator is not None:
        events = _investigate(batch, result, outcome, investigator)

    outcome.finished_at = datetime.now(timezone.utc)
    outcome.elapsed_seconds = time.perf_counter() - clock

    if audit is not None:
        audit.record_events(identifier, result.events)
        for event in events:
            audit.record_event(
                identifier, event["layer"], event["record_ref"],
                event["event_type"], event.get("payload"),
            )
        for txn_id, status in sorted(outcome.statuses.items()):
            audit.record_resolution(
                identifier, txn_id, status.value,
                _detail_for(txn_id, result, outcome),
                _verification_for(txn_id, outcome),
            )
        audit.commit()
        audit.finish_run(identifier, {"cost": outcome.cost.as_dict()})

    return outcome


# --------------------------------------------------------------------------
# Layers 2 and 3
# --------------------------------------------------------------------------


def _investigate(
    batch: GeneratedBatch,
    result: Layer1Result,
    outcome: RunOutcome,
    investigator: Investigation,
) -> list[dict]:
    """Investigate every exception and verify every verdict.

    The two are interleaved rather than run as separate phases, for two
    reasons. Exclusivity has to be checked against resolutions that already
    passed, and a verdict covering a second credit (a split payout) has to
    take that credit out of the queue before it is investigated again -
    which cost real requests during the Stage 6 live run, and would have
    failed ``exclusivity_violation`` on the second attempt anyway.

    The verifier starts with every payment Layer 1 already consumed marked
    as spent. Without that, a verdict could cite payments belonging to an
    auto-matched settlement and the money view would count them twice.
    """
    started = time.perf_counter()
    events: list[dict] = []
    verifier = Verifier(batch)
    verifier.consumed.update(
        pid for match in result.matches for pid in match.payment_ids
    )
    exhausted = False

    for item in result.exceptions:
        ref = item.bank_txn_id or item.settlement_id or "?"

        if exhausted:
            _escalate(outcome, item, QUOTA_NOTE)
            events.append(_event(ref, "quota_exhausted", exception_id=item.exception_id))
            continue

        if item.bank_txn_id and _already_resolved(outcome, item.bank_txn_id):
            # Covered by an earlier verdict that cited it as a second leg.
            outcome.cost.exceptions_skipped += 1
            events.append(_event(
                ref, "skipped_already_resolved", exception_id=item.exception_id,
            ))
            continue

        try:
            investigation = investigator.investigate(item)
        except QuotaExhausted as wall:
            exhausted = True
            outcome.cost.quota_exhausted = True
            _escalate(outcome, item, f"{QUOTA_NOTE} ({wall})")
            events.append(_event(ref, "quota_exhausted", detail=str(wall)))
            continue

        outcome.cost.exceptions_investigated += 1
        outcome.cost.tokens_used += getattr(investigation, "tokens_used", 0)
        events.extend(getattr(investigation, "events", []) or [])

        verdict = investigation.verdict
        outcome.verdicts[item.exception_id] = verdict

        verification = verifier.verify(verdict)
        outcome.verifications[item.exception_id] = verification

        # The full structures go into the log, not just a summary of them.
        # 9.1 makes `events` what replay mode reads, and it is the only
        # store that can hold these at all for a settlement-side exception:
        # E9 has no bank credit, so it has no `resolutions` row to hang a
        # verdict on, and its investigation would vanish on replay.
        events.append(_event(
            ref, "verdict_recorded", exception_id=item.exception_id,
            verdict=verdict.model_dump(mode="json"),
        ))
        events.append(_event(
            ref, "verified", layer="layer3", exception_id=item.exception_id,
            passed=verification.passed,
            rejection_reason=verification.rejection_reason,
            effective_confidence=verification.effective_confidence,
            schedule_anomaly=verification.schedule_anomaly,
        ))
        events.append(_event(
            ref, "verification_recorded", layer="layer3",
            exception_id=item.exception_id,
            result=verification.model_dump(mode="json"),
        ))

        if not verification.passed:
            _escalate(outcome, item, _rejection_note(verdict, verification))
            continue

        credits = _credits_claimed(verdict)
        problem = _credit_claim_problem(outcome, item, credits)
        if problem:
            # The verifier proves the arithmetic; it does not know which
            # credit the queue was asking about, nor what Layer 1 already
            # matched. Both are the orchestrator's to check, and both are
            # ways a sound-looking verdict double-counts money.
            events.append(_event(
                ref, "verdict_rejected_by_pipeline", layer="layer3",
                exception_id=item.exception_id, detail=problem,
            ))
            _escalate(outcome, item, f"verdict rejected: {problem}")
            continue

        verifier.commit(verification)
        for credit in credits:
            outcome.statuses[credit] = FinalStatus.AGENT_RESOLVED_VERIFIED
            outcome.notes.pop(credit, None)
            outcome.agent_matches[credit] = list(verdict.proposed_match.payment_ids)
            if verification.needs_human_signoff:
                outcome.needs_signoff.add(credit)

    outcome.cost.layer2_seconds = time.perf_counter() - started
    outcome.cost.requests_made = _requests_spent(investigator)
    outcome.cost.cache_hits = _attribute(investigator, "cache_hits")
    return events


def _already_resolved(outcome: RunOutcome, txn_id: str) -> bool:
    return outcome.statuses.get(txn_id) in (
        FinalStatus.AUTO_MATCHED, FinalStatus.AGENT_RESOLVED_VERIFIED
    )


def _credits_claimed(verdict: Verdict) -> list[str]:
    match = verdict.proposed_match
    assert match is not None  # guaranteed: a passing verdict proposed one
    return [match.bank_txn_id, *match.extra_bank_txn_ids]


def _credit_claim_problem(
    outcome: RunOutcome, item: ExceptionItem, credits: list[str]
) -> str | None:
    """Why a verified verdict still must not resolve these credits, if so.

    Two failure modes the verifier cannot see. A verdict may cite a credit
    another resolution already claimed - double-counting the money while
    every individual check passes. And a verdict may resolve some other
    credit entirely, leaving the exception it was asked about unanswered
    and marking an unrelated row resolved on evidence about a third one.
    """
    taken = sorted(c for c in credits if _already_resolved(outcome, c))
    if taken:
        return f"credit(s) already resolved elsewhere: {taken}"
    if item.bank_txn_id and item.bank_txn_id not in credits:
        return (
            f"verdict resolves {sorted(credits)} but the exception is about "
            f"{item.bank_txn_id}"
        )
    return None


def _rejection_note(verdict: Verdict, verification: VerifierResult) -> str:
    """What the escalation queue says about a verdict that did not survive.

    A rejection is not an embarrassment to bury - it is the system working,
    and 10.5 puts it on screen - so the note names the check that failed
    rather than only saying the exception is still open.
    """
    if verdict.confidence == "unresolvable":
        return f"agent could not resolve: {verdict.hypothesis}"
    return f"agent proposed, verifier rejected: {verification.rejection_reason}"


def _escalate(outcome: RunOutcome, item: ExceptionItem, note: str) -> None:
    if item.bank_txn_id is None:
        return  # settlement-side; there is no credit row to escalate
    outcome.statuses[item.bank_txn_id] = FinalStatus.ESCALATED
    outcome.notes[item.bank_txn_id] = f"{item.reason}: {note}"


def _event(record_ref: str, event_type: str, layer: str = "layer2", **payload: Any) -> dict:
    return {
        "layer": layer, "record_ref": record_ref,
        "event_type": event_type, "payload": payload,
    }


def _attribute(investigator: Investigation, name: str) -> int:
    """Read a counter off the investigator's client, if it keeps one.

    Mocked clients in tests do not, and a run must not depend on cost
    bookkeeping it cannot get.
    """
    return int(getattr(getattr(investigator, "client", None), name, 0) or 0)


def _requests_spent(investigator: Investigation) -> int:
    client = getattr(investigator, "client", None)
    budget = getattr(client, "budget", None)
    if budget is not None:
        return int(getattr(budget, "requests_made", 0) or 0)
    return int(getattr(client, "requests_made", 0) or 0)


# --------------------------------------------------------------------------
# Persistence and replay
# --------------------------------------------------------------------------


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
        if item.bank_txn_id != txn_id:
            continue
        verdict = outcome.verdicts.get(item.exception_id)
        detail: dict[str, Any] = {
            "exception_id": item.exception_id,
            "reason": item.reason,
            "detail": item.detail,
        }
        if txn_id in outcome.agent_matches:
            detail["payment_ids"] = outcome.agent_matches[txn_id]
            detail["needs_human_signoff"] = txn_id in outcome.needs_signoff
        else:
            detail["note"] = outcome.notes.get(txn_id, PENDING_NOTE).split(": ", 1)[-1]
        if verdict is not None:
            detail["verdict"] = verdict.model_dump(mode="json")
        return detail
    return {"note": outcome.notes.get(txn_id, PENDING_NOTE)}


def _verification_for(txn_id: str, outcome: RunOutcome) -> dict:
    """The verifier's own result for this credit, for the drill-down screen.

    Found through the verdict that claimed the credit rather than through
    the exception id, because a split payout resolves two credits from one
    exception and both rows should carry the checks that cleared them.
    """
    for exception_id, verdict in outcome.verdicts.items():
        if verdict.proposed_match is None or txn_id not in _credits_claimed(verdict):
            continue
        verification = outcome.verifications.get(exception_id)
        if verification is not None:
            return verification.model_dump(mode="json")
    return {}


def run_demo(
    audit: AuditLog | None = None,
    data_dir: Path = DEMO_DIR,
    run_id: str | None = None,
    investigator: Investigation | None = None,
) -> RunOutcome:
    """Load the frozen demo set and reconcile it. No network, no API key."""
    return run(
        load_batch(data_dir), seed=DEMO_SEED, audit=audit, run_id=run_id,
        investigator=investigator,
    )


def replay(run_id: str, audit: AuditLog) -> RunOutcome:
    """Rebuild a past run's outcome from the audit log alone.

    Reads only ``runs`` and ``resolutions`` - it does not re-execute the
    cascade or call a model. That is the point (10.1): if the network or
    the LLM API dies on stage, replaying the last good run has to be
    indistinguishable from having run it, and re-running the pipeline
    would not be a replay, it would be a second run that might disagree.

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
    outcome.cost = RunCost.from_dict(audit.run_config(run_id).get("cost", {}))
    _replay_investigations(outcome, audit.read_events(run_id))

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
        elif status is FinalStatus.AGENT_RESOLVED_VERIFIED:
            outcome.agent_matches[txn_id] = detail.get("payment_ids", [])
            if detail.get("needs_human_signoff"):
                outcome.needs_signoff.add(txn_id)
        elif "note" in detail:
            outcome.notes[txn_id] = f"{detail.get('reason', 'escalated')}: {detail['note']}"

    # A replayed outcome carries the matches so metrics.score can check the
    # cited payment ids against ground truth exactly as it would live.
    outcome.layer1 = Layer1Result(
        matches=replayed_matches, total_bank_txns=len(outcome.statuses)
    )
    return outcome


def _replay_investigations(outcome: RunOutcome, events: list[dict]) -> None:
    """Rebuild Layer 2's verdicts and Layer 3's results from the log.

    Without this a replayed run carries statuses and nothing behind them:
    the drill-down and escalation screens would render empty on the exact
    path the demo falls back to when the network dies (10.1). The rows in
    `resolutions` cannot supply it either - a settlement-side exception has
    no bank credit and therefore no row - so the append-only log is both
    the right source and the only complete one.
    """
    for event in events:
        payload = event.get("payload") or {}
        exception_id = payload.get("exception_id")
        if not exception_id:
            continue
        if event["event_type"] == "verdict_recorded":
            outcome.verdicts[exception_id] = Verdict.model_validate(payload["verdict"])
        elif event["event_type"] == "verification_recorded":
            outcome.verifications[exception_id] = VerifierResult.model_validate(
                payload["result"]
            )
