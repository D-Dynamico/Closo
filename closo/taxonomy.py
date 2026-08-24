"""The error taxonomy, settlement arithmetic, and the unresolvability guard.

Split out of ``generator.py`` because these are the parts other modules
legitimately need. ``metrics.py`` reports per-class counts, the tests assert
against the same table the generator built from, and Stage 3's matcher
reuses :func:`settlement_math` as the thing it recomputes. One definition,
imported everywhere, so a count can never drift between the generator and
the scorecard that grades it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from closo.config import (
    FEE_SCHEDULES,
    PASS_C_TOLERANCE,
    ZERO,
    FeeSchedule,
    gst_on,
    money,
)
from closo.schemas import BankTxn, Order, Payment, Settlement

# --------------------------------------------------------------------------
# Taxonomy (CLAUDE.md 5.2)
# --------------------------------------------------------------------------

TOTAL_PAYMENTS = 150

#: Payments seeded into each class. E1 is the remainder, so the total is
#: exactly TOTAL_PAYMENTS by construction rather than by luck.
PAYMENT_CLASS_COUNTS: dict[str, int] = {
    "E2": 8,   # settlement lag
    "E3": 5,   # partial refund netted
    "E4": 4,   # fee schedule mismatch
    "E5": 3,   # split settlement
    "E6": 2,   # duplicate UTR
    "E7": 4,   # rounding drift
    "E8": 2,   # missing UTR in narration
    "E9": 2,   # genuinely missing settlement
}
PAYMENT_CLASS_COUNTS["E1"] = TOTAL_PAYMENTS - sum(PAYMENT_CLASS_COUNTS.values())

#: E10 is a bank credit with no Razorpay counterpart, so it seeds no payments.
E10_COUNT = 2

#: Clean payments reserved for the fee-cutover boundary settlements. Drawn
#: from E1's allocation rather than added on top, so the batch stays at
#: exactly TOTAL_PAYMENTS. They are still E1 - a clean match that happens to
#: sit on the boundary (12.1).
CUTOVER_PAYMENTS = 4

#: Payments per settlement, by class. Chosen so the batch rolls into ~40
#: settlements while keeping each settlement class-homogeneous - a mixed
#: settlement would make its ground-truth error class ambiguous.
PAYMENTS_PER_SETTLEMENT: dict[str, int] = {
    "E1": 5, "E2": 2, "E3": 1, "E4": 2, "E5": 3,
    "E6": 1, "E7": 2, "E8": 1, "E9": 1,
}

#: Classes no layer may resolve. A run that resolves one of these has a
#: critical bug, not a good day (5.2).
DESIGNED_UNRESOLVABLE = frozenset({"E9", "E10"})

WINDOW_START = date(2026, 2, 2)
WINDOW_END = date(2026, 4, 24)

BANK_CODES = ("SBIN", "HDFC", "ICIC", "UTIB", "KKBK")

NARRATION_TEMPLATES = (
    "NEFT-RAZORPAYSOFTWARE-{utr}-SETTLEMENT",
    "NEFT/RAZORPAY SOFTWARE PVT/{utr}/CR",
    "RTGS-{utr}-RAZORPAY-COLLECTION",
    "IMPS REF {utr} RAZORPAY",
    "NEFT  RAZORPAYSOFTWARE  {utr}  CR",
)

#: Narrations for E8: the UTR is truncated below the 16-character minimum,
#: so config.normalize_utr refuses to parse it rather than prefix-matching.
TRUNCATED_NARRATION_TEMPLATES = (
    "NEFT-RAZORPAYSOFTWARE-{stub}-SETTLEMENT",
    "NEFT/RAZORPAY/{stub}/CR",
)

SKUS = ("SKU-KEYBOARD", "SKU-MOUSE", "SKU-MONITOR", "SKU-DOCK", "SKU-CABLE")
CHANNELS = ("web", "app", "marketplace")


@dataclass
class GeneratedBatch:
    """One complete synthetic dataset plus the truth about it."""

    payments: list[Payment] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)
    bank_txns: list[BankTxn] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)

    #: Ground truth keyed by bank transaction. One entry per credit, always.
    ground_truth: dict = field(default_factory=dict)

    #: E9 lives here rather than in ``ground_truth`` because it has no bank
    #: credit to key on - which is exactly what makes it unresolvable.
    missing_settlements: dict = field(default_factory=dict)

    def class_counts(self) -> dict[str, int]:
        """Bank credits per error class, plus E9 counted by settlement."""
        counts: dict[str, int] = {}
        for entry in self.ground_truth.values():
            counts[entry["error_class"]] = counts.get(entry["error_class"], 0) + 1
        if self.missing_settlements:
            counts["E9"] = len(self.missing_settlements)
        return counts

    def payments_by_id(self) -> dict[str, Payment]:
        """Index payments for lookup during verification."""
        return {p.payment_id: p for p in self.payments}


# --------------------------------------------------------------------------
# Settlement arithmetic
# --------------------------------------------------------------------------


def settlement_math(
    payments: list[Payment],
    schedule: FeeSchedule,
    rounding: Decimal = ZERO,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Compute (gross, mdr, gst, net) for a batch of payments.

    Gross is net of refunds because a refunded payment settles for less,
    while MDR is charged on the original gross - a processor keeps its fee
    on a refunded transaction. GST is quantized per fee row and then summed
    rather than computed on the total, so the figure is reproducible row by
    row the way the verifier will need it (12.1).
    """
    gross = money(sum((p.net_of_refund for p in payments), ZERO))
    mdr = ZERO
    gst = ZERO
    for payment in payments:
        row_mdr = schedule.mdr_for(payment.method, payment.amount_gross)
        mdr = money(mdr + row_mdr)
        gst = money(gst + gst_on(row_mdr))
    net = money(gross - mdr - gst + rounding)
    return gross, mdr, gst, net


# --------------------------------------------------------------------------
# Unresolvability guard (E10)
# --------------------------------------------------------------------------


def verify_unresolvable(
    credit: BankTxn,
    settlements: list[Settlement],
    payments_by_id: dict[str, Payment],
    tolerance: Decimal = PASS_C_TOLERANCE,
) -> bool:
    """True if ``credit`` matches no settlement under any fee schedule.

    Brute force on purpose. E10 is meant to be genuinely unresolvable, and
    "we constructed it that way" is not evidence: a randomly drawn amount
    can collide with a real settlement net by chance, and the resulting E10
    would be quietly resolvable while every test in the suite still passed.
    The honest exception list would be wrong with nothing to notice.

    Checks the recomputation Layer 1's Pass C actually performs, against
    both fee schedules, plus the settlement's own recorded net.
    """
    for settlement in settlements:
        members = [
            payments_by_id[pid]
            for pid in settlement.payment_ids
            if pid in payments_by_id
        ]
        if members:
            for schedule in FEE_SCHEDULES.values():
                _, _, _, net = settlement_math(members, schedule)
                if abs(credit.credit_amount - net) <= tolerance:
                    return False
        if abs(credit.credit_amount - settlement.amount_settled) <= tolerance:
            return False
    return True
