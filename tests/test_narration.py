"""Tests for the run narrator (TEST_PLAN 12.7, WORKFLOWS 10).

The Live-run screen is built entirely on this, so what matters is not that
a story gets produced but that it says the *right* thing about each ending.
A run has four different outcomes per exception - verified, verified but
needing sign-off, proposed and rejected, and never answered - and a
narrator that blurred any two of them would put a confident screen in front
of a judge and be wrong on the most interesting row.

Hand-built event lists throughout, so a change in the pipeline's logging
shows up here as a failure rather than being absorbed.
"""

from __future__ import annotations

import pytest

from closo.narration import MAX_ARG_CHARS, narrate

RUN = "run_x"


def event(layer: str, ref: str, kind: str, **payload) -> dict:
    return {"layer": layer, "record_ref": ref, "event_type": kind, "payload": payload}


def layer1_events(matched: int = 1, exceptions: int = 1) -> list[dict]:
    return [
        event("layer1", "batch", "layer1_started", bank_txns=47, settlements=46),
        event("layer1", "bt_1", "matched", settlement_id="s1", pass_used="A_utr_exact"),
        event("layer1", "bt_9", "exception", reason="duplicate_utr",
              detail="UTR matches 2 credits", exception_id="EX-001"),
        event("layer1", "batch", "layer1_finished", matched=matched,
              exceptions=exceptions, match_rate=0.83),
    ]


def investigation(confidence: str = "resolved", passed: bool = True, **verified) -> list[dict]:
    verdict = {
        "exception_id": "EX-001",
        "hypothesis": "settlement setl_1 paid across two credits",
        "confidence": confidence,
        "evidence": [],
        "hypotheses_rejected": [{"hypothesis": "a refund", "reason": "none recorded"}],
    }
    payload = {"passed": passed, "rejection_reason": None,
               "effective_confidence": confidence, "schedule_anomaly": None, **verified}
    return [
        event("layer2", "EX-001", "tool_call", tool="get_settlement",
              args={"settlement_id": "setl_1"}, call_number=1),
        event("layer2", "bt_9", "verdict_recorded", exception_id="EX-001",
              verdict=verdict),
        event("layer3", "bt_9", "verified", exception_id="EX-001", **payload),
        event("layer3", "bt_9", "verification_recorded", exception_id="EX-001",
              result={"passed": passed, "checks": [
                  {"check": "existence", "passed": True, "detail": "all exist"},
                  {"check": "arithmetic", "passed": passed, "detail": "recomputed"},
              ]}),
    ]


# --------------------------------------------------------------------------
# Layer 1
# --------------------------------------------------------------------------


def test_layer_1_counts_come_from_the_log():
    story = narrate(layer1_events(matched=39, exceptions=10))
    assert story.layer1.total_bank_txns == 47
    assert story.layer1.matched == 39
    assert story.layer1.exceptions == 10
    assert story.layer1.match_rate == pytest.approx(0.83)


def test_matches_are_counted_per_pass() -> None:
    """The Live-run screen shows the cascade's shape, not just its total -
    35 on UTR and 2 on tolerance is a different story from 37 somehow."""
    events = layer1_events() + [
        event("layer1", "bt_2", "matched", pass_used="C_netting_recompute"),
        event("layer1", "bt_3", "matched", pass_used="C_netting_recompute"),
    ]
    story = narrate(events)
    assert story.layer1.passes == {"A_utr_exact": 1, "C_netting_recompute": 2}


def test_an_exception_carries_the_reason_layer_1_gave() -> None:
    story = narrate(layer1_events())
    assert story.exceptions[0].reason == "duplicate_utr"
    assert story.exceptions[0].record_ref == "bt_9"


# --------------------------------------------------------------------------
# The four endings
# --------------------------------------------------------------------------


def test_a_verified_verdict_reads_as_verified() -> None:
    story = narrate(layer1_events() + investigation())
    told = story.exceptions[0]
    assert told.verified is True
    assert told.outcome_label == "verified - resolved"


def test_a_probable_verdict_says_it_needs_sign_off() -> None:
    """8.1's case. "Resolved" and "resolved, pending a human" must not read
    the same on a screen someone is deciding from."""
    story = narrate(layer1_events() + investigation(
        confidence="resolved", effective_confidence="probable",
        schedule_anomaly="cited v1 but v2 was active on 2026-03-18",
    ))
    told = story.exceptions[0]
    assert told.outcome_label == "verified - probable, needs human sign-off"
    assert "v1" in told.schedule_anomaly and "v2" in told.schedule_anomaly


