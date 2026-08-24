"""Seeded synthetic data generator with known ground truth.

Every accuracy number Closo reports is measured against the ground truth
this module writes, so it is built before the matcher rather than
alongside it.

**Determinism.** One seeded ``random.Random`` instance, never the global
module, and a fixed build order. Same seed, byte-identical files (12.1).
Reordering the class builders changes the RNG stream and so changes every
generated value, which would break the frozen demo set while the code
still looked correct.

The taxonomy, settlement arithmetic and the E10 guard live in
``taxonomy.py``; file layout lives in ``dataset_io.py``.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from closo.config import (
    DATA_DIR,
    DEMO_SEED,
    FEE_CUTOVER_DATE,
    FEE_SCHEDULES,
    PAYMENT_METHODS,
    ZERO,
    FeeSchedule,
    active_schedule,
    add_business_days,
    gst_on,
    previous_business_day,
    money,
)
from closo.dataset_io import write_batch
from closo.schemas import BankTxn, Order, Payment, Settlement
from closo.taxonomy import (
    BANK_CODES,
    CHANNELS,
    CUTOVER_PAYMENTS,
    E10_COUNT,
    NARRATION_TEMPLATES,
    PAYMENT_CLASS_COUNTS,
    PAYMENTS_PER_SETTLEMENT,
    SKUS,
    TRUNCATED_NARRATION_TEMPLATES,
    WINDOW_END,
    WINDOW_START,
    GeneratedBatch,
    settlement_math,
    verify_unresolvable,
)

import random


class Generator:
    """Builds one deterministic batch."""

    def __init__(self, seed: int = DEMO_SEED) -> None:
        self.rng = random.Random(seed)
        self.seed = seed
        self.batch = GeneratedBatch()
        self._utrs_issued: set[str] = set()
        self._pending_orders: list[Order] = []
        self._missing: dict[str, dict] = {}
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

    # -- record construction ----------------------------------------------

    def _make_payment(
        self, captured_at: date, method: str | None = None
    ) -> Payment:
        """One payment plus the internal order row behind it."""
        order_id = self._next_id("ord")
        payment_id = self._next_id("pay")
        gross = self._amount()
        chosen = method or self.rng.choice(PAYMENT_METHODS)

        self._pending_orders.append(
            Order(
                order_id=order_id,
                sku=self.rng.choice(SKUS),
                order_amount=gross,
                order_date=captured_at,
                channel=self.rng.choice(CHANNELS),
            )
        )
        return Payment(
            payment_id=payment_id,
            order_id=order_id,
            amount_gross=gross,
            method=chosen,
            captured_at=captured_at,
        )

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
            row_gst = gst_on(row_mdr)
            payment.settlement_id = settlement_id
            payment.settlement_utr = settlement_utr
            payment.settled_at = settled_at
            payment.fee_mdr = row_mdr
            payment.fee_gst = row_gst
            payment.amount_settled = money(
                payment.net_of_refund - row_mdr - row_gst
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
        utr: str | None = "",
    ) -> BankTxn:
        """One bank credit line for a settlement.

        ``utr=""`` means "use the settlement's"; an explicit None means the
        narration yielded nothing parseable (E8).
        """
        landed = value_date or self._business_day(settlement.settled_at)
        parsed = settlement.utr if utr == "" else utr
        return BankTxn(
            bank_txn_id=self._next_id("bt"),
            txn_date=landed,
            value_date=landed,
            narration=narration
            or self.rng.choice(NARRATION_TEMPLATES).format(utr=settlement.utr),
            utr=parsed,
            credit_amount=amount,
        )

    # -- bookkeeping -------------------------------------------------------

    def _group(self, error_class: str, reserve: int = 0) -> list[list[Payment]]:
        """Split this class's payments into settlement-sized groups."""
        count = PAYMENT_CLASS_COUNTS[error_class] - reserve
        size = PAYMENTS_PER_SETTLEMENT[error_class]
        payments = [self._make_payment(self._capture_date()) for _ in range(count)]
        return [payments[i : i + size] for i in range(0, len(payments), size)]

    def _settled_on(self, group: list[Payment]) -> date:
        """A plausible settlement date: T+1 business day after last capture."""
        latest = max(p.captured_at for p in group)
        return self._business_day(add_business_days(latest, 1))

    def _record(
        self,
        credit: BankTxn,
        error_class: str,
        payment_ids: list[str],
        resolution: str,
        settlement_id: str | None = None,
    ) -> None:
        """Register one bank credit in ground truth. Exactly once, always."""
        assert credit.bank_txn_id not in self.batch.ground_truth
        self.batch.ground_truth[credit.bank_txn_id] = {
            "source_payment_ids": payment_ids,
            "settlement_id": settlement_id,
            "error_class": error_class,
            "true_resolution": resolution,
        }

    def _commit(
        self,
        group: list[Payment],
        settlement: Settlement,
        credits: list[BankTxn],
        error_class: str,
        resolution: str,
    ) -> None:
        """Append one class's records and register its credits."""
        self.batch.payments.extend(group)
        self.batch.settlements.append(settlement)
        self.batch.bank_txns.extend(credits)
        for credit in credits:
            self._record(
                credit,
                error_class,
                settlement.payment_ids,
                resolution,
                settlement.settlement_id,
            )

    # -- per-class builders ------------------------------------------------

    def _build_simple(
        self,
        error_class: str,
        resolution: str,
        *,
        reserve: int = 0,
        schedule: FeeSchedule | None = None,
        rounding_drift: bool = False,
        lag_days: int = 0,
        truncate_utr: bool = False,
    ) -> None:
        """Build a class whose settlements each produce one bank credit.

        Covers E1 and the variations differing only in how the credit or the
        settlement is perturbed: a late value date (E2), an applied schedule
        that is not the active one (E4), a paisa or two of drift (E7), or a
        narration with no parseable UTR (E8).
        """
        for group in self._group(error_class, reserve):
            settled_at = self._settled_on(group)
            drift = ZERO
            if rounding_drift:
                drift = money(self.rng.choice(("1.00", "-1.00", "2.00", "-1.50")))

            settlement = self._settle(group, settled_at, schedule, drift)

            value_date = self._business_day(settled_at)
            if lag_days:
                value_date = add_business_days(settled_at, lag_days)

            narration = None
            parsed_utr: str | None = ""
            if truncate_utr:
                narration = self.rng.choice(TRUNCATED_NARRATION_TEMPLATES).format(
                    stub=settlement.utr[:10]
                )
                parsed_utr = None

            credit = self._credit(
                settlement,
                settlement.amount_settled,
                value_date=value_date,
                narration=narration,
                utr=parsed_utr,
            )
            self._commit(group, settlement, [credit], error_class, resolution)

    def _build_refunds(self) -> None:
        """E3 - a partial refund netted into the settlement.

        Nothing in the bank narration says why the credit is small. That is
        the point: the investigator has to go and find the refund.
        """
        for group in self._group("E3"):
            for payment in group:
                payment.refund_amount = money(
                    payment.amount_gross * Decimal("0.25")
                )
                payment.status = "partial_refund"

            settlement = self._settle(group, self._settled_on(group))
            credit = self._credit(settlement, settlement.amount_settled)
            self._commit(
                group, settlement, [credit], "E3",
                "partial refund netted into the settlement",
            )

    def _build_splits(self) -> None:
        """E5 - one settlement paid out across two bank credits.

        The legs are deliberately uneven. A clean half-and-half split would
        let a matcher guess the pairing from the amounts alone, and the class
        would prove nothing.
        """
        for group in self._group("E5"):
            settled_at = self._settled_on(group)
            settlement = self._settle(group, settled_at)

            first = money(settlement.amount_settled * Decimal("0.4"))
            second = money(settlement.amount_settled - first)
            assert money(first + second) == settlement.amount_settled

            legs = [
                self._credit(settlement, first),
                self._credit(
                    settlement, second,
                    value_date=add_business_days(settled_at, 1),
                ),
            ]
            self._commit(
                group, settlement, legs, "E5",
                "split settlement: two bank credits sum to one settlement net",
            )

    def _build_duplicate_utr(self) -> None:
        """E6 - two different settlements carrying the same UTR.

        Pass A must refuse a UTR join yielding two candidates rather than
        picking one (12.2). Amounts differ so the duplication is unambiguous
        in the data even though the join is not.
        """
        shared_utr = self._utr()
        credits: list[BankTxn] = []
        for group in self._group("E6"):
            settlement = self._settle(
                group, self._settled_on(group), utr=shared_utr
            )
            credit = self._credit(settlement, settlement.amount_settled)
            credits.append(credit)
            self._commit(
                group, settlement, [credit], "E6",
                "duplicate UTR shared by two settlements",
            )
        amounts = {c.credit_amount for c in credits}
        assert len(amounts) == len(credits), "E6 credits must differ in amount"

    # -- designed unresolvables -------------------------------------------

    def _build_missing_settlements(self) -> None:
        """E9 - the money genuinely never arrived.

        Recorded under a separate key because ground truth is indexed by
        bank transaction and an E9 has none, which is precisely the point.
        """
        for group in self._group("E9"):
            settlement = self._settle(group, self._settled_on(group))
            self.batch.payments.extend(group)
            self.batch.settlements.append(settlement)
            self._missing[settlement.settlement_id] = {
                "source_payment_ids": settlement.payment_ids,
                "error_class": "E9",
                "expected_amount": str(settlement.amount_settled),
                "true_resolution": (
                    "settlement genuinely absent from the bank statement - "
                    "raise with Razorpay support"
                ),
            }

    def _build_cutover_boundary(self) -> None:
        """Clean settlements pinned to the fee-schedule boundary (12.1).

        One lands exactly on the cutover (v2), one the day before (v1). The
        boundary is where an off-by-one-day bug lives, and without records
        sitting on it the verifier's schedule check is never exercised.
        These are still E1 - clean matches that happen to sit on the edge -
        and their payments come out of E1's allocation.
        """
        per_settlement = CUTOVER_PAYMENTS // 2
        for settled_at in (
            FEE_CUTOVER_DATE,
            previous_business_day(FEE_CUTOVER_DATE),
        ):
            group = [
                self._make_payment(settled_at - timedelta(days=3), method="card")
                for _ in range(per_settlement)
            ]
            settlement = self._settle(group, settled_at)
            credit = self._credit(settlement, settlement.amount_settled)
            self._commit(
                group, settlement, [credit], "E1",
                f"clean match on the {settlement.fee_schedule} boundary",
            )

    def _build_foreign_credits(self) -> None:
        """E10 - a bank credit with no Razorpay counterpart.

        Each candidate is checked against every settlement under both fee
        schedules and redrawn on collision. Constructing something to be
        unmatchable is not the same as it being unmatchable.
        """
        by_id = self.batch.payments_by_id()
        for _ in range(E10_COUNT):
            for _attempt in range(200):
                candidate = BankTxn(
                    bank_txn_id=self._next_id("bt"),
                    txn_date=self._business_day(self._capture_date()),
                    value_date=self._business_day(self._capture_date()),
                    narration=f"NEFT-INWARD-{self._utr()}-VENDOR REFUND",
                    utr=None,
                    credit_amount=self._amount(low=1_000, high=90_000),
                )
                if verify_unresolvable(candidate, self.batch.settlements, by_id):
                    break
            else:  # pragma: no cover - 200 consecutive collisions is implausible
                raise RuntimeError(
                    "could not draw an unresolvable E10 amount in 200 attempts"
                )

            self.batch.bank_txns.append(candidate)
            self._record(
                candidate, "E10", [],
                "foreign credit with no Razorpay counterpart - escalate",
            )

    # -- orchestration -----------------------------------------------------

    def build(self) -> GeneratedBatch:
        """Generate the full batch. Build order is fixed; see module docs."""
        self._build_simple(
            "E1", "clean straight-through match", reserve=CUTOVER_PAYMENTS
        )
        self._build_simple("E2", "settlement lag: credit lands T+3", lag_days=3)
        self._build_refunds()
        self._build_simple(
            "E4",
            "fee schedule v1 applied to a settlement the cutover puts on v2",
            schedule=FEE_SCHEDULES["v1"],
        )
        self._build_splits()
        self._build_duplicate_utr()
        self._build_simple(
            "E7", "rounding drift within tolerance", rounding_drift=True
        )
        self._build_simple(
            "E8", "no parseable UTR in the narration", truncate_utr=True
        )
        self._build_missing_settlements()
        self._build_cutover_boundary()
        self._build_foreign_credits()

        self.batch.missing_settlements = self._missing
        self.batch.orders = self._pending_orders
        self.batch.bank_txns.sort(key=lambda b: (b.value_date, b.bank_txn_id))
        self._stamp_expected_settlements()
        return self.batch

    def _stamp_expected_settlements(self) -> None:
        """Fill each order's expected settlement from its payment's leg.

        Left blank where the payment never settled, so an E9 order shows an
        expectation that was never met rather than a fabricated figure.
        """
        by_order = {p.order_id: p for p in self.batch.payments}
        for order in self.batch.orders:
            payment = by_order.get(order.order_id)
            if payment is not None and payment.amount_settled is not None:
                order.expected_settlement = payment.amount_settled


def generate(seed: int = DEMO_SEED) -> GeneratedBatch:
    """Build one batch for ``seed``."""
    return Generator(seed).build()


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``python -m closo.generator --seed 42 --out DIR``."""
    parser = argparse.ArgumentParser(description="Generate Closo's synthetic batch")
    parser.add_argument("--seed", type=int, default=DEMO_SEED)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    out_dir = args.out or (DATA_DIR / f"seed_{args.seed}")
    batch = generate(args.seed)
    write_batch(batch, out_dir, args.seed)

    counts = batch.class_counts()
    print(f"seed {args.seed} -> {out_dir}")
    print(
        f"  {len(batch.payments)} payments, {len(batch.settlements)} settlements, "
        f"{len(batch.bank_txns)} bank credits, {len(batch.orders)} orders"
    )
    print("  " + "  ".join(f"{k}={counts[k]}" for k in sorted(counts)))
    print(f"  {len(batch.missing_settlements)} settlements with no bank credit (E9)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
