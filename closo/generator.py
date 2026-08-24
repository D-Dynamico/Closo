"""Seeded synthetic data generator with known ground truth.

Every accuracy number Closo reports is measured against the ground truth
this module writes, so it is built before the matcher rather than
alongside it. Three properties matter more than realism:

**Determinism.** One seeded ``random.Random`` instance, never the global
module. Same seed, byte-identical CSVs (12.1).

**Exact taxonomy.** The counts in :data:`PAYMENT_CLASS_COUNTS` are the
contract. A generator that produced "roughly" the right mix would make
every per-class metric on the scorecard unfalsifiable.

**Honest unresolvables.** E9 and E10 must be genuinely impossible, not
merely hard. :func:`verify_unresolvable` brute-forces that rather than
trusting the construction, because an accidentally-solvable E10 would
break the honest-exception-list story with no visible symptom.

The pipeline never reads ``ground_truth.json``; only ``metrics.py`` may,
and only after a run completes (11.4).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from closo.config import (
    DEMO_SEED,
    FEE_CUTOVER_DATE,
    FEE_SCHEDULES,
    PASS_C_TOLERANCE,
    PAYMENT_METHODS,
    ZERO,
    FeeSchedule,
    active_schedule,
    add_business_days,
    gst_on,
    money,
)
from closo.schemas import BankTxn, Order, Payment, Settlement

# --------------------------------------------------------------------------
# Taxonomy (CLAUDE.md 5.2)
# --------------------------------------------------------------------------

TOTAL_PAYMENTS = 150

#: Payments seeded into each error class. E1 is the remainder, so the total
#: is exactly TOTAL_PAYMENTS by construction rather than by luck.
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

#: Payments per settlement, by class. Chosen so the batch rolls into ~40
#: settlements while keeping each settlement class-homogeneous - a mixed
#: settlement would make its ground-truth error class ambiguous.
PAYMENTS_PER_SETTLEMENT: dict[str, int] = {
    "E1": 5, "E2": 2, "E3": 1, "E4": 2, "E5": 3,
    "E6": 1, "E7": 2, "E8": 1, "E9": 1,
}

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
    ground_truth: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Money helpers
# --------------------------------------------------------------------------


def settlement_math(
    payments: list[Payment],
    schedule: FeeSchedule,
    rounding: Decimal = ZERO,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Compute (gross, mdr, gst, net) for a batch of payments.

    Gross is net of refunds because a refunded payment settles for less,
    while MDR is charged on the original gross - the processor keeps its
    fee on a refunded transaction. GST is computed and quantized per fee
    row, then summed, so the total is reproducible row by row (12.1).
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
# Generator
# --------------------------------------------------------------------------


class Generator:
    """Builds one deterministic batch.

    All randomness flows through ``self.rng``. Touching the global
    ``random`` module anywhere in here silently breaks the determinism
    guarantee, which is the whole basis of the replay demo.
    """

    def __init__(self, seed: int = DEMO_SEED) -> None:
        self.rng = random.Random(seed)
        self.seed = seed
        self._utrs_issued: set[str] = set()
        self._counter = 0

    # -- primitives --------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:04d}"

    def _utr(self) -> str:
        """A unique 16-character UTR: 4-letter bank code plus 12 digits."""
        while True:
            candidate = self.rng.choice(BANK_CODES) + "".join(
                str(self.rng.randint(0, 9)) for _ in range(12)
            )
            if candidate not in self._utrs_issued:
                self._utrs_issued.add(candidate)
                return candidate

    def _amount(self, low: int = 500, high: int = 50_000) -> Decimal:
        """A gross payment amount, to the paisa."""
        rupees = self.rng.randint(low, high)
        paise = self.rng.choice((0, 25, 50, 75, 99))
        return money(f"{rupees}.{paise:02d}")

    def _capture_date(self) -> date:
        span = (WINDOW_END - WINDOW_START).days
        return WINDOW_START + timedelta(days=self.rng.randint(0, span))

    def _business_day(self, day: date) -> date:
        """Nudge a date forward off a weekend. Banks do not settle Sat/Sun."""
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return day

    def _narration(self, utr: str) -> str:
        return self.rng.choice(NARRATION_TEMPLATES).format(utr=utr)

    # -- record construction ----------------------------------------------

    def _make_payment(
        self,
        captured_at: date,
        method: str | None = None,
        low: int = 500,
        high: int = 50_000,
    ) -> tuple[Payment, Order]:
        """One payment and the internal order row behind it."""
        order_id = self._next_id("ord")
        payment_id = self._next_id("pay")
        gross = self._amount(low, high)
        chosen = method or self.rng.choice(PAYMENT_METHODS)

        payment = Payment(
            payment_id=payment_id,
            order_id=order_id,
            amount_gross=gross,
            method=chosen,
            captured_at=captured_at,
        )
        order = Order(
            order_id=order_id,
            sku=self.rng.choice(SKUS),
            order_amount=gross,
            order_date=captured_at,
            channel=self.rng.choice(CHANNELS),
        )
        return payment, order

    def _settle(
        self,
        payments: list[Payment],
        settled_at: date,
        schedule: FeeSchedule | None = None,
        rounding: Decimal = ZERO,
        utr: str | None = None,
    ) -> Settlement:
        """Build a settlement and stamp its legs back onto the payments."""
        applied = schedule or active_schedule(settled_at)
        gross, mdr, gst, net = settlement_math(payments, applied, rounding)
        settlement_id = self._next_id("setl")
        settlement_utr = utr or self._utr()

        for payment in payments:
            row_mdr = applied.mdr_for(payment.method, payment.amount_gross)
            payment.settlement_id = settlement_id
            payment.settlement_utr = settlement_utr
            payment.settled_at = settled_at
            payment.fee_mdr = row_mdr
            payment.fee_gst = gst_on(row_mdr)
            payment.amount_settled = money(
                payment.net_of_refund - row_mdr - gst_on(row_mdr)
            )

        return Settlement(
            settlement_id=settlement_id,
            utr=settlement_utr,
            settled_at=settled_at,
            payment_ids=[p.payment_id for p in payments],
            amount_gross=gross,
            fee_mdr=mdr,
            fee_gst=gst,
            amount_settled=net,
            fee_schedule=applied.name,
            rounding=rounding,
        )

    def _credit(
        self,
        settlement: Settlement,
        amount: Decimal,
        value_date: date | None = None,
        narration: str | None = None,
        utr: str | None = None,
    ) -> BankTxn:
        """One bank credit line for a settlement."""
        landed = value_date or self._business_day(settlement.settled_at)
        parsed_utr = settlement.utr if utr is None else utr
        return BankTxn(
            bank_txn_id=self._next_id("bt"),
            txn_date=landed,
            value_date=landed,
            narration=narration or self._narration(settlement.utr),
            utr=parsed_utr,
            credit_amount=amount,
        )


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
    "we constructed it that way" is not evidence - a random amount can
    collide with a real settlement net by chance, and the resulting E10
    would be quietly resolvable while every test still passed. This checks
    the recomputation Layer 1's Pass C would actually perform, against both
    schedules, for every settlement in the batch.
    """
    for settlement in settlements:
        members = [
            payments_by_id[pid]
            for pid in settlement.payment_ids
            if pid in payments_by_id
        ]
        if not members:
            continue
        for schedule in FEE_SCHEDULES.values():
            _, _, _, net = settlement_math(members, schedule)
            if abs(credit.credit_amount - net) <= tolerance:
                return False
        if abs(credit.credit_amount - settlement.amount_settled) <= tolerance:
            return False
    return True


# --------------------------------------------------------------------------
# CSV / JSON output
# --------------------------------------------------------------------------

PAYMENT_COLUMNS = [
    "payment_id", "order_id", "amount_gross", "method", "captured_at",
    "settlement_id", "settlement_utr", "settled_at", "fee_mdr", "fee_gst",
    "amount_settled", "status", "refund_amount",
]
BANK_COLUMNS = ["txn_date", "value_date", "narration", "utr", "credit_amount", "balance"]
ORDER_COLUMNS = ["order_id", "sku", "order_amount", "order_date", "channel", "expected_settlement"]
SETTLEMENT_COLUMNS = [
    "settlement_id", "utr", "settled_at", "payment_ids", "amount_gross",
    "fee_mdr", "fee_gst", "amount_settled", "fee_schedule", "rounding",
]


def _cell(value: object) -> str:
    """Render one CSV cell. Amounts stay strings; None becomes empty."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    return str(value)


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    """Write a CSV with LF endings so hashes match across platforms."""
    with path.open("w", newline="\n", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _cell(row.get(c)) for c in columns})