def test_a_rejected_verdict_names_the_check_that_failed() -> None:
    """10.5: "agent proposed, verifier rejected" is the strongest thing in
    the demo, and it is worth nothing if the screen cannot say why."""
    story = narrate(layer1_events() + investigation(
        passed=False, rejection_reason="phantom_reference",
        effective_confidence="unresolvable",
    ))
    told = story.exceptions[0]
    assert told.verified is False
    assert told.outcome_label == "agent proposed, verifier rejected: phantom_reference"


def test_an_unanswered_exception_is_not_reported_as_rejected() -> None:
    """The distinction this narrator exists to keep. An `unresolvable`
    verdict claimed nothing, so the verifier neither passed nor rejected
    it - and marking it rejected would credit the verifier with catching
    something nobody proposed, against the one outcome the project argues
    is honest."""
    story = narrate(layer1_events() + investigation(
        confidence="unresolvable", passed=False,
        effective_confidence="unresolvable",
    ))
    told = story.exceptions[0]
    assert told.verified is None
    assert told.outcome_label == "agent could not resolve - escalated"

    stamp = [step for step in told.steps if step.kind == "verification"][0]
    assert stamp.ok is None, "no ✓ and no ✗ against a verdict that claimed nothing"
    assert "nothing claimed" in stamp.label


def test_a_skipped_exception_says_why_it_was_skipped() -> None:
    """A split payout's second leg. Silence here reads as an exception the
    run forgot about."""
    events = layer1_events() + [
        event("layer2", "bt_9", "skipped_already_resolved", exception_id="EX-001"),
    ]
    told = narrate(events).exceptions[0]
    assert told.skipped is True
    assert "earlier verdict" in told.outcome_label
    assert told.investigated is False


def test_an_exception_nobody_investigated_says_so() -> None:
    """A Layer-1-only run. "Not investigated" is a different claim from
    "investigated and unresolved", and only one of them is true."""
    told = narrate(layer1_events()).exceptions[0]
    assert told.outcome_label == "not investigated"
    assert told.steps == []


# --------------------------------------------------------------------------
# Ordering and shape
# --------------------------------------------------------------------------


def test_the_verifier_stamp_comes_after_the_verdict() -> None:
    """10 makes the verifier a visibly separate, later step. If the story
    interleaved them the screen could not pace them apart however long it
    slept."""
    told = narrate(layer1_events() + investigation()).exceptions[0]
    kinds = [step.kind for step in told.steps]
    assert kinds.index("verification") > kinds.index("verdict")
    assert kinds.index("verdict") > kinds.index("tool_call")


def test_events_keep_the_order_the_log_returned_them_in() -> None:
    """The log is ordered by event id rather than timestamp because several
    events share a millisecond. A re-ordered story is a different story."""
    events = layer1_events() + [
        event("layer2", "EX-001", "tool_call", tool="first", args={}),
        event("layer2", "EX-001", "tool_call", tool="second", args={}),
        event("layer2", "EX-001", "tool_call", tool="third", args={}),
    ]
    told = narrate(events).exceptions[0]
    assert [step.detail["tool"] for step in told.tool_calls] == [
        "first", "second", "third"
    ]


def test_both_logging_conventions_reach_the_same_exception() -> None:
    """The investigator logs against the exception id; the pipeline logs
    against the bank credit and names the exception in the payload. Both
    are correct for their own purpose, and a narrator that understood only
    one would drop half of every story."""
    events = layer1_events() + [
        event("layer2", "EX-001", "tool_call", tool="by_exception_ref", args={}),
        event("layer2", "bt_9", "verdict_recorded", exception_id="EX-001",
              verdict={"hypothesis": "h", "confidence": "unresolvable"}),
    ]
    story = narrate(events)
    assert len(story.exceptions) == 1, "the two conventions must not split the story"
    assert len(story.exceptions[0].steps) == 2


def test_the_verifier_checklist_is_attached_to_its_own_step() -> None:
    """The drill-down shows per-check ✓/✗ from the same object the
    Live-run block renders, so the two screens cannot disagree."""
    told = narrate(layer1_events() + investigation()).exceptions[0]
    stamp = [step for step in told.steps if step.kind == "verification"][0]
    assert len(stamp.detail["result"]["checks"]) == 2


def test_a_tool_call_is_neither_good_nor_bad_news() -> None:
    """Marking tool calls ✓ would have the screen inventing a conclusion
    the run never reached."""
    told = narrate(layer1_events() + investigation()).exceptions[0]
    assert all(step.ok is None for step in told.tool_calls)


# --------------------------------------------------------------------------
# Degenerate input
# --------------------------------------------------------------------------


def test_no_events_is_an_empty_story_not_a_crash() -> None:
    story = narrate([])
    assert story.exceptions == []
    assert story.layer1.total_bank_txns == 0


