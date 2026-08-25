"""Tests for Layer 2 (TEST_PLAN 12.5), with a mocked client throughout.

No test here touches the network or spends quota. That is not only about
cost: a suite that depends on a live model is a suite whose failures are
ambiguous, and the behaviour worth pinning down is what the *loop* does
when the model misbehaves - which is far easier to arrange with a script
than to provoke for real.

The recurring assertion is that a bad exception produces `unresolvable`
rather than an exception. A run that dies at record 12 of 20 leaves a
scorecard that is not wrong so much as meaningless.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from closo.config import DEMO_DIR
from closo.dataset_io import load_batch
from closo.layer2_investigator import (
    RETRY_PROMPT,
    SYSTEM_PROMPT,
    Investigator,
)
from closo.llm_client import LLMResponse, MockLLMClient, QuotaExhausted, ToolCall
from closo.schemas import ExceptionItem
from closo.tool_schema import READ_TOOL_NAMES, SUBMIT_VERDICT
from closo.tools import ToolBox

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def batch():
    return load_batch(DEMO_DIR)


@pytest.fixture(scope="module")
def toolbox(batch):
    return ToolBox(batch)


@pytest.fixture(scope="module")
def real_ids(batch):
    """A real settlement and its credit, so verdicts can cite live records."""
    settlement = next(s for s in batch.settlements if len(s.payment_ids) > 1)
    credit = batch.bank_txns[0]
    return settlement, credit


def an_exception(bank_txn_id: str = "bt_0280") -> ExceptionItem:
    return ExceptionItem(
        exception_id="EX-001", bank_txn_id=bank_txn_id,
        reason="outside_tolerance", detail="no schedule reproduces the amount",
    )


def verdict_call(**overrides) -> ToolCall:
    args = {"hypothesis": "settlement", "confidence": "unresolvable"}
    args.update(overrides)
    return ToolCall(SUBMIT_VERDICT, args)


def resolved_call(settlement, credit, schedule: str = "v1") -> ToolCall:
    from closo.tools import ToolBox as _TB  # local: keeps the fixture lazy

    return ToolCall(
        SUBMIT_VERDICT,
        {
            "hypothesis": "payout under superseded schedule",
            "confidence": "resolved",
            "bank_txn_id": credit.bank_txn_id,
            "payment_ids": list(settlement.payment_ids),
            "fee_schedule": schedule,
            "arithmetic": {
                "gross": "1.00", "mdr": "1.00", "gst": "1.00", "net": "1.00"
            },
        },
    )


def investigator(toolbox, responses, **kwargs) -> Investigator:
    return Investigator(toolbox, MockLLMClient(responses), **kwargs)


# --------------------------------------------------------------------------
# The straightforward paths
# --------------------------------------------------------------------------


def test_a_submitted_verdict_is_returned(toolbox) -> None:
    agent = investigator(toolbox, [LLMResponse(tool_calls=[verdict_call()])])
    outcome = agent.investigate(an_exception())
    assert outcome.verdict.confidence == "unresolvable"
    assert outcome.verdict.exception_id == "EX-001"


def test_tool_results_are_fed_back_and_recorded_as_evidence(toolbox) -> None:
    """Evidence is what the drill-down screen shows, so it has to be the
    actual tool output rather than the model's account of it."""
    agent = investigator(
        toolbox,
        [
            LLMResponse(tool_calls=[ToolCall("get_fee_schedules", {})]),
            LLMResponse(tool_calls=[verdict_call()]),
        ],
    )
    outcome = agent.investigate(an_exception())
    assert outcome.tool_calls_used == 1
    assert outcome.verdict.evidence[0].tool == "get_fee_schedules"
    assert "cutover_date" in outcome.verdict.evidence[0].result_summary


def test_the_opening_brief_includes_the_actual_record(toolbox) -> None:
    """The model starts with the credit in front of it rather than having to
    spend a tool call fetching what the matcher already knew."""
    client = MockLLMClient([LLMResponse(tool_calls=[verdict_call()])])
    Investigator(toolbox, client).investigate(an_exception())
    brief = client.calls[0]["messages"][0]["content"]
    assert "bt_0280" in brief
    assert "outside_tolerance" in brief


