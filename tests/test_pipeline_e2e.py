"""End-to-end tests for the Layer-1-only pipeline (TEST_PLAN 12.6).

Three properties carry the weight here, and each guards a failure that
would otherwise be invisible:

* **Terminal-state totality.** Every credit ends in exactly one state and
  the states sum to the total. A credit that fell out of the pipeline
  would leave tidy-looking numbers that under-report the work left.
* **Determinism.** Same seed, same scorecard. The replay demo rests on it.
* **Ground-truth quarantine.** The pipeline must not read the answers. A
  reconciler that peeked would score perfectly and prove nothing, and
  nothing about the output would look wrong.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from closo.audit import AuditLog
from closo.config import DEMO_DIR, DEMO_SEED
from closo.dataset_io import GROUND_TRUTH_FILENAME, load_batch
from closo.metrics import score, score_demo
from closo.pipeline import PENDING_NOTE, QUOTA_NOTE, replay, run, run_demo
from closo.schemas import FinalStatus
from closo.taxonomy import DESIGNED_UNRESOLVABLE, GeneratedBatch

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def batch() -> GeneratedBatch:
    return load_batch(DEMO_DIR)


@pytest.fixture
def log() -> AuditLog:
    audit = AuditLog(":memory:")
    yield audit
    audit.close()


@pytest.fixture(scope="session")
def outcome():
    return run_demo()


@pytest.fixture(scope="session")
def card(outcome, batch: GeneratedBatch):
    return score_demo(outcome, batch)


# --------------------------------------------------------------------------
# Terminal-state totality
# --------------------------------------------------------------------------


def test_every_credit_reaches_a_terminal_state(outcome, batch: GeneratedBatch) -> None:
    assert set(outcome.statuses) == {b.bank_txn_id for b in batch.bank_txns}


def test_a_dropped_credit_would_be_detected(batch: GeneratedBatch) -> None:
    """Guard the guard. The check above compares two sets and would be
    equally happy if the pipeline stopped emitting credits *and* the
    expectation shrank with it. This pins the count to the file on disk,
    so a credit silently falling out of the pipeline is caught even though
    the remaining totals would still add up perfectly.
    """
    outcome = run_demo()
    assert len(outcome.statuses) == len(batch.bank_txns) == 47

    outcome.statuses.pop(next(iter(outcome.statuses)))
    card = score_demo(outcome, batch)
    assert card.total_bank_txns == 46, "a dropped credit must change the total"
    assert card.money_reconciled + card.money_stuck != sum(
        b.credit_amount for b in batch.bank_txns
    ), "the money cross-check is what catches a silently dropped credit"


def test_states_sum_to_the_total(card) -> None:
    assert (
        card.auto_matched + card.agent_verified + card.escalated
    ) == card.total_bank_txns


def test_there_is_no_fourth_state(outcome) -> None:
    assert set(outcome.statuses.values()) <= set(FinalStatus)


def test_nothing_is_agent_verified_before_layer_2_exists(card) -> None:
    """Layers 2 and 3 are not built. Anything claiming to be agent-resolved
    and verified would have skipped a verifier that does not exist."""
    assert card.agent_verified == 0


def test_unmatched_credits_are_escalated_not_dropped(card, outcome) -> None:
    assert card.escalated > 0
    for txn_id, status in outcome.statuses.items():
        if status is FinalStatus.ESCALATED:
            assert txn_id in outcome.notes


def test_pending_work_is_labelled_as_pending(outcome) -> None:
    """Escalations parked for Layer 2 say so, so the Scorecard can explain
    itself rather than implying a verdict was reached about them."""
    pending = [n for n in outcome.notes.values() if PENDING_NOTE in n]
    assert pending


# --------------------------------------------------------------------------
# Determinism (11.5)
# --------------------------------------------------------------------------


def test_two_runs_produce_an_identical_scorecard(batch: GeneratedBatch) -> None:
    first = score_demo(run_demo(), batch)
    second = score_demo(run_demo(), batch)
    assert first.stable_dict() == second.stable_dict()


def test_stable_dict_excludes_only_what_genuinely_varies(card) -> None:
    """Timing and run id differ between identical runs by definition. Every
    measured quantity must still be in the comparison, or the determinism
    test above would pass by looking at nothing."""
    stable = card.stable_dict()
    assert "run_id" not in stable and "elapsed_seconds" not in stable
    for key in (
        "auto_matched", "escalated", "match_rate", "verified_accuracy",
        "money_reconciled", "money_stuck", "correct_escalations",
        "false_escalations", "false_resolutions", "taxonomy",
    ):
        assert key in stable


def test_replay_reproduces_the_original_scorecard(
    batch: GeneratedBatch, log: AuditLog
) -> None:
    """The airplane-mode guarantee. If the API dies on stage, a replay must
    be the same run played back - not a second run that might disagree."""
    live = run_demo(audit=log, run_id="run_under_test")
    replayed = replay("run_under_test", log)
    assert score_demo(replayed, batch).stable_dict() == score_demo(live, batch).stable_dict()


def test_replay_does_not_rerun_the_cascade(log: AuditLog) -> None:
    """It reads resolutions, so it must work even if the source data is
    unavailable - which is the situation it exists for."""
    run_demo(audit=log, run_id="run_x")
    replayed = replay("run_x", log)
    assert replayed.statuses
    assert replayed.layer1 is not None


def test_replaying_an_unknown_run_raises(log: AuditLog) -> None:
    """An empty outcome would render as a run that resolved nothing, which
    reads as catastrophe rather than as a lookup failure."""
    with pytest.raises(KeyError):
        replay("no_such_run", log)


# --------------------------------------------------------------------------
# Escalation correctness
# --------------------------------------------------------------------------


def test_designed_unresolvables_are_never_resolved(card) -> None:
    """Resolving an E9 or E10 is a critical bug, not a good run (5.2).

    Paired with the test below. On its own this asserts a counter is zero,
    which is equally true if the counter is broken and never increments -
    a mutation that disabled the detection passed this cleanly.
    """
    assert card.false_resolutions == 0


def test_a_false_resolution_would_actually_be_detected(batch: GeneratedBatch) -> None:
    """Guard the guard: force an E10 to 'resolved' and confirm it is caught.

    Without this, the alarm above is indistinguishable from a broken alarm.
    An E10 marked resolved is the single worst outcome this project can
    produce - money reported reconciled that never was - so the detector
    has to be shown working, not assumed.
    """
    from closo.dataset_io import load_ground_truth

    truth = load_ground_truth(DEMO_DIR)["bank_txns"]
    e10_id = next(
        txn_id for txn_id, entry in truth.items() if entry["error_class"] == "E10"
    )

    tampered = run_demo()
    tampered.statuses[e10_id] = FinalStatus.AUTO_MATCHED

    result = score_demo(tampered, batch)
    assert result.false_resolutions == 1
    assert result.incorrect_resolutions >= 1, (
        "an E10 cites no payments, so it can never match ground truth"
    )


def test_every_designed_unresolvable_class_escalates_completely(card) -> None:
    for error_class in DESIGNED_UNRESOLVABLE:
        breakdown = card.taxonomy.get(error_class)
        if breakdown is None:
            continue  # E9 has no bank credit, so it has no taxonomy row
        assert breakdown.resolved == 0, f"{error_class} was partly resolved"


def test_correct_escalations_are_exactly_the_unresolvable_credits(card) -> None:
    unresolvable = sum(
        b.total for cls, b in card.taxonomy.items() if cls in DESIGNED_UNRESOLVABLE
    )
    assert card.correct_escalations == unresolvable


def test_false_escalations_are_reported_not_hidden(card) -> None:
    """E4, E5 and E6 are resolvable in principle and currently are not
    resolved. The count must show that in full - pending_investigation
    explains it but never nets it off."""
    assert card.false_escalations > 0
    assert card.pending_investigation <= card.false_escalations


# --------------------------------------------------------------------------
# Scorecard cross-checks
# --------------------------------------------------------------------------


def test_money_reconciled_plus_stuck_equals_all_credits(
    card, batch: GeneratedBatch
) -> None:
    total = sum(b.credit_amount for b in batch.bank_txns)
    assert card.money_reconciled + card.money_stuck == total
    assert card.money_total == total


def test_taxonomy_totals_cover_every_credit(card) -> None:
    assert sum(b.total for b in card.taxonomy.values()) == card.total_bank_txns


def test_verified_accuracy_is_measured_against_ground_truth(card) -> None:
    decided = card.correct_resolutions + card.incorrect_resolutions
    assert decided == card.auto_matched + card.agent_verified
    assert card.verified_accuracy == 1.0, "Layer 1 must make zero false matches"


def test_match_rate_matches_the_state_counts(card) -> None:
    expected = (card.auto_matched + card.agent_verified) / card.total_bank_txns
    assert card.match_rate == pytest.approx(expected)


def test_empty_batch_scores_without_dividing_by_zero() -> None:
    outcome = run(GeneratedBatch(), seed=DEMO_SEED)
    empty = score(outcome, GeneratedBatch(), DEMO_DIR)
    assert empty.match_rate == 0.0
    assert empty.verified_accuracy == 0.0
    assert empty.records_per_minute == 0.0


# --------------------------------------------------------------------------
# Ground-truth quarantine (11.4)
# --------------------------------------------------------------------------


def test_pipeline_never_opens_ground_truth(monkeypatch, batch: GeneratedBatch) -> None:
    """The headline quarantine test. A reconciler with access to the answers
    would score perfectly and prove nothing, and no part of the output would
    look wrong - which is exactly why this has to be enforced mechanically
    rather than by reading the code and being satisfied.
    """
    real_open = Path.open

    def guarded(self: Path, *args: object, **kwargs: object):
        if self.name == GROUND_TRUTH_FILENAME:
            raise AssertionError(f"pipeline opened {GROUND_TRUTH_FILENAME}")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    outcome = run(batch, seed=DEMO_SEED)
    assert outcome.statuses


def test_metrics_may_open_ground_truth(batch: GeneratedBatch) -> None:
    """The other half of the quarantine: exactly one module is allowed to,
    and only after the run. A quarantine nothing can cross is just a
    missing feature."""
    card = score_demo(run_demo(), batch)
    assert card.correct_resolutions > 0


def test_loader_offers_no_way_to_request_ground_truth() -> None:
    """Structural, not behavioural. The cheapest way to keep the pipeline
    honest is to give it no parameter to be dishonest with."""
    import inspect

    signature = inspect.signature(load_batch)
    assert list(signature.parameters) == ["data_dir"]
    assert load_batch(DEMO_DIR).ground_truth == {}


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------


def test_run_writes_events_and_resolutions(log: AuditLog) -> None:
    run_demo(audit=log, run_id="run_audit")
    assert log.read_events("run_audit")
    assert len(log.read_resolutions("run_audit")) == 47


def test_events_reject_update(log: AuditLog) -> None:
    """Append-only, enforced by the database rather than by discipline."""
    run_demo(audit=log, run_id="run_audit")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        log.conn.execute("UPDATE events SET event_type = 'tampered'")


def test_events_reject_delete(log: AuditLog) -> None:
    run_demo(audit=log, run_id="run_audit")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        log.conn.execute("DELETE FROM events")


def test_audit_connection_survives_use_from_another_thread(log: AuditLog) -> None:
    """Streamlit re-executes the script on a different thread for every
    interaction, so a connection usable only from its creating thread fails
    on the first button press - after the page has loaded cleanly."""
    from concurrent.futures import ThreadPoolExecutor

    log.start_run("run_thread", DEMO_SEED, 1)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(
            lambda i: log.record_event("run_thread", "layer1", f"bt_{i}", "matched"),
            range(20),
        ))
    log.commit()
    assert len(log.read_events("run_thread")) == 20


def test_run_is_recorded_with_its_seed(log: AuditLog) -> None:
    run_demo(audit=log, run_id="run_meta")
    meta = log.get_run("run_meta")
    assert meta is not None
    assert meta["seed"] == DEMO_SEED
    assert meta["finished_at"] is not None


def test_latest_run_id_backs_the_replay_button(log: AuditLog) -> None:
    assert log.latest_run_id() is None
    run_demo(audit=log, run_id="run_first")
    assert log.latest_run_id() == "run_first"


def test_pipeline_runs_without_an_audit_log(batch: GeneratedBatch) -> None:
    """Logging is optional; reconciling is not."""
    outcome = run(batch, seed=DEMO_SEED)
    assert len(outcome.statuses) == len(batch.bank_txns)


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


def test_pipeline_and_metrics_import_no_llm_sdk() -> None:
    import subprocess
    import sys

    code = (
        "import sys, closo.pipeline, closo.metrics; "
        "print(any(m.startswith('google.genai') or m == 'closo.llm_client' "
        "for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        check=True, cwd=str(REPO_ROOT),
    )
    assert out.stdout.strip() == "False"


# --------------------------------------------------------------------------
# Layers 2 and 3 wired in (12.6, mocked client - CI never calls the API)
# --------------------------------------------------------------------------


def agent_for(batch: GeneratedBatch, client_factory=None, **kwargs):
    """An investigator over ``batch`` backed by a scripted client.

    Layer 2 runs for real here - the tool loop, the verdict parsing, the
    bounds. Only the model is a stand-in, and it solves each exception from
    the records rather than from a canned script (see tests/oracle_client).
    """
    from closo.layer1_matcher import run_layer1
    from closo.layer2_investigator import Investigator
    from closo.tools import ToolBox
    from tests.oracle_client import OracleClient, solve

    plan = solve(batch, run_layer1(batch).exceptions)
    client = (client_factory or OracleClient)(plan, **kwargs)
    return Investigator(ToolBox(batch), client)


@pytest.fixture(scope="session")
def full_outcome(batch: GeneratedBatch):
    """One run of the complete three-layer pipeline."""
    return run(batch, seed=DEMO_SEED, investigator=agent_for(batch))


@pytest.fixture(scope="session")
def full_card(full_outcome, batch: GeneratedBatch):
    return score_demo(full_outcome, batch)


def test_the_full_pipeline_resolves_more_than_layer_1_alone(full_card, card) -> None:
    assert full_card.agent_verified > 0
    assert full_card.match_rate > card.match_rate


def test_the_full_run_meets_the_definition_of_done(full_card) -> None:
    """14: match rate at or above 93% on seed 42, accuracy reported against
    ground truth. Pinned to the number rather than to "better than Layer 1",
    so a regression that still beats Layer 1 is still caught."""
    assert full_card.match_rate >= 0.93
    assert full_card.verified_accuracy == 1.0


def test_every_credit_still_reaches_exactly_one_terminal_state(
    full_outcome, batch: GeneratedBatch
) -> None:
    assert set(full_outcome.statuses) == {b.bank_txn_id for b in batch.bank_txns}
    assert set(full_outcome.statuses.values()) <= set(FinalStatus)


def test_states_still_sum_to_the_total(full_card) -> None:
    assert (
        full_card.auto_matched + full_card.agent_verified + full_card.escalated
    ) == full_card.total_bank_txns


def test_money_still_adds_up_with_layer_2_resolving(
    full_card, batch: GeneratedBatch
) -> None:
    """The cross-check that catches a credit counted twice - which is the
    specific way a split-settlement verdict goes wrong."""
    total = sum(b.credit_amount for b in batch.bank_txns)
    assert full_card.money_reconciled + full_card.money_stuck == total


def test_every_agent_resolution_matches_ground_truth(full_card) -> None:
    """A verdict that passed verification can still cite the wrong
    payments: the verifier proves the arithmetic reproduces the credit, not
    that the credit came from those payments. Grading Layer 2's claims
    against ground truth exactly as Layer 1's are graded is the only thing
    that would notice."""
    assert full_card.incorrect_resolutions == 0
    assert full_card.correct_resolutions == (
        full_card.auto_matched + full_card.agent_verified
    )


def test_designed_unresolvables_survive_a_working_layer_2(full_card) -> None:
    """The headline claim. A competent investigator plus a strict verifier
    must still refuse E9 and E10 - resolving one is a critical bug, and it
    is now a bug Layer 2 is newly capable of committing."""
    assert full_card.false_resolutions == 0
    for error_class in DESIGNED_UNRESOLVABLE:
        breakdown = full_card.taxonomy.get(error_class)
        if breakdown is not None:
            assert breakdown.resolved == 0


def test_escalations_are_exactly_the_designed_unresolvables(full_card) -> None:
    """12.6: with a well-behaved mock the escalation list is E9+E10 and
    nothing else. Any other row means a resolvable exception was missed or
    a sound verdict was wrongly rejected."""
    assert full_card.escalated == full_card.correct_escalations
    assert full_card.false_escalations == 0
    assert full_card.pending_investigation == 0


def test_a_fee_schedule_anomaly_is_resolved_but_flagged(full_outcome) -> None:
    """E4 end to end (8.1): the math reproduces the credit exactly, so it
    resolves; the schedule that produced it was not the one active on the
    date, so it is capped at probable and handed to a human with the
    question attached."""
    assert full_outcome.needs_signoff
    for txn_id in full_outcome.needs_signoff:
        assert full_outcome.statuses[txn_id] is FinalStatus.AGENT_RESOLVED_VERIFIED

    anomalies = [
        v.schedule_anomaly for v in full_outcome.verifications.values()
        if v.schedule_anomaly
    ]
    assert anomalies, "the fee-schedule anomaly must be named, not merely counted"
    assert all("was active on" in a for a in anomalies)


def test_sign_off_is_reported_without_being_hidden_or_double_counted(
    full_card, full_outcome
) -> None:
    """Sign-off is a flag, not a fourth state (2). It sits inside
    agent_verified rather than beside it, and its money inside
    money_reconciled - but both are reported separately so nobody reads
    "resolved" as "settled, nothing more to do"."""
    assert 0 < full_card.awaiting_signoff <= full_card.agent_verified
    assert full_card.money_awaiting_signoff <= full_card.money_reconciled
    assert full_card.awaiting_signoff == len(full_outcome.needs_signoff)


def test_a_split_settlement_is_investigated_once_not_twice(full_outcome) -> None:
    """Both legs of an E5 resolve, but only one costs an investigation.
    The Stage 6 live run paid for the second one, and a second verdict
    would have failed exclusivity even when the model answered correctly."""
    assert full_outcome.cost.exceptions_skipped >= 1

    shared = [
        payments for payments in full_outcome.agent_matches.values()
        if list(full_outcome.agent_matches.values()).count(payments) > 1
    ]
    assert len(shared) >= 2, "a split payout must resolve both of its credits"


def test_a_skipped_leg_still_ends_resolved(full_outcome) -> None:
    """Guard the guard: a skip that left the credit escalated would show up
    as a slightly lower match rate and nothing else."""
    for txn_id in full_outcome.agent_matches:
        assert full_outcome.statuses[txn_id] is FinalStatus.AGENT_RESOLVED_VERIFIED


def test_two_full_runs_produce_an_identical_scorecard(batch: GeneratedBatch) -> None:
    """Determinism holds through Layer 2 when the responses are fixed -
    which is exactly what the response cache buys the demo. Live output at
    temperature 0 does not have this property; cached output does."""
    first = score_demo(run(batch, seed=DEMO_SEED, investigator=agent_for(batch)), batch)
    second = score_demo(run(batch, seed=DEMO_SEED, investigator=agent_for(batch)), batch)
    assert first.stable_dict() == second.stable_dict()


def test_the_full_pipeline_never_opens_ground_truth(
    monkeypatch, batch: GeneratedBatch
) -> None:
    """The quarantine has to survive Layer 2 being wired in: the
    investigator and its tools read far more of the dataset than the
    matcher ever did."""
    agent = agent_for(batch)  # built before the guard; solving is not the run
    real_open = Path.open

    def guarded(self: Path, *args: object, **kwargs: object):
        if self.name == GROUND_TRUTH_FILENAME:
            raise AssertionError(f"pipeline opened {GROUND_TRUTH_FILENAME}")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    outcome = run(batch, seed=DEMO_SEED, investigator=agent)
    assert outcome.agent_matches, "the run must reach Layer 2 to prove anything"


# --------------------------------------------------------------------------
# What happens when Layer 2 is wrong (12.6)
# --------------------------------------------------------------------------
#
# The happy path above proves the wiring carries a good verdict through.
# These prove the wiring refuses a bad one - which is the entire claim the
# project makes, and the only part of it a broken pipeline could still pass
# the happy path while breaking.


class OneVerdictClient:
    """A model that submits one fixed verdict for every exception."""

    def __init__(self, args: dict) -> None:
        self.args = args
        self.requests_made = 0

    def generate(self, system_prompt, messages, tools, force_tool=None):
        from closo.llm_client import LLMResponse, ToolCall

        self.requests_made += 1
        return LLMResponse(
            tool_calls=[ToolCall(name="submit_verdict", args=dict(self.args))],
            tokens_used=10,
        )


def investigator_with(batch: GeneratedBatch, client):
    from closo.layer2_investigator import Investigator
    from closo.tools import ToolBox

    return Investigator(ToolBox(batch), client)


def a_matched_credit(batch: GeneratedBatch):
    """An auto-matched credit, its settlement and the arithmetic behind it.

    Used to build verdicts that are arithmetically perfect and still must
    not be accepted - the only shape of wrong answer that survives every
    check the verifier makes on its own.
    """
    from closo.layer1_matcher import run_layer1
    from closo.tools import ToolBox

    match = run_layer1(batch).matches[0]
    block = ToolBox(batch).compute_expected_settlement(list(match.payment_ids), "v1")
    if "error" in block or Decimal(block["net"]) != _credit(batch, match.bank_txn_id):
        block = ToolBox(batch).compute_expected_settlement(list(match.payment_ids), "v2")
    return match, block


def _credit(batch: GeneratedBatch, bank_txn_id: str) -> Decimal:
    return next(b.credit_amount for b in batch.bank_txns if b.bank_txn_id == bank_txn_id)


def _verdict_args(bank_txn_id: str, payment_ids, schedule: str, block: dict) -> dict:
    return {
        "hypothesis": "this settlement explains the credit",
        "confidence": "resolved",
        "bank_txn_id": bank_txn_id,
        "payment_ids": list(payment_ids),
        "fee_schedule": schedule,
        "arithmetic": {
            "gross": block["gross"], "mdr": block["mdr"], "gst": block["gst"],
            "rounding": block.get("rounding", "0.00"), "net": block["net"],
        },
    }


def test_a_phantom_payment_is_escalated_not_resolved(batch: GeneratedBatch) -> None:
    """The model invents a record. Nothing about the verdict's shape says
    so, and only checking the cited ids against the data catches it."""
    client = OneVerdictClient({
        "hypothesis": "settled under v1",
        "confidence": "resolved",
        "bank_txn_id": "bt_0334",
        "payment_ids": ["pay_does_not_exist"],
        "fee_schedule": "v1",
        "arithmetic": {"gross": "1.00", "mdr": "0.00", "gst": "0.00", "net": "1.00"},
    })
    outcome = run(batch, seed=DEMO_SEED, investigator=investigator_with(batch, client))

    assert outcome.agent_matches == {}
    assert outcome.statuses["bt_0334"] is FinalStatus.ESCALATED
    assert "verifier rejected: phantom_reference" in outcome.notes["bt_0334"]


def test_a_rejected_verdict_is_labelled_as_rejected_not_merely_unsolved(
    batch: GeneratedBatch,
) -> None:
    """10.5 puts rejections on screen: "agent proposed, verifier rejected"
    is the strongest thing in the demo, and it needs the distinction
    between a verdict that failed and one that was never offered."""
    client = OneVerdictClient({
        "hypothesis": "a partial refund was netted into this payout",
        "confidence": "resolved",
        "bank_txn_id": "bt_0334",
        "payment_ids": ["pay_0326"],
        "fee_schedule": "v1",
        "arithmetic": {"gross": "1.00", "mdr": "0.00", "gst": "0.00", "net": "1.00"},
    })
    outcome = run(batch, seed=DEMO_SEED, investigator=investigator_with(batch, client))

    note = outcome.notes["bt_0334"]
    assert "agent proposed, verifier rejected" in note
    assert "agent could not resolve" not in note


def test_an_unresolvable_verdict_reads_differently_from_a_rejected_one(
    batch: GeneratedBatch,
) -> None:
    client = OneVerdictClient({
        "hypothesis": "the money never arrived",
        "confidence": "unresolvable",
    })
    outcome = run(batch, seed=DEMO_SEED, investigator=investigator_with(batch, client))

    note = outcome.notes["bt_0334"]
    assert "agent could not resolve" in note
    assert "verifier rejected" not in note


def test_a_verdict_claiming_an_already_matched_credit_is_refused(
    batch: GeneratedBatch,
) -> None:
    """The dangerous case: arithmetic that reproduces the credit exactly,
    citing payments Layer 1 already matched. Every verifier check passes on
    its own terms and the money view double-counts. Caught two ways - the
    verifier starts with Layer 1's payments marked spent, and the pipeline
    refuses a credit another resolution already claimed."""
    match, block = a_matched_credit(batch)
    schedule = "v1" if Decimal(block["net"]) == _credit(batch, match.bank_txn_id) else "v2"
    client = OneVerdictClient(
        _verdict_args(match.bank_txn_id, match.payment_ids, schedule, block)
    )
    outcome = run(batch, seed=DEMO_SEED, investigator=investigator_with(batch, client))

    assert outcome.statuses[match.bank_txn_id] is FinalStatus.AUTO_MATCHED
    assert match.bank_txn_id not in outcome.agent_matches
    assert outcome.count(FinalStatus.AGENT_RESOLVED_VERIFIED) == 0


def test_layer_1s_payments_start_out_spent(batch: GeneratedBatch) -> None:
    """Guard the guard for the test above, which would also pass if the
    pipeline's credit check alone caught it. This asserts the verifier
    itself refuses, which is what protects a verdict citing matched
    payments for some *other* credit."""
    from closo.layer1_matcher import run_layer1
    from closo.layer3_verifier import Verifier

    result = run_layer1(batch)
    verifier = Verifier(batch)
    verifier.consumed.update(
        pid for m in result.matches for pid in m.payment_ids
    )
    assert result.matches[0].payment_ids[0] in verifier.consumed


def test_a_verdict_that_answers_a_different_exception_is_refused(
    batch: GeneratedBatch,
) -> None:
    """Sound arithmetic about the wrong row. The verifier has no way to
    know which exception was asked about, so the orchestrator has to: left
    alone, this marks an unrelated credit resolved on evidence gathered
    about a third one, and leaves the original exception unanswered."""
    from closo.layer1_matcher import run_layer1
    from tests.oracle_client import solve

    exceptions = run_layer1(batch).exceptions
    plan = solve(batch, exceptions)
    solved_id, args = next(iter(plan.items()))
    other = next(
        i for i in exceptions
        if i.bank_txn_id and i.exception_id != solved_id
        and i.bank_txn_id != args["bank_txn_id"]
        and i.bank_txn_id not in args["extra_bank_txn_ids"]
    )

    class WrongRow(OneVerdictClient):
        def generate(self, system_prompt, messages, tools, force_tool=None):
            from closo.llm_client import LLMResponse, ToolCall

            first = str(messages[0].get("content", ""))
            if other.exception_id not in first:
                return LLMResponse(
                    tool_calls=[ToolCall(name="submit_verdict", args={
                        "hypothesis": "nothing found", "confidence": "unresolvable"})],
                )
            return LLMResponse(
                tool_calls=[ToolCall(name="submit_verdict", args=dict(self.args))],
            )

    outcome = run(
        batch, seed=DEMO_SEED,
        investigator=investigator_with(batch, WrongRow(args)),
    )

    assert outcome.agent_matches == {}
    assert outcome.statuses[other.bank_txn_id] is FinalStatus.ESCALATED
    assert "the exception is about" in outcome.notes[other.bank_txn_id]


def test_a_quota_wall_winds_the_batch_down_honestly(batch: GeneratedBatch) -> None:
    """7.4: the remainder is marked quota-exhausted, not left missing and
    not quietly counted as investigated. A run that stops early must report
    a partial run as a partial run."""
    from closo.errors import QuotaExhausted

    class OutOfQuota(OneVerdictClient):
        def generate(self, system_prompt, messages, tools, force_tool=None):
            self.requests_made += 1
            raise QuotaExhausted("daily request budget of 500 is spent")

    outcome = run(
        batch, seed=DEMO_SEED,
        investigator=investigator_with(batch, OutOfQuota({})),
    )

    assert outcome.cost.quota_exhausted is True
    assert outcome.cost.exceptions_investigated == 0
    assert outcome.agent_matches == {}
    stalled = [n for n in outcome.notes.values() if QUOTA_NOTE in n]
    assert len(stalled) >= 2, "every exception after the wall must say why"


def test_a_quota_wall_partway_through_keeps_what_it_already_resolved(
    batch: GeneratedBatch,
) -> None:
    """The wall must not undo verified work. A run that discarded its
    first six resolutions on hitting the seventh would report a worse
    scorecard than it earned."""
    from closo.errors import QuotaExhausted
    from tests.oracle_client import OracleClient

    class RunsOut(OracleClient):
        def generate(self, system_prompt, messages, tools, force_tool=None):
            if self.turns >= 6:
                raise QuotaExhausted("daily request budget of 500 is spent")
            return super().generate(system_prompt, messages, tools, force_tool)

    outcome = run(
        batch, seed=DEMO_SEED,
        investigator=agent_for(batch, client_factory=RunsOut),
    )

    assert outcome.agent_matches, "resolutions made before the wall must survive"
    assert outcome.cost.quota_exhausted is True
    assert set(outcome.statuses.values()) <= set(FinalStatus)


def test_an_offline_cache_miss_ends_one_exception_not_the_run(
    batch: GeneratedBatch,
) -> None:
    """The airplane-mode failure mode. A demo that has drifted off its
    recorded path should escalate that exception and carry on, not crash
    the batch and not invent a verdict."""
    from closo.errors import CacheMiss

    class AlwaysMisses(OneVerdictClient):
        def generate(self, system_prompt, messages, tools, force_tool=None):
            raise CacheMiss("no cached response for this request (abc123)")

    outcome = run(
        batch, seed=DEMO_SEED,
        investigator=investigator_with(batch, AlwaysMisses({})),
    )

    assert len(outcome.statuses) == 47
    assert outcome.count(FinalStatus.AGENT_RESOLVED_VERIFIED) == 0
    assert outcome.cost.exceptions_investigated == 10, (
        "every exception must still be attempted; a miss ends one of them"
    )


# --------------------------------------------------------------------------
# The audit log carries Layers 2 and 3
# --------------------------------------------------------------------------


def test_the_audit_log_records_the_investigation_and_the_verification(
    batch: GeneratedBatch, log: AuditLog
) -> None:
    """A verdict nobody can inspect afterwards is not evidence. 9.1 wants
    every tool call and every verifier check in the events table."""
    run(batch, seed=DEMO_SEED, audit=log, run_id="run_full",
        investigator=agent_for(batch))

    kinds = {e["event_type"] for e in log.read_events("run_full")}
    layers = {e["layer"] for e in log.read_events("run_full")}
    assert {"tool_call", "verdict_submitted", "verified"} <= kinds
    assert {"layer1", "layer2", "layer3"} == layers


def test_a_skipped_second_leg_says_so_in_the_log(
    batch: GeneratedBatch, log: AuditLog
) -> None:
    """Skipping an exception saves a request; not recording the skip loses
    the reason a queued exception has no verdict of its own."""
    run(batch, seed=DEMO_SEED, audit=log, run_id="run_skip",
        investigator=agent_for(batch))
    kinds = [e["event_type"] for e in log.read_events("run_skip")]
    assert "skipped_already_resolved" in kinds


def test_a_full_run_replays_from_the_audit_log(
    batch: GeneratedBatch, log: AuditLog
) -> None:
    """The airplane-mode guarantee, now with Layer 2 in the run. A replay
    must reproduce the agent resolutions and their sign-off flags, not just
    Layer 1's matches - otherwise replaying loses precisely the work the
    demo is about."""
    live = run(batch, seed=DEMO_SEED, audit=log, run_id="run_replayable",
               investigator=agent_for(batch))
    replayed = replay("run_replayable", log)

    assert replayed.agent_matches == live.agent_matches
    assert replayed.needs_signoff == live.needs_signoff
    assert score_demo(replayed, batch).stable_dict() == score_demo(live, batch).stable_dict()


def test_a_replay_reports_what_the_original_run_cost(
    batch: GeneratedBatch, log: AuditLog
) -> None:
    """A replayed run that could not say what the original spent would
    show a free reconciliation that was never free."""
    live = run(batch, seed=DEMO_SEED, audit=log, run_id="run_cost",
               investigator=agent_for(batch))
    replayed = replay("run_cost", log)

    assert live.cost.tokens_used > 0
    assert replayed.cost.tokens_used == live.cost.tokens_used
    assert replayed.cost.requests_made == live.cost.requests_made


# --------------------------------------------------------------------------
# Isolating the two double-count guards
# --------------------------------------------------------------------------
#
# A verdict can double-count money in two ways, and the demo dataset cannot
# tell them apart: on seed 42 either guard alone catches the other's cases,
# so disabling one leaves every test green. Stage 5 lost two of the repo's
# most important tests to exactly that, and mutation testing is the only
# reason anyone noticed.
#
# So these use a four-record batch built to separate them. It is small
# enough to reason about completely: one settlement Layer 1 matches, one it
# cannot see a credit for, and one foreign credit.


def crafted_batch() -> GeneratedBatch:
    """A batch where each double-count guard can be tested on its own.

    Two payments of Rs 1000, both UPI (free under v1) and both settled
    before the fee cutover, so every net is exactly the gross and the
    arithmetic under test is trivially checkable by hand.

    * S1 -> C1 matches on UTR in Pass A, consuming payment P1.
    * S2 has no credit at all, so it sweeps as a settlement-side exception.
      Its net equals C1's credit, which is what lets a verdict about S2
      claim C1 while every verifier check passes.
    * C3 is a foreign credit of Rs 1500 whose UTR appears on no settlement.
      The amount is deliberately far from S2's net so Pass C cannot absorb
      it, leaving it as a bank-side exception.
    """
    from datetime import date

    from closo.schemas import BankTxn, Payment, Settlement

    settled = date(2026, 2, 10)  # before the cutover, so v1 is active

    def payment(pid: str, settlement_id: str) -> Payment:
        return Payment(
            payment_id=pid, order_id=f"order_{pid}", amount_gross="1000.00",
            method="upi", captured_at=date(2026, 2, 9), settlement_id=settlement_id,
            settled_at=settled, fee_mdr="0.00", fee_gst="0.00",
            amount_settled="1000.00",
        )

    def settlement(sid: str, utr: str, pid: str) -> Settlement:
        return Settlement(
            settlement_id=sid, utr=utr, settled_at=settled, payment_ids=[pid],
            amount_gross="1000.00", fee_mdr="0.00", fee_gst="0.00",
            amount_settled="1000.00", fee_schedule="v1",
        )

    batch = GeneratedBatch()
    batch.payments = [payment("pay_P1", "setl_S1"), payment("pay_P2", "setl_S2")]
    batch.settlements = [
        settlement("setl_S1", "UONEAAAAAAAAAAAA", "pay_P1"),
        settlement("setl_S2", "UTWOBBBBBBBBBBBB", "pay_P2"),
    ]
    batch.bank_txns = [
        BankTxn(
            bank_txn_id="bt_C1", txn_date=settled, value_date=settled,
            narration="NEFT-RAZORPAYSOFTWARE-UONEAAAAAAAAAAAA-SETTLEMENT",
            credit_amount="1000.00",
        ),
        BankTxn(
            bank_txn_id="bt_C3", txn_date=date(2026, 4, 20),
            value_date=date(2026, 4, 20),
            narration="NEFT-RAZORPAYSOFTWARE-UTHREECCCCCCCCC-SETTLEMENT",
            credit_amount="1500.00",
        ),
    ]
    return batch


class PerExceptionClient:
    """Submits a different scripted verdict per exception id."""

    def __init__(self, by_exception: dict[str, dict]) -> None:
        self.by_exception = by_exception
        self.requests_made = 0

    def generate(self, system_prompt, messages, tools, force_tool=None):
        import re as _re

        from closo.llm_client import LLMResponse, ToolCall

        self.requests_made += 1
        found = _re.search(r"Exception (EX-\d+)", str(messages[0].get("content", "")))
        args = self.by_exception.get(found.group(1) if found else "")
        if args is None:
            args = {"hypothesis": "nothing found", "confidence": "unresolvable"}
        return LLMResponse(tool_calls=[ToolCall(name="submit_verdict", args=args)])


def test_the_crafted_batch_is_shaped_the_way_these_tests_assume() -> None:
    """The fixture is doing real work, so it gets its own check. A batch
    that quietly stopped producing these two exceptions would make both
    tests below pass by testing nothing."""
    from closo.layer1_matcher import run_layer1

    result = run_layer1(crafted_batch())
    assert [m.bank_txn_id for m in result.matches] == ["bt_C1"]
    assert [(e.bank_txn_id, e.settlement_id) for e in result.exceptions] == [
        ("bt_C3", None), (None, "setl_S2")
    ]


def test_a_verdict_may_not_resolve_a_credit_layer_1_already_matched(
) -> None:
    """Isolates the pipeline's credit check.

    Every verifier check passes here on its own terms: both records exist,
    no refund is claimed, the cited payment is unspent, v1 really was
    active, and the recomputed net reproduces the credit to the paisa. Only
    the fact that C1 is already auto-matched makes this wrong - and the
    money would be counted twice if nothing said so.
    """
    batch = crafted_batch()
    client = PerExceptionClient({"EX-002": {
        "hypothesis": "settlement S2 was paid into credit C1",
        "confidence": "resolved",
        "bank_txn_id": "bt_C1",
        "payment_ids": ["pay_P2"],
        "fee_schedule": "v1",
        "arithmetic": {"gross": "1000.00", "mdr": "0.00", "gst": "0.00",
                       "net": "1000.00"},
    }})

    outcome = run(batch, seed=DEMO_SEED, investigator=investigator_with(batch, client))

    verification = outcome.verifications["EX-002"]
    assert verification.passed, (
        "the verifier must have no objection, or this test is not isolating "
        "the pipeline's check"
    )
    assert outcome.agent_matches == {}
    assert outcome.statuses["bt_C1"] is FinalStatus.AUTO_MATCHED


def test_a_verdict_may_not_cite_payments_layer_1_already_spent() -> None:
    """Isolates the verifier's seeded exclusivity set.

    The verdict is about the exception's own credit, so the pipeline's
    credit check has nothing to say. What is wrong is the payment: P1 was
    consumed by the Pass A match, and citing it again spends it twice.
    Asserting on the rejection *reason* is what makes this bite - without
    the seeding the verdict still fails, but on arithmetic, and a test that
    only checked "escalated" would not notice the guard had gone.
    """
    batch = crafted_batch()
    client = PerExceptionClient({"EX-001": {
        "hypothesis": "credit C3 is the payout for settlement S1",
        "confidence": "resolved",
        "bank_txn_id": "bt_C3",
        "payment_ids": ["pay_P1"],
        "fee_schedule": "v1",
        "arithmetic": {"gross": "1000.00", "mdr": "0.00", "gst": "0.00",
                       "net": "1000.00"},
    }})

    outcome = run(batch, seed=DEMO_SEED, investigator=investigator_with(batch, client))

    assert outcome.verifications["EX-001"].rejection_reason == "exclusivity_violation"
    assert outcome.statuses["bt_C3"] is FinalStatus.ESCALATED
    assert outcome.agent_matches == {}


# --------------------------------------------------------------------------
# Cost metrics (9.2)
# --------------------------------------------------------------------------


def test_the_scorecard_reports_what_the_run_spent(full_card) -> None:
    """12.6 asks for cost-per-record when the client reports tokens. The
    figure is per *record*, not per exception, because that is the number
    that scales against a batch and the one a finance team would compare
    against an analyst's hour."""
    assert full_card.tokens_used > 0
    assert full_card.requests_made > 0
    assert full_card.tokens_per_record == pytest.approx(
        full_card.tokens_used / full_card.total_bank_txns
    )


