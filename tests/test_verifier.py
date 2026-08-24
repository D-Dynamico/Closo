"""Tests for Layer 3 (TEST_PLAN 12.4) — the most important file in the repo.

Closo's claim is that a separate deterministic checker can overrule the
model. Everything else is scaffolding around that. So every verdict here
is **hand-crafted**, never generated: a fixture written by the thing under
test proves only that it agrees with itself.

Each rejection case gets its own test, and each one is a specific way a
plausible-looking verdict is wrong. The recurring theme is that all of
them would pass a checker that read the verdict's own arithmetic block.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from closo.config import FEE_SCHEDULES, active_schedule, money
from closo.layer3_verifier import MAX_CLAIMED_ROUNDING, Verifier, verify_verdict
from closo.schemas import Arithmetic, Payment, ProposedMatch, Settlement, Verdict
from closo.taxonomy import GeneratedBatch, settlement_math
from closo.schemas import BankTxn

REPO_ROOT = Path(__file__).resolve().parent.parent

# Monday, after the cutover, so v2 is the active schedule.
SETTLED = date(2026, 3, 9)
PRE_CUTOVER = date(2026, 2, 27)  # the Friday before; v1 is active


# --------------------------------------------------------------------------
# World builders — small, explicit, hand-written
# --------------------------------------------------------------------------


def a_payment(
    pid: str, gross: str, method: str = "card",
    settled_at: date | None = SETTLED, refund: str = "0.00",
) -> Payment:
    return Payment(
        payment_id=pid, order_id=f"ord_{pid}", amount_gross=gross, method=method,
        captured_at=date(2026, 3, 2), refund_amount=refund,
        status="partial_refund" if Decimal(refund) > 0 else "captured",
        settlement_id="setl_1", settled_at=settled_at,
    )


def a_credit(bid: str, amount: str) -> BankTxn:
    return BankTxn(
        bank_txn_id=bid, txn_date=SETTLED, value_date=SETTLED,
        narration=f"NEFT-RAZORPAY-{bid}", credit_amount=amount,
    )


def a_world(payments: list[Payment], credits: list[BankTxn]) -> GeneratedBatch:
    batch = GeneratedBatch()
    batch.payments = payments
    batch.bank_txns = credits
    batch.settlements = [
        Settlement(
            settlement_id="setl_1", utr="SBIN0123456789AB", settled_at=SETTLED,
            payment_ids=[p.payment_id for p in payments],
            amount_gross="0.00", fee_mdr="0.00", fee_gst="0.00",
            amount_settled="0.00", fee_schedule="v2",
        )
    ]
    return batch


def truthful_verdict(
    payments: list[Payment], credit_id: str, schedule: str = "v2",
    confidence: str = "resolved", hypothesis: str = "clean settlement",
    extra_credits: list[str] | None = None, rounding: str = "0.00",
) -> Verdict:
    """A verdict whose arithmetic is genuinely correct.

    The baseline every rejection test perturbs by exactly one thing, so a
    failure is attributable to that one thing.
    """
    gross, mdr, gst, _net = settlement_math(
        payments, FEE_SCHEDULES[schedule], money(rounding)
    )
    return Verdict(
        exception_id="EX-001", hypothesis=hypothesis, confidence=confidence,
        proposed_match=ProposedMatch(
            bank_txn_id=credit_id,
            extra_bank_txn_ids=extra_credits or [],
            payment_ids=[p.payment_id for p in payments],
            fee_schedule=schedule,
            arithmetic=Arithmetic(
                gross=gross, mdr=mdr, gst=gst, rounding=rounding,
                net=money(gross - mdr - gst + money(rounding)),
            ),
        ),
    )


def settled_net(payments: list[Payment], schedule: str = "v2") -> str:
    _, _, _, net = settlement_math(payments, FEE_SCHEDULES[schedule])
    return str(net)


@pytest.fixture
def simple():
    """One card payment, one credit for exactly the right amount."""
    payment = a_payment("pay_1", "5000.00")
    credit = a_credit("bt_1", settled_net([payment]))
    return a_world([payment], [credit]), [payment], credit


# --------------------------------------------------------------------------
# The happy path — one test, because it proves the least
# --------------------------------------------------------------------------


def test_a_correct_verdict_passes(simple) -> None:
    batch, payments, credit = simple
    result = verify_verdict(truthful_verdict(payments, credit.bank_txn_id), batch)
    assert result.passed
    assert result.effective_confidence == "resolved"
    assert result.needs_human_signoff is False
    assert {c.check for c in result.checks} == {
        "existence", "refund_consistency", "exclusivity", "fee_schedule", "arithmetic"
    }


# --------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------


def test_phantom_payment_id_is_rejected(simple) -> None:
    """An invented record. The arithmetic cannot be checked at all, so this
    has to be caught before anything else is attempted."""
    batch, payments, credit = simple
    verdict = truthful_verdict(payments, credit.bank_txn_id)
    verdict.proposed_match.payment_ids = ["pay_does_not_exist"]

    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "phantom_reference"


def test_phantom_bank_txn_id_is_rejected(simple) -> None:
    batch, payments, credit = simple
    verdict = truthful_verdict(payments, credit.bank_txn_id)
    verdict.proposed_match.bank_txn_id = "bt_imaginary"

    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "phantom_reference"


def test_off_by_a_paisa_is_rejected(simple) -> None:
    """The test that proves the model's arithmetic is a claim, not evidence.

    The verdict's block is internally consistent and sums to the credit.
    Only recomputation from the raw payment reveals the one-paisa gap, and
    a paisa is not a rounding difference to be waved through - it is the
    difference between reconciled and not.
    """
    batch, payments, _credit = simple
    wrong_credit = a_credit("bt_off", money(Decimal(settled_net(payments)) + Decimal("0.01")))
    batch.bank_txns.append(wrong_credit)

    verdict = truthful_verdict(payments, "bt_off")
    # Make the block agree with the credit rather than with the records.
    verdict.proposed_match.arithmetic.net = wrong_credit.credit_amount
    verdict.proposed_match.arithmetic.gross = money(
        verdict.proposed_match.arithmetic.gross + Decimal("0.01")
    )

    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "arithmetic_mismatch"


def test_internally_consistent_but_wrong_is_rejected(simple) -> None:
    """The most dangerous case. The block is self-consistent AND matches the
    credit; only the cited payments' real gross disagrees.

    A verifier that trusted the block would pass this, and the resolution
    would look impeccable in the drill-down.
    """
    batch, _payments, _credit = simple
    real = a_payment("pay_real", "5000.00")
    batch.payments.append(real)

    # A credit for a settlement of 9000 gross, cited against a 5000 payment.
    fake_payments = [a_payment("pay_ghost", "9000.00")]
    gross, mdr, gst, net = settlement_math(fake_payments, FEE_SCHEDULES["v2"])
    credit = a_credit("bt_consistent", str(net))
    batch.bank_txns.append(credit)

    verdict = Verdict(
        exception_id="EX-001", hypothesis="settlement", confidence="resolved",
        proposed_match=ProposedMatch(
            bank_txn_id="bt_consistent", payment_ids=["pay_real"], fee_schedule="v2",
            arithmetic=Arithmetic(gross=gross, mdr=mdr, gst=gst, net=net),
        ),
    )

    # The block is beyond reproach on its own terms.
    assert money(gross - mdr - gst) == net == credit.credit_amount

    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "arithmetic_mismatch"


def test_a_bottom_line_that_does_not_follow_from_its_own_line_items(simple) -> None:
    """The block's line items are all correct, but its stated net does not
    follow from them — and happens to equal the bank credit.

    This isolates the single most important behaviour in the project: the
    verifier must compute the net from the records, not read the net the
    model wrote. Every other rejection test here also trips a second check,
    so each of them would still pass if the verifier trusted the claimed
    net. Mutating it to do exactly that was caught by nothing until this.
    """
    batch, payments, _credit = simple
    gross, mdr, gst, real_net = settlement_math(payments, FEE_SCHEDULES["v2"])

    lie = money(real_net + Decimal("300.00"))
    batch.bank_txns.append(a_credit("bt_lie", str(lie)))

    verdict = Verdict(
        exception_id="EX-001", hypothesis="settlement", confidence="resolved",
        proposed_match=ProposedMatch(
            bank_txn_id="bt_lie", payment_ids=["pay_1"], fee_schedule="v2",
            # Line items are exactly right; only the total is invented.
            arithmetic=Arithmetic(gross=gross, mdr=mdr, gst=gst, net=lie),
        ),
    )
    assert verdict.proposed_match.arithmetic.gross == gross
    assert verdict.proposed_match.arithmetic.net == lie

    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "arithmetic_mismatch"


def test_line_items_that_lie_while_the_total_comes_out_right(simple) -> None:
    """The mirror image: the net is genuinely correct and reproduces the
    credit, but the claimed gross and MDR are both inflated by the same
    amount so they still net out.

    The arithmetic check alone passes this, because the recomputed net does
    match the credit. Only comparing the block's individual figures against
    the records catches it. Disabling that comparison was caught by nothing
    until this test — the drill-down would have shown a completely fictional
    fee breakdown under a correct bottom line.
    """
    batch, payments, credit = simple
    gross, mdr, gst, net = settlement_math(payments, FEE_SCHEDULES["v2"])
    assert credit.credit_amount == net

    inflated = Decimal("1000.00")
    verdict = Verdict(
        exception_id="EX-001", hypothesis="settlement", confidence="resolved",
        proposed_match=ProposedMatch(
            bank_txn_id=credit.bank_txn_id, payment_ids=["pay_1"],
            fee_schedule="v2",
            arithmetic=Arithmetic(
                gross=money(gross + inflated), mdr=money(mdr + inflated),
                gst=gst, net=net,
            ),
        ),
    )
    block = verdict.proposed_match.arithmetic
    assert money(block.gross - block.mdr - block.gst) == net == credit.credit_amount

    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "arithmetic_mismatch"
    detail = next(c.detail for c in result.checks if not c.passed)
    assert "gross" in detail


def test_double_spend_across_resolutions_is_rejected(simple) -> None:
    """Two exceptions each resolving with the same payment. Both look
    individually correct; together they double-count the money."""
    batch, payments, credit = simple
    second = a_credit("bt_2", settled_net(payments))
    batch.bank_txns.append(second)

    verifier = Verifier(batch)
    first = verifier.verify(truthful_verdict(payments, credit.bank_txn_id))
    assert first.passed
    verifier.commit(first)

    again = verifier.verify(truthful_verdict(payments, "bt_2"))
    assert not again.passed
    assert again.rejection_reason == "exclusivity_violation"


def test_same_payment_cited_twice_in_one_verdict_is_rejected(simple) -> None:
    """Cheaper way to double-count: cite it twice in a single claim."""
    batch, payments, credit = simple
    verdict = truthful_verdict(payments, credit.bank_txn_id)
    verdict.proposed_match.payment_ids = ["pay_1", "pay_1"]

    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "exclusivity_violation"


def test_fabricated_refund_is_rejected(simple) -> None:
    """A refund is the most natural way to explain a credit that came up
    short, which makes it the claim most worth checking against records."""
    batch, payments, credit = simple
    verdict = truthful_verdict(
        payments, credit.bank_txn_id,
        hypothesis="credit is short because of a partial refund on the payment",
    )
    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "refund_fabrication"


def test_genuine_refund_passes() -> None:
    """The other half: a real refund must not be treated as fabrication."""
    payment = a_payment("pay_1", "5000.00", refund="1000.00")
    credit = a_credit("bt_1", settled_net([payment]))
    batch = a_world([payment], [credit])

    verdict = truthful_verdict(
        [payment], "bt_1", hypothesis="partial refund netted into the settlement"
    )
    result = verify_verdict(verdict, batch)
    assert result.passed


def test_unknown_fee_schedule_is_rejected(simple) -> None:
    batch, payments, credit = simple
    verdict = truthful_verdict(payments, credit.bank_txn_id)
    verdict.proposed_match.fee_schedule = "v99"

    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "wrong_fee_schedule"


def test_rounding_cannot_be_used_as_a_fudge_factor(simple) -> None:
    """An unbounded rounding field reconciles anything to anything. Bounded
    at the Pass C tolerance, it stays what it is meant to be."""
    batch, payments, _credit = simple
    net = Decimal(settled_net(payments))
    credit = a_credit("bt_fudge", money(net + Decimal("500.00")))
    batch.bank_txns.append(credit)

    verdict = truthful_verdict(payments, "bt_fudge", rounding="500.00")
    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "arithmetic_mismatch"


def test_rounding_at_the_bound_is_accepted(simple) -> None:
    batch, payments, _credit = simple
    net = Decimal(settled_net(payments))
    batch.bank_txns.append(a_credit("bt_edge", money(net + MAX_CLAIMED_ROUNDING)))

    verdict = truthful_verdict(payments, "bt_edge", rounding=str(MAX_CLAIMED_ROUNDING))
    assert verify_verdict(verdict, batch).passed


def test_a_penny_past_the_rounding_bound_is_rejected(simple) -> None:
    batch, payments, _credit = simple
    over = MAX_CLAIMED_ROUNDING + Decimal("0.01")
    net = Decimal(settled_net(payments))
    batch.bank_txns.append(a_credit("bt_over", money(net + over)))

    verdict = truthful_verdict(payments, "bt_over", rounding=str(over))
    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "arithmetic_mismatch"


def test_resolved_verdict_with_no_proposed_match_is_malformed(simple) -> None:
    batch, _payments, _credit = simple
    verdict = Verdict(exception_id="EX-001", hypothesis="h", confidence="resolved")
    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "malformed_verdict"


# --------------------------------------------------------------------------
# Fee-schedule anomaly (8.1) — the E4 resolution
# --------------------------------------------------------------------------


def test_correct_schedule_on_the_cutover_passes_as_resolved() -> None:
    """A settlement on the cutover belongs to v2. Citing v2 is unremarkable."""
    payment = a_payment("pay_1", "5000.00", settled_at=date(2026, 3, 2))
    assert active_schedule(date(2026, 3, 2)).name == "v2"
    credit = a_credit("bt_1", settled_net([payment], "v2"))
    batch = a_world([payment], [credit])

    result = verify_verdict(truthful_verdict([payment], "bt_1", "v2"), batch)
    assert result.passed
    assert result.effective_confidence == "resolved"
    assert result.schedule_anomaly is None


def test_v1_on_the_day_before_cutover_passes_as_resolved() -> None:
    payment = a_payment("pay_1", "5000.00", settled_at=PRE_CUTOVER)
    assert active_schedule(PRE_CUTOVER).name == "v1"
    credit = a_credit("bt_1", settled_net([payment], "v1"))
    batch = a_world([payment], [credit])

    result = verify_verdict(truthful_verdict([payment], "bt_1", "v1"), batch)
    assert result.passed
    assert result.effective_confidence == "resolved"
    assert result.schedule_anomaly is None


def test_inactive_schedule_that_reproduces_the_credit_is_capped_not_failed() -> None:
    """Error class E4. The math is proven; the intent is not.

    Failing this outright would make E4 unresolvable by construction while
    the spec lists it as a Layer 2 class (8.1).
    """
    payment = a_payment("pay_1", "50000.00", settled_at=SETTLED)  # v2 is active
    credit = a_credit("bt_1", settled_net([payment], "v1"))       # paid under v1
    batch = a_world([payment], [credit])

    result = verify_verdict(truthful_verdict([payment], "bt_1", "v1"), batch)
    assert result.passed
    assert result.effective_confidence == "probable"
    assert result.needs_human_signoff is True
    assert result.schedule_anomaly is not None
    assert "v1" in result.schedule_anomaly and "v2" in result.schedule_anomaly


def test_the_anomaly_names_both_schedules_and_the_date() -> None:
    """The escalation screen has to ask a human a specific question, not
    report that something was odd."""
    payment = a_payment("pay_1", "50000.00", settled_at=SETTLED)
    credit = a_credit("bt_1", settled_net([payment], "v1"))
    batch = a_world([payment], [credit])

    result = verify_verdict(truthful_verdict([payment], "bt_1", "v1"), batch)
    assert SETTLED.isoformat() in result.schedule_anomaly


def test_inactive_schedule_that_reproduces_nothing_still_fails() -> None:
    """The cap applies to intent, never to arithmetic."""
    payment = a_payment("pay_1", "50000.00", settled_at=SETTLED)
    credit = a_credit("bt_1", "12345.67")  # matches neither schedule
    batch = a_world([payment], [credit])

    verdict = truthful_verdict([payment], "bt_1", "v1")
    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "arithmetic_mismatch"


# --------------------------------------------------------------------------
# Confidence handling
# --------------------------------------------------------------------------


def test_probable_verdict_that_verifies_needs_signoff(simple) -> None:
    """Verified math, unverified intent. It must not silently become
    AGENT_RESOLVED_VERIFIED on the strength of clean arithmetic."""
    batch, payments, credit = simple
    verdict = truthful_verdict(payments, credit.bank_txn_id, confidence="probable")

    result = verify_verdict(verdict, batch)
    assert result.passed
    assert result.effective_confidence == "probable"
    assert result.needs_human_signoff is True


def test_the_verifier_never_promotes_confidence(simple) -> None:
    """It can demote and only demote. Clean math does not make a hedged
    verdict certain — it makes a hedged verdict arithmetically sound."""
    batch, payments, credit = simple
    result = verify_verdict(
        truthful_verdict(payments, credit.bank_txn_id, confidence="probable"), batch
    )
    assert result.effective_confidence != "resolved"


def test_unresolvable_verdict_is_not_a_verification_failure(simple) -> None:
    """Giving up is a valid, expected outcome (7.2). It claims nothing, so
    there is nothing to reject."""
    batch, _payments, _credit = simple
    verdict = Verdict(
        exception_id="EX-001", hypothesis="no counterpart found",
        confidence="unresolvable",
    )
    result = verify_verdict(verdict, batch)
    assert result.rejection_reason is None
    assert result.effective_confidence == "unresolvable"
    assert not result.passed


# --------------------------------------------------------------------------
# Split settlements (E5)
# --------------------------------------------------------------------------


def test_split_settlement_citing_both_legs_passes() -> None:
    """E5. The two credits together must equal the settlement net."""
    payments = [a_payment("pay_1", "5000.00"), a_payment("pay_2", "7000.00")]
    net = Decimal(settled_net(payments))
    first = money(net * Decimal("0.4"))
    legs = [a_credit("bt_1", str(first)), a_credit("bt_2", str(money(net - first)))]
    batch = a_world(payments, legs)

    verdict = truthful_verdict(payments, "bt_1", extra_credits=["bt_2"])
    result = verify_verdict(verdict, batch)
    assert result.passed


def test_split_settlement_citing_only_one_leg_is_rejected() -> None:
    """Half the money is not the money. This has to fail even though the
    cited leg is a real credit and the payments are real payments."""
    payments = [a_payment("pay_1", "5000.00"), a_payment("pay_2", "7000.00")]
    net = Decimal(settled_net(payments))
    first = money(net * Decimal("0.4"))
    legs = [a_credit("bt_1", str(first)), a_credit("bt_2", str(money(net - first)))]
    batch = a_world(payments, legs)

    result = verify_verdict(truthful_verdict(payments, "bt_1"), batch)
    assert not result.passed
    assert result.rejection_reason == "arithmetic_mismatch"


def test_split_verdict_citing_a_phantom_second_leg_is_rejected() -> None:
    payments = [a_payment("pay_1", "5000.00")]
    batch = a_world(payments, [a_credit("bt_1", settled_net(payments))])

    verdict = truthful_verdict(payments, "bt_1", extra_credits=["bt_nonexistent"])
    result = verify_verdict(verdict, batch)
    assert not result.passed
    assert result.rejection_reason == "phantom_reference"


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_every_check_is_reported_even_on_success(simple) -> None:
    """The drill-down shows the reasoning, not a verdict about the verdict."""
    batch, payments, credit = simple
    result = verify_verdict(truthful_verdict(payments, credit.bank_txn_id), batch)
    assert len(result.checks) == 5
    assert all(c.detail for c in result.checks)


def test_a_failure_reports_which_check_failed(simple) -> None:
    batch, payments, credit = simple
    verdict = truthful_verdict(payments, credit.bank_txn_id)
    verdict.proposed_match.payment_ids = ["pay_ghost"]

    result = verify_verdict(verdict, batch)
    failed = [c for c in result.checks if not c.passed]
    assert len(failed) == 1
    assert failed[0].check == "existence"
    assert "pay_ghost" in failed[0].detail


def test_arithmetic_failure_reports_the_actual_difference(simple) -> None:
    """The number a human needs first. 'Does not reconcile' is not useful."""
    batch, payments, _credit = simple
    net = Decimal(settled_net(payments))
    batch.bank_txns.append(a_credit("bt_x", money(net + Decimal("50.00"))))

    result = verify_verdict(truthful_verdict(payments, "bt_x"), batch)
    assert not result.passed
    detail = next(c.detail for c in result.checks if not c.passed)
    assert "50.00" in detail


def test_a_rejected_verdict_is_not_marked_as_consuming_anything(simple) -> None:
    """A failed verdict must not lock up payments a later, correct verdict
    might legitimately need."""
    batch, payments, credit = simple
    verdict = truthful_verdict(payments, credit.bank_txn_id)
    verdict.proposed_match.fee_schedule = "v99"

    verifier = Verifier(batch)
    result = verifier.verify(verdict)
    verifier.commit(result)
    assert verifier.consumed == set()


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


def test_verifier_imports_no_llm_sdk() -> None:
    code = (
        "import sys, closo.layer3_verifier; "
        "print(any(m.startswith('google.genai') or m == 'closo.llm_client' "
        "for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        check=True, cwd=str(REPO_ROOT),
    )
    assert out.stdout.strip() == "False"


def test_verifier_does_not_read_ground_truth(monkeypatch, simple) -> None:
    """It checks arithmetic, not answers. Reading ground truth would make it
    a lookup table and the accuracy figures meaningless."""
    from closo.dataset_io import GROUND_TRUTH_FILENAME

    real_open = Path.open

    def guarded(self: Path, *args: object, **kwargs: object):
        if self.name == GROUND_TRUTH_FILENAME:
            raise AssertionError("verifier opened ground truth")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    batch, payments, credit = simple
    assert verify_verdict(truthful_verdict(payments, credit.bank_txn_id), batch).passed
