"""Tests for the Layer 1 cascade (CLAUDE.md 12.2).

The demo-set numbers matter, but the crafted fixtures matter more. A
matcher that scores 83 percent on one dataset has proved very little;
what needs proving is that it declines every opportunity to guess, and
guessing only shows up at boundaries you build on purpose.

Most tests here construct a two-record world so the assertion is about
one behaviour and nothing else.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from closo.config import PASS_C_TOLERANCE, money, normalize_utr
from closo.generator import generate
from closo.layer1_matcher import Layer1Matcher, run_layer1
from closo.schemas import BankTxn, Payment, Settlement
from closo.taxonomy import GeneratedBatch, settlement_math

REPO_ROOT = Path(__file__).resolve().parent.parent

# A Monday, well after the fee cutover, so the active schedule is v2.
MONDAY = date(2026, 3, 9)
FRIDAY = date(2026, 3, 6)
UTR_A = "SBIN0123456789AB"
UTR_B = "HDFC9876543210ZZ"


# --------------------------------------------------------------------------
# Fixture builders
# --------------------------------------------------------------------------


def a_payment(pid: str, gross: str, method: str = "card") -> Payment:
    return Payment(
        payment_id=pid, order_id=f"ord_{pid}", amount_gross=gross,
        method=method, captured_at=MONDAY - timedelta(days=3),
    )


def a_settlement(
    sid: str, payments: list[Payment], utr: str, settled_at: date = MONDAY,
    net_override: str | None = None,
) -> Settlement:
    """A settlement whose recorded figures are internally consistent.

    ``net_override`` deliberately breaks that consistency, which is how the
    fixtures for E4-shaped cases are built.
    """
    from closo.config import active_schedule

    schedule = active_schedule(settled_at)
    gross, mdr, gst, net = settlement_math(payments, schedule)
    return Settlement(
        settlement_id=sid, utr=utr, settled_at=settled_at,
        payment_ids=[p.payment_id for p in payments],
        amount_gross=gross, fee_mdr=mdr, fee_gst=gst,
        amount_settled=net_override if net_override is not None else net,
        fee_schedule=schedule.name,
    )


def a_credit(
    bid: str, amount: str, utr: str | None = UTR_A,
    value_date: date = MONDAY, narration: str | None = None,
) -> BankTxn:
    text = narration if narration is not None else (
        f"NEFT-RAZORPAYSOFTWARE-{utr}-SETTLEMENT" if utr else "NEFT-RAZORPAY-CR"
    )
    return BankTxn(
        bank_txn_id=bid, txn_date=value_date, value_date=value_date,
        narration=text, utr=None, credit_amount=amount,
    )


def a_batch(
    payments: list[Payment], settlements: list[Settlement], credits: list[BankTxn]
) -> GeneratedBatch:
    batch = GeneratedBatch()
    batch.payments = payments
    batch.settlements = settlements
    batch.bank_txns = credits
    return batch


def reasons(result) -> set[str]:
    return {e.reason for e in result.exceptions}


# --------------------------------------------------------------------------
# Demo set: the headline numbers
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def demo_batch() -> GeneratedBatch:
    return generate(42)


@pytest.fixture(scope="session")
def demo_result(demo_batch: GeneratedBatch):
    return run_layer1(demo_batch)


def test_auto_match_rate_meets_the_target(demo_result) -> None:
    """Section 6 sets the bar at 80 percent."""
    assert demo_result.match_rate >= 0.80, f"{demo_result.match_rate:.1%}"


def test_zero_false_matches(demo_batch: GeneratedBatch, demo_result) -> None:
    """The number that actually matters. A high match rate with one wrong
    match is worse than a lower rate with none, because the wrong one
    reconciles and nobody looks at it again."""
    wrong = []
    for match in demo_result.matches:
        truth = demo_batch.ground_truth[match.bank_txn_id]
        if sorted(truth["source_payment_ids"]) != sorted(match.payment_ids):
            wrong.append((match.bank_txn_id, match.pass_used))
    assert wrong == []


def test_every_credit_is_matched_or_excepted_exactly_once(
    demo_batch: GeneratedBatch, demo_result
) -> None:
    """No credit may be silently dropped, and none may appear twice."""
    matched = [m.bank_txn_id for m in demo_result.matches]
    excepted = [e.bank_txn_id for e in demo_result.exceptions if e.bank_txn_id]
    assert len(matched) + len(excepted) == demo_result.total_bank_txns
    assert not (set(matched) & set(excepted)), "a credit was both matched and excepted"
    assert len(matched) == len(set(matched)), "duplicate match"


def test_no_settlement_is_matched_twice(demo_result) -> None:
    """Exclusivity. One settlement paying two credits would double-count
    money on the scorecard."""
    ids = [m.settlement_id for m in demo_result.matches]
    assert len(ids) == len(set(ids))


def test_designed_unresolvables_are_never_matched(
    demo_batch: GeneratedBatch, demo_result
) -> None:
    """Resolving an E9 or E10 is a critical bug, not a good run (5.2)."""
    for match in demo_result.matches:
        cls = demo_batch.ground_truth[match.bank_txn_id]["error_class"]
        assert cls not in {"E9", "E10"}, f"{match.bank_txn_id} is {cls}"


def test_e9_settlements_reach_the_exception_queue(
    demo_batch: GeneratedBatch, demo_result
) -> None:
    """Escalation is success, not failure (11.8)."""
    escalated = {
        e.settlement_id
        for e in demo_result.exceptions
        if e.settlement_id and not e.bank_txn_id
    }
    assert escalated == set(demo_batch.missing_settlements)


def test_all_three_passes_contribute(demo_result) -> None:
    """If a pass never fires on the demo set it is untested by it, and its
    behaviour on stage would be a first run."""
    used = {m.pass_used for m in demo_result.matches}
    assert used == {"A_utr_exact", "B_amount_date_window", "C_netting_recompute"}


def test_layer1_is_deterministic(demo_batch: GeneratedBatch) -> None:
    first = run_layer1(demo_batch)
    second = run_layer1(demo_batch)
    assert [m.model_dump() for m in first.matches] == [
        m.model_dump() for m in second.matches
    ]
    assert [e.model_dump() for e in first.exceptions] == [
        e.model_dump() for e in second.exceptions
    ]


# --------------------------------------------------------------------------
# Pass A: UTR normalization and strictness
# --------------------------------------------------------------------------


def test_pass_a_matches_through_ugly_narration() -> None:
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    credit = a_credit(
        "bt_1", str(settlement.amount_settled),
        narration=f"neft/razorpay software pvt/{UTR_A.lower()}/cr",
    )
    result = run_layer1(a_batch([payment], [settlement], [credit]))
    assert len(result.matches) == 1
    assert result.matches[0].pass_used == "A_utr_exact"


def test_truncated_utr_does_not_prefix_match() -> None:
    """The single most dangerous failure mode in Pass A. Prefix matching a
    shortened UTR onto a real one produces a confident wrong match, so a
    truncated reference must fall through instead."""
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    credit = a_credit(
        "bt_1", str(settlement.amount_settled),
        narration=f"NEFT-RAZORPAY-{UTR_A[:10]}-SETTLEMENT",
    )
    result = run_layer1(a_batch([payment], [settlement], [credit]))
    assert all(m.pass_used != "A_utr_exact" for m in result.matches)


def test_pass_a_rejects_a_one_paisa_disagreement() -> None:
    """Section 12.2: a matching UTR with amounts off by 0.01 is not a Pass
    A match. It falls through, where Pass C can decide whether the gap has
    an explanation."""
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    off_by_a_paisa = money(settlement.amount_settled + Decimal("0.01"))
    credit = a_credit("bt_1", str(off_by_a_paisa))

    result = run_layer1(a_batch([payment], [settlement], [credit]))
    assert all(m.pass_used != "A_utr_exact" for m in result.matches)


def test_pass_a_falls_through_to_pass_c_on_small_disagreement() -> None:
    """The fall-through has to actually go somewhere useful."""
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    credit = a_credit("bt_1", str(money(settlement.amount_settled + Decimal("0.01"))))

    result = run_layer1(a_batch([payment], [settlement], [credit]))
    assert len(result.matches) == 1
    assert result.matches[0].pass_used == "C_netting_recompute"
    assert result.matches[0].tolerance_applied == Decimal("0.01")


def test_duplicate_utr_sends_both_credits_to_exceptions() -> None:
    """E6. A UTR join yielding two candidates has no correct answer, and
    choosing one produces a match that reconciles and is wrong."""
    first = a_payment("pay_1", "5000.00")
    second = a_payment("pay_2", "7000.00")
    settlement_a = a_settlement("setl_1", [first], UTR_A)
    settlement_b = a_settlement("setl_2", [second], UTR_A)
    credits = [
        a_credit("bt_1", str(settlement_a.amount_settled)),
        a_credit("bt_2", str(settlement_b.amount_settled)),
    ]

    result = run_layer1(a_batch([first, second], [settlement_a, settlement_b], credits))
    assert result.matches == []
    assert "duplicate_utr" in reasons(result)
    assert {e.bank_txn_id for e in result.exceptions if e.bank_txn_id} == {"bt_1", "bt_2"}


def test_ambiguous_tie_is_recorded_in_the_audit_log() -> None:
    """Section 12.2 asks for the reason to be recorded, not just the
    refusal - the escalation screen has to be able to explain itself."""
    first = a_payment("pay_1", "5000.00")
    second = a_payment("pay_2", "7000.00")
    settlements = [
        a_settlement("setl_1", [first], UTR_A),
        a_settlement("setl_2", [second], UTR_A),
    ]
    credits = [a_credit("bt_1", str(settlements[0].amount_settled))]

    result = run_layer1(a_batch([first, second], settlements, credits))
    kinds = {e.event_type for e in result.events}
    assert "exception" in kinds


def test_settlement_consumed_by_pass_a_is_invisible_later() -> None:
    """Exclusivity across passes. A settlement Pass A took must not be
    available to B or C, or one settlement pays two credits."""
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    exact = str(settlement.amount_settled)
    credits = [
        a_credit("bt_1", exact),                       # matches on UTR
        a_credit("bt_2", exact, utr=None),             # same amount, no UTR
    ]

    result = run_layer1(a_batch([payment], [settlement], credits))
    assert len(result.matches) == 1
    assert result.matches[0].bank_txn_id == "bt_1"
    assert {e.bank_txn_id for e in result.exceptions if e.bank_txn_id} == {"bt_2"}


# --------------------------------------------------------------------------
# Pass B: the date window
# --------------------------------------------------------------------------


def _window_case(value_date: date, settled_at: date = MONDAY):
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A, settled_at=settled_at)
    credit = a_credit(
        "bt_1", str(settlement.amount_settled), utr=None, value_date=value_date
    )
    return run_layer1(a_batch([payment], [settlement], [credit]))


def test_credit_on_the_settlement_date_matches() -> None:
    result = _window_case(MONDAY)
    assert result.matches and result.matches[0].pass_used == "B_amount_date_window"


def test_credit_on_the_third_business_day_matches() -> None:
    """The inclusive edge of the window."""
    result = _window_case(date(2026, 3, 12))  # Mon + 3 business days = Thu
    assert result.matches and result.matches[0].pass_used == "B_amount_date_window"


def test_credit_on_the_fourth_business_day_does_not_match() -> None:
    result = _window_case(date(2026, 3, 13))  # Friday, one too far
    assert all(m.pass_used != "B_amount_date_window" for m in result.matches)


def test_window_skips_the_weekend_for_a_friday_settlement() -> None:
    """The case the business-day arithmetic exists for. A naive three
    calendar days would reject this and show up as a mysteriously low match
    rate rather than as an error."""
    result = _window_case(date(2026, 3, 11), settled_at=FRIDAY)  # Fri + 3bd = Wed
    assert result.matches and result.matches[0].pass_used == "B_amount_date_window"


def test_credit_before_the_settlement_date_does_not_match() -> None:
    """Money cannot land before it was sent."""
    result = _window_case(date(2026, 3, 5), settled_at=MONDAY)
    assert all(m.pass_used != "B_amount_date_window" for m in result.matches)


def test_two_settlements_same_amount_overlapping_windows_both_except() -> None:
    """The test this suite exists to have. Guessing here is the bug."""
    first = a_payment("pay_1", "5000.00")
    second = a_payment("pay_2", "5000.00")
    # Friday and the following Monday: a credit dated Monday sits inside
    # both T+3 windows, so the amount alone cannot say which settlement it
    # belongs to.
    settlements = [
        a_settlement("setl_1", [first], UTR_A, settled_at=FRIDAY),
        a_settlement("setl_2", [second], UTR_B, settled_at=MONDAY),
    ]
    assert settlements[0].amount_settled == settlements[1].amount_settled
    credit = a_credit("bt_1", str(settlements[0].amount_settled), utr=None,
                      value_date=MONDAY)

    result = run_layer1(a_batch([first, second], settlements, [credit]))
    assert result.matches == []
    assert "ambiguous_tie" in reasons(result)


def test_one_settlement_claimed_by_two_credits_is_ambiguous() -> None:
    """The direction that is easy to miss. Checking uniqueness only per
    credit would let this settlement be matched twice."""
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    exact = str(settlement.amount_settled)
    credits = [
        a_credit("bt_1", exact, utr=None),
        a_credit("bt_2", exact, utr=None, value_date=date(2026, 3, 10)),
    ]

    result = run_layer1(a_batch([payment], [settlement], credits))
    assert result.matches == []
    assert "ambiguous_tie" in reasons(result)


# --------------------------------------------------------------------------
# Pass C: the tolerance boundary
# --------------------------------------------------------------------------


def _tolerance_case(drift: str):
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    credit = a_credit(
        "bt_1", str(money(settlement.amount_settled + Decimal(drift))), utr=None
    )
    return run_layer1(a_batch([payment], [settlement], [credit]))


def test_drift_of_exactly_two_rupees_matches() -> None:
    """Inclusive. Getting this boundary wrong silently drops every record
    that lands on it."""
    result = _tolerance_case("2.00")
    assert result.matches
    assert result.matches[0].pass_used == "C_netting_recompute"
    assert result.matches[0].tolerance_applied == PASS_C_TOLERANCE


def test_drift_of_two_rupees_and_one_paisa_does_not_match() -> None:
    result = _tolerance_case("2.01")
    assert result.matches == []
    assert result.exceptions


def test_negative_drift_is_also_within_tolerance() -> None:
    """The bank credited slightly less. Comparing a signed difference
    instead of an absolute one would match one direction only."""
    result = _tolerance_case("-2.00")
    assert result.matches and result.matches[0].pass_used == "C_netting_recompute"


def test_tolerance_applied_is_logged_on_every_match(demo_result) -> None:
    """Section 6 asks for the tolerance used on every match, so the audit
    log can answer 'was this exact?' without re-deriving it."""
    for match in demo_result.matches:
        assert match.tolerance_applied is not None
        if match.pass_used == "A_utr_exact":
            assert match.tolerance_applied == Decimal("0.00")


def test_pass_c_uses_the_active_schedule_not_the_recorded_one() -> None:
    """E4 in miniature. If Pass C trusted the schedule written on the
    settlement it would happily agree with a payout that ran on a stale
    one, and the class would resolve here instead of reaching Layer 2."""
    payment = a_payment("pay_1", "50000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)

    from closo.config import FEE_SCHEDULES

    _, _, _, stale_net = settlement_math([payment], FEE_SCHEDULES["v1"])
    credit = a_credit("bt_1", str(stale_net))

    result = run_layer1(a_batch([payment], [settlement], [credit]))
    assert result.matches == []


def test_settlement_with_no_known_payments_is_skipped_not_crashed() -> None:
    """Degenerate input must not take the batch down."""
    settlement = a_settlement("setl_1", [a_payment("pay_missing", "5000.00")], UTR_A)
    credit = a_credit("bt_1", "100.00", utr=None)
    result = run_layer1(a_batch([], [settlement], [credit]))
    assert result.matches == []
    assert result.exceptions


# --------------------------------------------------------------------------
# Degenerate input
# --------------------------------------------------------------------------


def test_empty_batch_runs_clean() -> None:
    result = run_layer1(a_batch([], [], []))
    assert result.matches == []
    assert result.exceptions == []
    assert result.match_rate == 0.0


def test_empty_bank_statement_escalates_every_settlement() -> None:
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    result = run_layer1(a_batch([payment], [settlement], []))
    assert result.matches == []
    assert {e.reason for e in result.exceptions} == {"no_counterpart"}


def test_credits_with_no_settlements_at_all() -> None:
    credit = a_credit("bt_1", "5000.00")
    result = run_layer1(a_batch([], [], [credit]))
    assert result.matches == []
    assert reasons(result) == {"no_utr_match"}


def test_single_record_batch_matches() -> None:
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    credit = a_credit("bt_1", str(settlement.amount_settled))
    result = run_layer1(a_batch([payment], [settlement], [credit]))
    assert len(result.matches) == 1
    assert result.match_rate == 1.0


def test_credit_with_unparseable_narration_and_no_amount_match() -> None:
    """E8 that also fails Pass B. Distinguished from a UTR that parsed but
    matched nothing, because the escalation notes differ."""
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    credit = a_credit("bt_1", "1.00", utr=None, narration="NEFT-RAZORPAY-SHORT-CR")
    result = run_layer1(a_batch([payment], [settlement], [credit]))
    assert reasons(result) >= {"no_utr_in_narration"}


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------


def test_every_match_and_exception_emits_an_event(demo_result) -> None:
    """Stage 4 persists these; the replay demo reads them back."""
    matched = {
        e.record_ref for e in demo_result.events if e.event_type == "matched"
    }
    assert matched == {m.bank_txn_id for m in demo_result.matches}
    excepted = {
        e.record_ref for e in demo_result.events if e.event_type == "exception"
    }
    assert len(excepted) == len(demo_result.exceptions)


def test_run_is_bracketed_by_start_and_finish_events(demo_result) -> None:
    kinds = [e.event_type for e in demo_result.events]
    assert kinds[0] == "layer1_started"
    assert kinds[-1] == "layer1_finished"


def test_pass_a_amount_mismatch_is_logged_with_the_difference() -> None:
    """The number a human needs to see first when this happens."""
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    credit = a_credit("bt_1", str(money(settlement.amount_settled + Decimal("50.00"))))

    result = run_layer1(a_batch([payment], [settlement], [credit]))
    logged = [e for e in result.events if e.event_type == "pass_a_amount_mismatch"]
    assert logged
    assert logged[0].payload["difference"] == "50.00"


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


def test_matcher_imports_no_llm_sdk() -> None:
    """Section 11.3, checked in a clean interpreter so an unrelated earlier
    test cannot decide the result."""
    code = (
        "import sys, closo.layer1_matcher; "
        "print(any(m.startswith('google.genai') or m == 'closo.llm_client' "
        "for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        check=True, cwd=str(REPO_ROOT),
    )
    assert out.stdout.strip() == "False"


def test_matcher_derives_utrs_from_narration_only() -> None:
    """A pre-parsed utr column would let Pass A skip normalization and E8
    would stop testing anything (6)."""
    payment = a_payment("pay_1", "5000.00")
    settlement = a_settlement("setl_1", [payment], UTR_A)
    credit = a_credit("bt_1", str(settlement.amount_settled))
    assert credit.utr is None

    matcher = Layer1Matcher(a_batch([payment], [settlement], [credit]))
    assert matcher.parsed_utr["bt_1"] == normalize_utr(credit.narration) == UTR_A