def test_the_system_prompt_is_sent(toolbox) -> None:
    client = MockLLMClient([LLMResponse(tool_calls=[verdict_call()])])
    Investigator(toolbox, client).investigate(an_exception())
    assert client.calls[0]["system_prompt"] == SYSTEM_PROMPT


def test_unresolvable_is_reachable_without_a_proposed_match(toolbox) -> None:
    """Giving up must not require inventing a match to give up with."""
    agent = investigator(toolbox, [LLMResponse(tool_calls=[verdict_call()])])
    assert agent.investigate(an_exception()).verdict.proposed_match is None


# --------------------------------------------------------------------------
# Tool budget
# --------------------------------------------------------------------------


def test_tool_budget_is_capped(toolbox) -> None:
    """A model that never stops asking for data must be stopped."""
    endless = [
        LLMResponse(tool_calls=[ToolCall("get_fee_schedules", {})]) for _ in range(30)
    ]
    agent = investigator(toolbox, endless)
    outcome = agent.investigate(an_exception())
    assert outcome.tool_calls_used == 8
    assert outcome.verdict.confidence == "unresolvable"


def test_budget_exhaustion_is_recorded_in_the_audit_log(toolbox) -> None:
    endless = [
        LLMResponse(tool_calls=[ToolCall("get_fee_schedules", {})]) for _ in range(30)
    ]
    outcome = investigator(toolbox, endless).investigate(an_exception())
    assert any(e["event_type"] == "tool_budget_exhausted" for e in outcome.events)


def test_the_last_turn_forces_a_verdict(toolbox) -> None:
    """On the final turn the model is compelled to submit rather than asked
    to, so a hedging reply cannot waste the last of the budget."""
    client = MockLLMClient(
        [LLMResponse(tool_calls=[ToolCall("get_fee_schedules", {})]) for _ in range(8)]
        + [LLMResponse(tool_calls=[verdict_call()])]
    )
    Investigator(toolbox, client).investigate(an_exception())
    assert client.calls[-1]["force_tool"] == SUBMIT_VERDICT
    assert all(c["force_tool"] is None for c in client.calls[:-1])


def test_a_custom_budget_is_respected(toolbox) -> None:
    endless = [
        LLMResponse(tool_calls=[ToolCall("get_fee_schedules", {})]) for _ in range(10)
    ]
    outcome = investigator(toolbox, endless, max_tool_calls=3).investigate(
        an_exception()
    )
    assert outcome.tool_calls_used == 3


# --------------------------------------------------------------------------
# Timeout
# --------------------------------------------------------------------------


def test_a_slow_investigation_times_out(toolbox) -> None:
    """A stalled exception yields unresolvable rather than hanging the run.

    The timeout is passed explicitly rather than taken from config, so
    retuning the real one - as the first live run required - does not break
    a test that is about the mechanism, not the value.
    """
    ticks = iter([0.0, 1.0, 45.0, 46.0, 47.0])
    agent = Investigator(
        toolbox,
        MockLLMClient(
            [LLMResponse(tool_calls=[ToolCall("get_fee_schedules", {})])] * 5
        ),
        timeout_seconds=30,
        clock=lambda: next(ticks),
    )
    outcome = agent.investigate(an_exception())
    assert outcome.verdict.confidence == "unresolvable"
    assert "exceeded" in outcome.verdict.hypothesis


def test_a_timeout_is_recorded(toolbox) -> None:
    ticks = iter([0.0, 99.0, 100.0])
    agent = Investigator(
        toolbox, MockLLMClient([LLMResponse(tool_calls=[verdict_call()])]),
        timeout_seconds=30, clock=lambda: next(ticks),
    )
    outcome = agent.investigate(an_exception())
    assert any(e["event_type"] == "timeout" for e in outcome.events)