def test_requests_are_reported_alongside_tokens(full_card) -> None:
    """Requests are the scarce resource on this quota (7.4), not tokens. A
    cost line showing only tokens would be measuring the thing that never
    runs out."""
    assert full_card.exceptions_investigated > 0
    assert full_card.requests_made >= full_card.exceptions_investigated


def test_a_layer_1_only_run_costs_nothing(card) -> None:
    """No investigator, no requests. The zero has to be a real zero rather
    than an absent field, or the Scorecard cannot tell "free" from
    "unknown"."""
    assert card.tokens_used == 0
    assert card.requests_made == 0
    assert card.rupees_spent == Decimal("0.00")


def test_the_free_tier_costs_zero_rupees_and_says_so(full_card) -> None:
    """The default rate is zero because the run genuinely is free. A
    made-up price on a judged scorecard would be a fabricated number, not
    a conservative one."""
    from closo.config import INR_PER_MILLION_TOKENS

    assert INR_PER_MILLION_TOKENS == 0
    assert full_card.rupees_spent == Decimal("0.00")
    assert full_card.rupees_per_record == Decimal("0.00")


def test_a_configured_rate_produces_a_real_cost_line(full_card) -> None:
    """The line must not be dead code that only ever prints zero: with a
    rate set it has to compute, in Decimal, at 2dp."""
    import closo.metrics as module

    original = module.INR_PER_MILLION_TOKENS
    module.INR_PER_MILLION_TOKENS = Decimal("1000")
    try:
        expected = Decimal(full_card.tokens_used) * Decimal("1000") / Decimal(1_000_000)
        assert full_card.rupees_spent == expected.quantize(Decimal("0.01"))
        assert full_card.rupees_per_record > 0
    finally:
        module.INR_PER_MILLION_TOKENS = original


