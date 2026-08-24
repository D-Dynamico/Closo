"""Tests for the record, verdict and result models.

The point of these is not that pydantic works. It is that the two rules
the schemas exist to enforce actually hold at the boundary: money never
becomes a float, and there are exactly three terminal states.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from closo.schemas import (
    Arithmetic,
    BankTxn,
    Evidence,
    ExceptionItem,
    FinalStatus,
    MatchRecord,
    Order,
    Payment,
    ProposedMatch,
    RejectedHypothesis,
    Resolution,
    RunResult,
    Settlement,
    Verdict,
    VerifierResult,
)


def a_payment(**overrides: object) -> Payment:
    """A minimal valid payment, overridable per test."""
    base: dict[str, object] = {
        "payment_id": "pay_001",
        "order_id": "ord_001",
        "amount_gross": "5000.00",
        "method": "card",
        "captured_at": date(2026, 3, 2),
    }
    base.update(overrides)
    return Payment(**base)  # type: ignore[arg-type]


def a_bank_txn(**overrides: object) -> BankTxn:
    base: dict[str, object] = {
        "bank_txn_id": "bt_001",
        "txn_date": date(2026, 3, 4),
        "value_date": date(2026, 3, 4),
        "narration": "NEFT-RAZORPAY-SETTLEMENT",
        "credit_amount": "4867.25",
    }
    base.update(overrides)
    return BankTxn(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Money discipline
# --------------------------------------------------------------------------


def test_string_amount_becomes_quantized_decimal() -> None:
    p = a_payment(amount_gross="5000")
    assert p.amount_gross == Decimal("5000.00")
    assert isinstance(p.amount_gross, Decimal)


def test_amount_is_quantized_on_the_way_in() -> None:
    """Coercion happens at the boundary, not on first use, so nothing
    downstream ever sees a three-decimal amount."""
    assert a_payment(amount_gross="99.999").amount_gross == Decimal("100.00")


def test_float_amount_is_rejected() -> None:
    """A float arriving from a careless CSV read must fail here rather than
    three layers deep where the origin is unrecoverable."""
    with pytest.raises(ValidationError, match="float is not allowed"):
        a_payment(amount_gross=5000.5)


def test_float_rejected_in_nested_arithmetic_block() -> None:
    """The arithmetic block inside a verdict is where a stray float would
    be hardest to spot, which is exactly why the rule rides on the type."""
    with pytest.raises(ValidationError, match="float is not allowed"):
        Arithmetic(gross=5000.0, mdr="112.50", gst="20.25", net="4867.25")


def test_garbage_amount_is_rejected() -> None:
    with pytest.raises(ValidationError):
        a_payment(amount_gross="not-a-number")


def test_money_serializes_as_string_not_float() -> None:
    """A JSON float on the way out would undo the whole Decimal discipline.
    Tools return amounts as strings for the same reason (7.1)."""
    payload = json.loads(a_bank_txn().model_dump_json())
    assert payload["credit_amount"] == "4867.25"
    assert isinstance(payload["credit_amount"], str)


def test_json_round_trip_is_lossless() -> None:
    original = a_bank_txn(credit_amount="123456.78")
    restored = BankTxn.model_validate_json(original.model_dump_json())
    assert restored.credit_amount == original.credit_amount


def test_nested_money_also_serializes_as_string() -> None:
    v = Verdict(
        exception_id="EX-1",
        hypothesis="h",
        confidence="resolved",
        proposed_match=ProposedMatch(
            bank_txn_id="bt_001",
            payment_ids=["pay_001"],
            fee_schedule="v2",
            arithmetic=Arithmetic(gross="5000", mdr="112.50", gst="20.25", net="4867.25"),
        ),
    )
    dumped = v.model_dump(mode="json")
    assert dumped["proposed_match"]["arithmetic"]["gross"] == "5000.00"


def test_optional_amount_stays_none() -> None:
    """E9 is a payment that never settled. None must survive coercion
    rather than becoming a helpful zero, which would look like a
    successful settlement of nothing."""
    p = a_payment()
    assert p.amount_settled is None
    assert p.model_dump()["amount_settled"] is None


def test_net_of_refund_subtracts_correctly() -> None:
    p = a_payment(amount_gross="5000.00", refund_amount="1250.50")
    assert p.net_of_refund == Decimal("3749.50")


def test_net_of_refund_with_no_refund_equals_gross() -> None:
    assert a_payment().net_of_refund == Decimal("5000.00")


# --------------------------------------------------------------------------
# Strictness
# --------------------------------------------------------------------------


def test_unknown_field_is_rejected() -> None:
    """extra='forbid' catches a renamed CSV column instead of silently
    dropping it and reconciling against a missing value."""
    with pytest.raises(ValidationError):
        a_payment(bogus_column="x")


def test_invalid_payment_method_rejected() -> None:
    with pytest.raises(ValidationError):
        a_payment(method="crypto")


def test_invalid_status_rejected() -> None:
    with pytest.raises(ValidationError):
        a_payment(status="pending")


def test_assignment_is_revalidated() -> None:
    """Mutating a model after construction must not bypass the money rule."""
    p = a_payment()
    with pytest.raises(ValidationError):
        p.amount_gross = 1.5  # type: ignore[assignment]


def test_proposed_match_requires_at_least_one_payment() -> None:
    """A match citing no payments is not a match. It would trivially pass a
    naive arithmetic check by summing to zero."""
    with pytest.raises(ValidationError):
        ProposedMatch(
            bank_txn_id="bt_001",
            payment_ids=[],
            fee_schedule="v1",
            arithmetic=Arithmetic(gross="0", mdr="0", gst="0", net="0"),
        )


def test_invalid_confidence_rejected() -> None:
    with pytest.raises(ValidationError):
        Verdict(exception_id="EX-1", hypothesis="h", confidence="pretty_sure")


def test_invalid_match_pass_rejected() -> None:
    with pytest.raises(ValidationError):
        MatchRecord(
            bank_txn_id="bt_001",
            settlement_id="setl_001",
            payment_ids=["pay_001"],
            pass_used="D_vibes",
        )


def test_invalid_rejection_reason_rejected() -> None:
    with pytest.raises(ValidationError):
        VerifierResult(exception_id="EX-1", passed=False, rejection_reason="dunno")


# --------------------------------------------------------------------------
# Terminal states
# --------------------------------------------------------------------------


def test_there_are_exactly_three_terminal_states() -> None:
    """Section 2 says there is no fourth. The temptation later will be to
    add something like NEEDS_REVIEW for probable verdicts; that case is a
    flag on the resolution, not a state, so this test guards the invariant
    that a rejected verdict is escalated and never resolved."""
    assert len(FinalStatus) == 3
    assert {s.value for s in FinalStatus} == {
        "AUTO_MATCHED",
        "AGENT_RESOLVED_VERIFIED",
        "ESCALATED",
    }


def test_probable_signoff_is_a_flag_not_a_fourth_state() -> None:
    """A probable verdict that passes verification lands in the sign-off
    sub-list while still carrying a real terminal status (8)."""
    r = Resolution(
        bank_txn_id="bt_001",
        final_status=FinalStatus.AGENT_RESOLVED_VERIFIED,
        needs_human_signoff=True,
    )
    assert r.final_status in set(FinalStatus)
    assert r.needs_human_signoff is True


def test_invalid_final_status_rejected() -> None:
    with pytest.raises(ValidationError):
        Resolution(bank_txn_id="bt_001", final_status="NEEDS_REVIEW")


def test_rejected_verdict_can_be_preserved_on_an_escalation() -> None:
    """Rejections are demo gold, not embarrassments (8). The escalated
    resolution keeps both the verdict and the reason it failed."""
    verdict = Verdict(exception_id="EX-1", hypothesis="h", confidence="resolved")
    result = VerifierResult(
        exception_id="EX-1", passed=False, rejection_reason="phantom_reference"
    )
    r = Resolution(
        bank_txn_id="bt_001",
        final_status=FinalStatus.ESCALATED,
        verdict=verdict,
        verifier_result=result,
    )
    assert r.final_status is FinalStatus.ESCALATED
    assert r.verdict is not None
    assert r.verifier_result is not None and not r.verifier_result.passed


# --------------------------------------------------------------------------
# Verdict shape
# --------------------------------------------------------------------------


def test_unresolvable_verdict_needs_no_proposed_match() -> None:
    """Giving up is a valid, expected outcome (7.2). Forcing a match here
    would push the model to invent one."""
    v = Verdict(exception_id="EX-1", hypothesis="no counterpart", confidence="unresolvable")
    assert v.proposed_match is None


def test_verdict_carries_rejected_hypotheses() -> None:
    """The escalation screen is far more convincing showing what was ruled
    out than saying only 'could not resolve' (10.5)."""
    v = Verdict(
        exception_id="EX-1",
        hypothesis="genuinely missing settlement",
        hypotheses_rejected=[
            RejectedHypothesis(hypothesis="partial refund", reason="no refunds exist"),
            RejectedHypothesis(hypothesis="fee schedule v2", reason="net still off by 900"),
        ],
        confidence="unresolvable",
        evidence=[Evidence(tool="get_refunds", args={"payment_id": "pay_001"}, result_summary="none")],
    )
    assert len(v.hypotheses_rejected) == 2
    assert v.evidence[0].tool == "get_refunds"


def test_split_settlement_verdict_can_cite_a_second_bank_leg() -> None:
    """E5 spans two bank credits; the schema has to be able to say so."""
    pm = ProposedMatch(
        bank_txn_id="bt_001",
        extra_bank_txn_ids=["bt_002"],
        payment_ids=["pay_001", "pay_002"],
        fee_schedule="v1",
        arithmetic=Arithmetic(gross="5000", mdr="100", gst="18", net="4882"),
    )
    assert pm.extra_bank_txn_ids == ["bt_002"]


# --------------------------------------------------------------------------
# Remaining records
# --------------------------------------------------------------------------


def test_settlement_and_order_round_trip() -> None:
    s = Settlement(
        settlement_id="setl_001",
        utr="SBIN0123456789AB",
        settled_at=date(2026, 3, 4),
        payment_ids=["pay_001"],
        amount_gross="5000",
        fee_mdr="112.50",
        fee_gst="20.25",
        amount_settled="4867.25",
        fee_schedule="v2",
    )
    assert Settlement.model_validate_json(s.model_dump_json()) == s

    o = Order(
        order_id="ord_001",
        sku="SKU-1",
        order_amount="5000",
        order_date=date(2026, 3, 1),
        channel="web",
    )
    assert Order.model_validate_json(o.model_dump_json()) == o


def test_bank_txn_keeps_raw_narration_alongside_parsed_utr() -> None:
    """The drill-down screen shows what the parser actually saw, which is
    the whole story for an E8 exception."""
    b = a_bank_txn(narration="NEFT-RAZORPAY-TRUNC-SETTLEMENT", utr=None)
    assert b.utr is None
    assert "TRUNC" in b.narration


def test_exception_item_requires_a_known_reason() -> None:
    ExceptionItem(exception_id="EX-1", bank_txn_id="bt_001", reason="ambiguous_tie")
    with pytest.raises(ValidationError):
        ExceptionItem(exception_id="EX-1", bank_txn_id="bt_001", reason="felt_wrong")


def test_run_result_defaults_to_empty() -> None:
    r = RunResult(run_id="run_1", seed=42, started_at=datetime(2026, 3, 4, 10, 0))
    assert r.resolutions == []
    assert r.finished_at is None