def test_the_timeout_allows_a_full_length_investigation(toolbox) -> None:
    """The bound that the first live run showed was contradictory.

    Eight tool calls means nine requests, and at the measured ~4s per
    request the original 30s fired before the tool budget could ever be
    spent - the timeout was silently capping work rather than catching a
    stall. Two limits that cannot both be reached are one limit and a bug.
    """
    from closo.config import EXCEPTION_TIMEOUT_SECONDS, MAX_TOOL_CALLS_PER_EXCEPTION

    observed_seconds_per_request = 4.0
    requests_for_a_full_investigation = MAX_TOOL_CALLS_PER_EXCEPTION + 1
    needed = requests_for_a_full_investigation * observed_seconds_per_request
    assert EXCEPTION_TIMEOUT_SECONDS > needed, (
        f"timeout {EXCEPTION_TIMEOUT_SECONDS}s cannot fit "
        f"{requests_for_a_full_investigation} requests at {observed_seconds_per_request}s"
    )


def test_one_slow_exception_does_not_kill_the_batch(toolbox) -> None:
    """The property the whole bounded design exists for."""
    class SlowThenFine:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, *args, **kwargs) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("model stalled")
            return LLMResponse(tool_calls=[verdict_call()])

    agent = Investigator(toolbox, SlowThenFine())
    items = [an_exception(), ExceptionItem(
        exception_id="EX-002", bank_txn_id="bt_0280", reason="no_utr_match"
    )]
    outcomes = agent.investigate_all(items)
    assert len(outcomes) == 2
    assert outcomes[0].verdict.confidence == "unresolvable"
    assert outcomes[1].verdict.confidence == "unresolvable"


# --------------------------------------------------------------------------
# Malformed output
# --------------------------------------------------------------------------


def test_prose_instead_of_a_verdict_gets_one_corrective_retry(toolbox) -> None:
    client = MockLLMClient(
        [
            LLMResponse(text="I think this is probably a refund, but I am unsure."),
            LLMResponse(tool_calls=[verdict_call()]),
        ]
    )
    outcome = Investigator(toolbox, client).investigate(an_exception())
    assert outcome.verdict.confidence == "unresolvable"
    assert any(e["event_type"] == "malformed_verdict_retry" for e in outcome.events)
    assert client.calls[-1]["messages"][-1]["content"] == RETRY_PROMPT


def test_prose_twice_ends_as_unresolvable(toolbox) -> None:
    """One retry, not a negotiation."""
    agent = investigator(
        toolbox, [LLMResponse(text="still unsure"), LLMResponse(text="sorry")]
    )
    outcome = agent.investigate(an_exception())
    assert outcome.verdict.confidence == "unresolvable"
    assert any(e["event_type"] == "malformed_verdict_final" for e in outcome.events)


def test_an_empty_turn_is_treated_as_malformed(toolbox) -> None:
    agent = investigator(toolbox, [LLMResponse(), LLMResponse()])
    assert agent.investigate(an_exception()).verdict.confidence == "unresolvable"


# --------------------------------------------------------------------------
# Verdict schema violations — rejected before the verifier
# --------------------------------------------------------------------------


def test_unknown_confidence_is_rejected(toolbox) -> None:
    """The verifier checks arithmetic against records; it is not there to
    repair input that never parsed."""
    agent = investigator(
        toolbox, [LLMResponse(tool_calls=[verdict_call(confidence="pretty sure")])]
    )
    outcome = agent.investigate(an_exception())
    assert outcome.verdict.confidence == "unresolvable"
    assert "unknown confidence" in outcome.verdict.hypothesis


def test_resolved_without_an_arithmetic_block_is_rejected(toolbox, real_ids) -> None:
    settlement, credit = real_ids
    call = resolved_call(settlement, credit)
    del call.args["arithmetic"]

    outcome = investigator(toolbox, [LLMResponse(tool_calls=[call])]).investigate(
        an_exception()
    )
    assert outcome.verdict.confidence == "unresolvable"
    assert "complete proposed match" in outcome.verdict.hypothesis
    assert "verifier" not in outcome.verdict.hypothesis, (
        "this module refused the verdict, not the verifier - and the "
        "escalation queue shows both, so the wording has to say which"
    )