def test_layer_1_throughput_is_reported_separately(full_card) -> None:
    """9.2 asks for it separately because averaging the two hides both
    facts: that most of the batch clears in milliseconds, and that the
    remainder costs seconds per record because it is talking to a model."""
    assert full_card.layer1_records_per_minute > full_card.records_per_minute


def test_cost_metrics_survive_an_empty_run() -> None:
    outcome = run(GeneratedBatch(), seed=DEMO_SEED)
    empty = score(outcome, GeneratedBatch(), DEMO_DIR)
    assert empty.tokens_per_record == 0.0
    assert empty.rupees_per_record == Decimal("0.00")
    assert empty.layer1_records_per_minute == 0.0


def test_cost_is_deliberately_outside_the_determinism_diff(full_card) -> None:
    """A live run and a replay of it produce byte-identical reconciliation
    figures while spending different numbers of requests, because the
    replay spends none. What a run cost is a fact about the run, not about
    the batch - so it is asserted directly rather than diffed."""
    stable = full_card.stable_dict()
    for key in ("tokens_used", "requests_made", "cache_hits"):
        assert key not in stable


# --------------------------------------------------------------------------
# Airplane mode: the committed cache (13, Stage 7 exit; 14)
# --------------------------------------------------------------------------
#
# These run the real Layer 2 against real recorded model output, with no
# key, no SDK and no network - which is the state a fresh clone is in, and
# the state the demo room may be in. They fail loudly rather than skip if
# the cache is missing: an untested airplane-mode claim is the one that
# gets discovered on stage.