def test_an_unknown_event_type_is_ignored() -> None:
    """The log is append-only, so a run recorded by a later version will
    carry events this code has never heard of. Ignoring them keeps an old
    run replayable; raising would make the audit log a liability."""
    events = layer1_events() + [
        event("layer2", "EX-001", "invented_in_stage_9", exception_id="EX-001"),
    ]
    told = narrate(events).exceptions[0]
    assert told.steps == []


def test_an_event_belonging_to_no_exception_is_skipped() -> None:
    events = layer1_events() + [event("layer2", "batch", "tool_call", tool="x")]
    assert len(narrate(events).exceptions) == 1


def test_a_long_tool_argument_is_trimmed() -> None:
    """A pasted narration would otherwise push the tool name off the line."""
    events = layer1_events() + [
        event("layer2", "EX-001", "tool_call", tool="get_bank_txn",
              args={"narration": "N" * 400}),
    ]
    label = narrate(events).exceptions[0].tool_calls[0].label
    assert len(label) < MAX_ARG_CHARS + 40
    assert label.startswith("get_bank_txn(narration=")


def test_a_verdict_with_no_hypothesis_still_renders() -> None:
    events = layer1_events() + [
        event("layer2", "bt_9", "verdict_recorded", exception_id="EX-001",
              verdict={"confidence": "unresolvable"}),
    ]
    told = narrate(events).exceptions[0]
    assert told.steps[0].label == "no hypothesis given"


def test_a_quota_wall_is_narrated_as_a_reason_not_a_verdict() -> None:
    """Half a batch with no verdict needs to say *why* on the screen, or
    it reads as an investigation that concluded nothing."""
    events = layer1_events() + [
        event("layer2", "bt_9", "quota_exhausted", exception_id="EX-001"),
    ]
    told = narrate(events).exceptions[0]
    assert told.steps[0].kind == "note"
    assert "quota" in told.steps[0].label
    assert told.steps[0].ok is False


def test_a_pipeline_refusal_is_narrated() -> None:
    """A verdict the verifier passed and the orchestrator refused - the
    double-count guards. Invisible on screen otherwise."""
    events = layer1_events() + [
        event("layer3", "bt_9", "verdict_rejected_by_pipeline", exception_id="EX-001",
              detail="credit(s) already resolved elsewhere: ['bt_1']"),
    ]
    told = narrate(events).exceptions[0]
    assert "already resolved elsewhere" in told.steps[0].label


# --------------------------------------------------------------------------
# Against a real run
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_story():
    """Narrated from an actual cached run, not hand-built events.

    The tests above pin the meaning of each ending; this pins that the
    pipeline still emits what the narrator reads. They fail for different
    reasons, which is why both exist.
    """
    from closo.audit import AuditLog
    from closo.config import DEMO_DIR, DEMO_SEED
    from closo.dataset_io import load_batch
    from closo.layer2_investigator import Investigator
    from closo.pipeline import run
    from closo.response_cache import demo_client
    from closo.tools import ToolBox

    batch = load_batch(DEMO_DIR)
    client = demo_client()
    assert client is not None, "the committed response cache is missing"
    with AuditLog(":memory:") as log:
        run(batch, seed=DEMO_SEED, audit=log, run_id=RUN,
            investigator=Investigator(ToolBox(batch), client))
        return narrate(log.read_events(RUN))


def test_a_real_run_narrates_every_exception(real_story) -> None:
    assert len(real_story.exceptions) == 10
    assert real_story.layer1.matched == 39


def test_a_real_run_shows_its_evidence(real_story) -> None:
    """Every investigated exception must show the tool calls behind its
    verdict. An empty trail is what a broken join between the two logging
    conventions would look like."""
    for told in real_story.investigated:
        assert told.tool_calls, f"{told.exception_id} has no evidence to show"


def test_a_real_run_ends_every_investigation_with_a_verifier_step(real_story) -> None:
    for told in real_story.investigated:
        assert told.steps[-1].kind == "verification", (
            f"{told.exception_id} does not end on the verifier"
        )


def test_a_real_run_produces_the_sign_off_case_the_demo_shows(real_story) -> None:
    signoff = [
        told for told in real_story.exceptions
        if told.outcome_label.endswith("needs human sign-off")
    ]
    assert len(signoff) == 2
    assert all("was active on" in told.schedule_anomaly for told in signoff)


def test_a_real_run_skips_the_split_settlements_second_leg(real_story) -> None:
    skipped = [told for told in real_story.exceptions if told.skipped]
    assert len(skipped) == 1
    assert skipped[0].tool_calls == []


def test_a_real_run_marks_no_honest_escalation_as_a_rejection(real_story) -> None:
    """E9 and E10 are unresolvable by design. None of them may carry a ✗
    from the verifier, because none of them proposed anything to reject."""
    for told in real_story.exceptions:
        if told.confidence == "unresolvable":
            assert told.verified is None
            assert told.rejection_reason is None