def test_resolved_without_payment_ids_is_rejected(toolbox, real_ids) -> None:
    settlement, credit = real_ids
    call = resolved_call(settlement, credit)
    call.args["payment_ids"] = []

    outcome = investigator(toolbox, [LLMResponse(tool_calls=[call])]).investigate(
        an_exception()
    )
    assert outcome.verdict.confidence == "unresolvable"


def test_a_well_formed_resolved_verdict_survives(toolbox, real_ids) -> None:
    """The other half: valid input must reach the verifier intact, or the
    rejections above would be indistinguishable from rejecting everything."""
    settlement, credit = real_ids
    outcome = investigator(
        toolbox, [LLMResponse(tool_calls=[resolved_call(settlement, credit)])]
    ).investigate(an_exception())

    assert outcome.verdict.confidence == "resolved"
    assert outcome.verdict.proposed_match is not None
    assert outcome.verdict.proposed_match.payment_ids == list(settlement.payment_ids)


def test_rejected_hypotheses_are_preserved(toolbox) -> None:
    """The escalation screen is far more convincing showing what was ruled
    out than saying only that nothing worked."""
    call = verdict_call(
        hypotheses_rejected=[
            {"hypothesis": "partial refund", "reason": "no refunds on the payment"},
            {"hypothesis": "split payout", "reason": "no second credit that date"},
        ]
    )
    outcome = investigator(toolbox, [LLMResponse(tool_calls=[call])]).investigate(
        an_exception()
    )
    assert len(outcome.verdict.hypotheses_rejected) == 2
    assert outcome.verdict.hypotheses_rejected[0].reason.startswith("no refunds")


def test_malformed_rejected_hypotheses_do_not_crash_the_verdict(toolbox) -> None:
    """Model output is untrusted input, including the parts that are only
    there to be displayed."""
    call = verdict_call(hypotheses_rejected=["just a string", {"hypothesis": "x"}])
    outcome = investigator(toolbox, [LLMResponse(tool_calls=[call])]).investigate(
        an_exception()
    )
    assert outcome.verdict.confidence == "unresolvable"
    assert len(outcome.verdict.hypotheses_rejected) == 1


# --------------------------------------------------------------------------
# API errors and quota
# --------------------------------------------------------------------------


def test_a_transient_error_is_retried_once(toolbox) -> None:
    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, *args, **kwargs) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("503 Service Unavailable: model overloaded")
            return LLMResponse(tool_calls=[verdict_call()])

    client = Flaky()
    outcome = Investigator(toolbox, client).investigate(an_exception())
    assert client.calls == 2
    assert outcome.verdict.confidence == "unresolvable"
    assert any(e["event_type"] == "api_error_retry" for e in outcome.events)


def test_a_persistent_error_ends_as_unresolvable(toolbox) -> None:
    class AlwaysDown:
        def generate(self, *args, **kwargs) -> LLMResponse:
            raise RuntimeError("529 overloaded")

    outcome = Investigator(toolbox, AlwaysDown()).investigate(an_exception())
    assert outcome.verdict.confidence == "unresolvable"
    assert "API error" in outcome.verdict.hypothesis


def test_a_non_transient_error_is_not_retried(toolbox) -> None:
    """Retrying a malformed request just spends quota to fail twice."""
    class BadRequest:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, *args, **kwargs) -> LLMResponse:
            self.calls += 1
            raise ValueError("400 invalid argument: tool schema rejected")

    client = BadRequest()
    outcome = Investigator(toolbox, client).investigate(an_exception())
    assert client.calls == 1
    assert outcome.verdict.confidence == "unresolvable"


def test_quota_exhaustion_stops_the_batch_and_marks_the_rest(toolbox) -> None:
    """The wall must degrade a run honestly, not truncate it silently. Every
    remaining exception gets a verdict saying why (7.4)."""
    class Walled:
        def generate(self, *args, **kwargs) -> LLMResponse:
            raise QuotaExhausted("daily request budget of 500 is spent")

    items = [
        ExceptionItem(exception_id=f"EX-{n:03d}", bank_txn_id="bt_0280",
                      reason="no_utr_match")
        for n in range(1, 5)
    ]
    outcomes = Investigator(toolbox, Walled()).investigate_all(items)

    assert len(outcomes) == len(items)
    assert all(o.verdict.confidence == "unresolvable" for o in outcomes)
    assert all("quota" in o.verdict.hypothesis.lower() for o in outcomes)