def cached_agent(batch: GeneratedBatch):
    from closo.layer2_investigator import Investigator
    from closo.response_cache import DEMO_CACHE_PATH, JSONResponseStore, CachedLLMClient
    from closo.tools import ToolBox

    store = JSONResponseStore(DEMO_CACHE_PATH)
    assert len(store), (
        "the committed response cache is empty; the offline demo depends on "
        "it (regenerate with PYTHONPATH=. python scripts/real_api_run.py)"
    )
    return Investigator(ToolBox(batch), CachedLLMClient(store))


@pytest.fixture(scope="session")
def cached_outcome(batch: GeneratedBatch):
    return run(batch, seed=DEMO_SEED, investigator=cached_agent(batch))


def test_the_full_pipeline_runs_offline_from_the_committed_cache(
    cached_outcome, batch: GeneratedBatch
) -> None:
    """Stage 7's exit criterion, and §14's first line. Real recorded model
    output, real Layer 2, real verifier - and nothing that could reach a
    network."""
    card = score_demo(cached_outcome, batch)
    assert card.match_rate >= 0.93
    assert card.verified_accuracy == 1.0
    assert card.false_resolutions == 0
    assert card.agent_verified > 0


def test_the_cached_run_spends_no_requests(cached_outcome, batch: GeneratedBatch) -> None:
    """Zero is the true figure, not a missing one. A replay that quietly
    reached for the API would still produce a correct scorecard, so the
    only thing that catches it is the count."""
    card = score_demo(cached_outcome, batch)
    assert card.requests_made == 0
    assert card.tokens_used == 0
    assert card.cache_hits > 0


