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
from pathlib import Path

import pytest

from closo.audit import AuditLog
from closo.config import DEMO_DIR, DEMO_SEED
from closo.dataset_io import GROUND_TRUTH_FILENAME, load_batch
from closo.metrics import score, score_demo
from closo.pipeline import PENDING_NOTE, replay, run, run_demo
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