def test_quota_exhaustion_stops_issuing_requests(toolbox) -> None:
    """Once the wall is hit, continuing would spend nothing but time."""
    class CountingWall:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, *args, **kwargs) -> LLMResponse:
            self.calls += 1
            raise QuotaExhausted("spent")

    client = CountingWall()
    items = [
        ExceptionItem(exception_id=f"EX-{n}", bank_txn_id="bt_0280",
                      reason="no_utr_match")
        for n in range(6)
    ]
    Investigator(toolbox, client).investigate_all(items)
    assert client.calls == 1


# --------------------------------------------------------------------------
# Tool dispatch
# --------------------------------------------------------------------------


def test_an_unknown_tool_name_is_reported_not_raised(toolbox) -> None:
    agent = investigator(
        toolbox,
        [
            LLMResponse(tool_calls=[ToolCall("get_the_answer", {})]),
            LLMResponse(tool_calls=[verdict_call()]),
        ],
    )
    outcome = agent.investigate(an_exception())
    assert "unknown_tool" in outcome.verdict.evidence[0].result_summary


def test_bad_tool_arguments_are_reported_not_raised(toolbox) -> None:
    """A wrong argument name must be recoverable - the model can read the
    error and try again, which it cannot do with a stack trace."""
    agent = investigator(
        toolbox,
        [
            LLMResponse(tool_calls=[ToolCall("get_payment", {"wrong_kwarg": "x"})]),
            LLMResponse(tool_calls=[verdict_call()]),
        ],
    )
    outcome = agent.investigate(an_exception())
    assert "bad_argument" in outcome.verdict.evidence[0].result_summary


def test_every_declared_read_tool_actually_exists(toolbox) -> None:
    """A declared tool with no implementation is a call that always fails,
    and the model would keep trying it."""
    for name in READ_TOOL_NAMES:
        assert callable(getattr(toolbox, name, None)), f"{name} is not implemented"


def test_submit_verdict_is_not_dispatched_to_the_toolbox() -> None:
    """It is the exit condition, not a tool - the toolbox must not gain a
    method by that name or the loop would never terminate."""
    assert SUBMIT_VERDICT not in READ_TOOL_NAMES
    assert not hasattr(ToolBox, SUBMIT_VERDICT)


# --------------------------------------------------------------------------
# Determinism and invariants
# --------------------------------------------------------------------------


def test_the_same_script_produces_the_same_verdict(toolbox) -> None:
    """Same inputs, same output. Without this the replay demo stops at
    Layer 1 (11.5)."""
    def run_once():
        return investigator(
            toolbox,
            [
                LLMResponse(tool_calls=[ToolCall("get_fee_schedules", {})]),
                LLMResponse(tool_calls=[verdict_call()]),
            ],
        ).investigate(an_exception())

    first, second = run_once(), run_once()
    assert first.verdict.model_dump() == second.verdict.model_dump()


def test_investigator_does_not_read_ground_truth(monkeypatch, toolbox) -> None:
    """The model must never reach the answers, however indirectly."""
    from closo.dataset_io import GROUND_TRUTH_FILENAME

    real_open = Path.open

    def guarded(self: Path, *args: object, **kwargs: object):
        if self.name == GROUND_TRUTH_FILENAME:
            raise AssertionError("investigator opened ground truth")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    agent = investigator(toolbox, [LLMResponse(tool_calls=[verdict_call()])])
    assert agent.investigate(an_exception()).verdict


def test_the_mocked_suite_never_imports_the_sdk() -> None:
    """These tests must stay free and offline. Importing the investigator
    must not pull in google.genai."""
    code = (
        "import sys, closo.layer2_investigator, closo.tool_schema; "
        "print(any(m.startswith('google.genai') for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        check=True, cwd=str(REPO_ROOT),
    )
    assert out.stdout.strip() == "False"