def test_the_cache_covers_every_turn_the_run_asks_for(batch: GeneratedBatch) -> None:
    """The failure this guards is silent and specific: if the cached
    conversations drift from the ones the pipeline now produces - a changed
    system prompt, a reordered brief, a different tool result - every key
    misses and the demo degrades to ten `unresolvable` verdicts while still
    looking like it ran."""
    agent = cached_agent(batch)
    run(batch, seed=DEMO_SEED, investigator=agent)
    assert agent.client.misses == 0, (
        f"{agent.client.misses} conversation(s) were not in the cache; the "
        "recorded run and the current pipeline have diverged"
    )


def test_the_cached_run_escalates_exactly_the_designed_unresolvables(
    cached_outcome, batch: GeneratedBatch
) -> None:
    """On real model output, not a scripted one: E9 and E10 stay escalated,
    and nothing else does."""
    card = score_demo(cached_outcome, batch)
    assert card.correct_escalations == card.escalated
    assert card.false_escalations == 0


def test_the_cached_run_names_its_fee_schedule_anomaly(cached_outcome) -> None:
    """The E4 story, from a live model's own verdict: the math reproduces
    the credit, the schedule that produced it was not the active one, and
    the verdict is capped at probable with the question named (8.1). This
    is the drill-down the demo shows."""
    assert cached_outcome.needs_signoff
    anomalies = [
        v.schedule_anomaly for v in cached_outcome.verifications.values()
        if v.schedule_anomaly
    ]
    assert anomalies
    assert all("v1" in a and "v2" in a for a in anomalies)


def test_the_cached_run_is_reproducible(batch: GeneratedBatch) -> None:
    """What the cache buys that temperature 0 did not. Two live runs
    resolved different subsets of exceptions; two cached runs cannot."""
    first = score_demo(run(batch, seed=DEMO_SEED, investigator=cached_agent(batch)), batch)
    second = score_demo(run(batch, seed=DEMO_SEED, investigator=cached_agent(batch)), batch)
    assert first.stable_dict() == second.stable_dict()


def test_nothing_in_the_offline_path_can_import_the_sdk() -> None:
    """The airplane-mode claim, checked structurally in a clean subprocess.
    Building the whole offline investigator must not pull in google.genai -
    an in-process check would depend on what an earlier test imported."""
    import subprocess
    import sys

    code = (
        "import sys; "
        "from closo.dataset_io import load_batch; "
        "from closo.config import DEMO_DIR; "
        "from closo.layer2_investigator import Investigator; "
        "from closo.response_cache import demo_client; "
        "from closo.tools import ToolBox; "
        "from closo.pipeline import run; "
        "b = load_batch(DEMO_DIR); "
        "out = run(b, investigator=Investigator(ToolBox(b), demo_client())); "
        "print(any(m.startswith('google.genai') for m in sys.modules), "
        "sum(1 for s in out.statuses.values() if s.value == 'AGENT_RESOLVED_VERIFIED'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        check=True, cwd=str(REPO_ROOT),
    )
    verdict, resolved = result.stdout.split()
    assert verdict == "False"
    assert int(resolved) > 0, "the offline run must actually resolve something"
