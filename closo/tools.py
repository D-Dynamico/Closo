"""Read-only tools the investigator may call (ARCHITECTURE 7.1).

Three rules shape every function here.

**Errors are returned, never raised.** An unknown id produces
``{"error": "not_found", ...}``. The model has to be able to recover from a
wrong guess and try something else; an exception would kill the exception's
investigation over a typo.

**Amounts are strings, never floats.** JSON has no Decimal, and a float
would reintroduce exactly the rounding error the rest of the codebase works
to avoid - in the one place the model then reasons from.

**The tool does the arithmetic, never the model.**
:func:`compute_expected_settlement` returns the full breakdown so every
number in a verdict traces back to something computed here. A figure the
model produced on its own cannot be reproduced, and the verifier will
reject it.

Nothing in this module writes. There is no connection, no cursor, no path
that could mutate a record; audit writes go through ``audit.py`` alone.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from closo.config import (
    FEE_CUTOVER_DATE,
    FEE_SCHEDULES,
    active_schedule,
    gst_on,
    money,
    normalize_utr,
)
from closo.taxonomy import GeneratedBatch, settlement_math

MAX_RESULTS = 50


def _error(kind: str, detail: str) -> dict[str, Any]:
    """A structured failure the model can read and route around."""
    return {"error": kind, "detail": detail}


def _parse_amount(value: Any, field: str) -> tuple[Decimal | None, dict | None]:
    """Coerce a model-supplied amount, refusing floats and nonsense."""
    if value is None:
        return None, None
    if isinstance(value, float):
        return None, _error("bad_argument", f"{field} must be a string, not a float")
    try:
        return money(value), None
    except (InvalidOperation, ValueError, TypeError):
        return None, _error("bad_argument", f"{field} is not a valid amount: {value!r}")


def _parse_date(value: Any, field: str) -> tuple[date | None, dict | None]:
    if value is None:
        return None, None
    if isinstance(value, date):
        return value, None
    try:
        return date.fromisoformat(str(value)), None
    except ValueError:
        return None, _error("bad_argument", f"{field} is not an ISO date: {value!r}")


class ToolBox:
    """The tool surface, bound to one batch of records.

    Constructed per run and handed to the investigator. Holds no writable
    handle to anything.
    """

    def __init__(self, batch: GeneratedBatch) -> None:
        self._payments = {p.payment_id: p for p in batch.payments}
        self._settlements = {s.settlement_id: s for s in batch.settlements}
        self._bank = {b.bank_txn_id: b for b in batch.bank_txns}
        self._orders = {o.order_id: o for o in batch.orders}

    # -- payments ----------------------------------------------------------

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        """One payment, with its settlement leg if it has one."""
        payment = self._payments.get(payment_id)
        if payment is None:
            return _error("not_found", f"no payment {payment_id!r}")
        return self._payment_view(payment)

    def _payment_view(self, payment) -> dict[str, Any]:
        return {
            "payment_id": payment.payment_id,
            "order_id": payment.order_id,
            "amount_gross": str(payment.amount_gross),
            "refund_amount": str(payment.refund_amount),
            "net_of_refund": str(payment.net_of_refund),
            "method": payment.method,
            "status": payment.status,
            "captured_at": payment.captured_at.isoformat(),
            "settlement_id": payment.settlement_id,
            "settled_at": payment.settled_at.isoformat() if payment.settled_at else None,
            "fee_mdr": str(payment.fee_mdr) if payment.fee_mdr is not None else None,
            "fee_gst": str(payment.fee_gst) if payment.fee_gst is not None else None,
            "amount_settled": (
                str(payment.amount_settled)
                if payment.amount_settled is not None
                else None
            ),
        }

    def list_payments(
        self,
        amount_min: str | None = None,
        amount_max: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        method: str | None = None,
    ) -> dict[str, Any]:
        """Payments matching a filter. All bounds inclusive.

        Inclusive on purpose: the investigator's most useful query is an
        exact amount, expressed as min == max, and an exclusive bound would
        silently return nothing for the single most common lookup.
        """
        low, error = _parse_amount(amount_min, "amount_min")
        if error:
            return error
        high, error = _parse_amount(amount_max, "amount_max")
        if error:
            return error
        start, error = _parse_date(date_from, "date_from")
        if error:
            return error
        end, error = _parse_date(date_to, "date_to")
        if error:
            return error

        hits = []
        for payment in self._payments.values():
            if low is not None and payment.amount_gross < low:
                continue
            if high is not None and payment.amount_gross > high:
                continue
            if start is not None and payment.captured_at < start:
                continue
            if end is not None and payment.captured_at > end:
                continue
            if method is not None and payment.method != method:
                continue
            hits.append(payment)

        hits.sort(key=lambda p: p.payment_id)
        return {
            "count": len(hits),
            "truncated": len(hits) > MAX_RESULTS,
            "payments": [self._payment_view(p) for p in hits[:MAX_RESULTS]],
        }

    def get_refunds(self, payment_id: str) -> dict[str, Any]:
        """Refunds against a payment. An empty list is a real answer.

        "No refunds" is the finding that rules out a whole hypothesis, so it
        is returned as data rather than as an error.
        """
        payment = self._payments.get(payment_id)
        if payment is None:
            return _error("not_found", f"no payment {payment_id!r}")
        if payment.refund_amount <= 0:
            return {"payment_id": payment_id, "count": 0, "refunds": []}
        return {
            "payment_id": payment_id,
            "count": 1,
            "refunds": [
                {
                    "amount": str(payment.refund_amount),
                    "status": payment.status,
                    "leaves_net": str(payment.net_of_refund),
                }
            ],
        }

    # -- settlements -------------------------------------------------------

    def get_settlement(self, settlement_id: str) -> dict[str, Any]:
        settlement = self._settlements.get(settlement_id)
        if settlement is None:
            return _error("not_found", f"no settlement {settlement_id!r}")
        return self._settlement_view(settlement)

    def _settlement_view(self, settlement) -> dict[str, Any]:
        return {
            "settlement_id": settlement.settlement_id,
            "utr": settlement.utr,
            "settled_at": settlement.settled_at.isoformat(),
            "payment_ids": list(settlement.payment_ids),
            "amount_gross": str(settlement.amount_gross),
            "fee_mdr": str(settlement.fee_mdr),
            "fee_gst": str(settlement.fee_gst),
            "amount_settled": str(settlement.amount_settled),
            "fee_schedule_recorded": settlement.fee_schedule,
            "fee_schedule_active_on_that_date": active_schedule(
                settlement.settled_at
            ).name,
        }

    def list_settlements(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        amount_min: str | None = None,
        amount_max: str | None = None,
    ) -> dict[str, Any]:
        """Settlements matching a filter. All bounds inclusive."""
        start, error = _parse_date(date_from, "date_from")
        if error:
            return error
        end, error = _parse_date(date_to, "date_to")
        if error:
            return error
        low, error = _parse_amount(amount_min, "amount_min")
        if error:
            return error
        high, error = _parse_amount(amount_max, "amount_max")
        if error:
            return error

        hits = []
        for settlement in self._settlements.values():
            if start is not None and settlement.settled_at < start:
                continue
            if end is not None and settlement.settled_at > end:
                continue
            if low is not None and settlement.amount_settled < low:
                continue
            if high is not None and settlement.amount_settled > high:
                continue
            hits.append(settlement)

        hits.sort(key=lambda s: s.settlement_id)
        return {
            "count": len(hits),
            "truncated": len(hits) > MAX_RESULTS,
            "settlements": [self._settlement_view(s) for s in hits[:MAX_RESULTS]],
        }

    # -- arithmetic --------------------------------------------------------

    def compute_expected_settlement(
        self, payment_ids: list[str], fee_schedule: str
    ) -> dict[str, Any]:
        """Recompute a settlement net from payments under one fee schedule.

        **This is where all settlement arithmetic happens.** Every number in
        a verdict's ``arithmetic`` block must come from here, because the
        verifier recomputes the same way and rejects anything it cannot
        reproduce. A model doing its own sums produces figures that look
        right and cannot be checked.

        One schedule per call, deliberately. Mixing them inside a single
        computation would hide which rate applied to which payment, and that
        distinction is the entire content of error class E4.
        """
        if not payment_ids:
            return _error("bad_argument", "payment_ids is empty")
        if fee_schedule not in FEE_SCHEDULES:
            return _error(
                "bad_argument",
                f"unknown fee schedule {fee_schedule!r}; expected one of "
                f"{sorted(FEE_SCHEDULES)}",
            )

        missing = [pid for pid in payment_ids if pid not in self._payments]
        if missing:
            return _error("not_found", f"no such payment(s): {sorted(missing)}")

        duplicates = {pid for pid in payment_ids if payment_ids.count(pid) > 1}
        if duplicates:
            return _error(
                "bad_argument",
                f"payment(s) cited more than once: {sorted(duplicates)}",
            )

        schedule = FEE_SCHEDULES[fee_schedule]
        members = [self._payments[pid] for pid in payment_ids]
        gross, mdr, gst, net = settlement_math(members, schedule)

        return {
            "fee_schedule": fee_schedule,
            "payment_ids": list(payment_ids),
            "gross": str(gross),
            "mdr": str(mdr),
            "gst": str(gst),
            "net": str(net),
            "breakdown": [
                {
                    "payment_id": p.payment_id,
                    "method": p.method,
                    "gross": str(p.amount_gross),
                    "refund": str(p.refund_amount),
                    "mdr": str(schedule.mdr_for(p.method, p.amount_gross)),
                    "gst": str(gst_on(schedule.mdr_for(p.method, p.amount_gross))),
                }
                for p in members
            ],
        }

    # -- lookups -----------------------------------------------------------

    def check_duplicate_utr(self, utr: str) -> dict[str, Any]:
        """How many settlements and bank credits carry a UTR.

        Bank-side counts come from re-parsing narrations, because that is
        what the statement actually contains and what Pass A saw.
        """
        normalized = normalize_utr(utr) or utr.strip().upper()
        settlements = sorted(
            s.settlement_id for s in self._settlements.values() if s.utr == normalized
        )
        credits = sorted(
            b.bank_txn_id
            for b in self._bank.values()
            if normalize_utr(b.narration) == normalized
        )
        return {
            "utr": normalized,
            "settlement_count": len(settlements),
            "settlement_ids": settlements,
            "bank_txn_count": len(credits),
            "bank_txn_ids": credits,
            "is_duplicate": len(settlements) > 1 or len(credits) > 1,
        }

    def get_bank_txn(self, bank_txn_id: str) -> dict[str, Any]:
        credit = self._bank.get(bank_txn_id)
        if credit is None:
            return _error("not_found", f"no bank transaction {bank_txn_id!r}")
        return {
            "bank_txn_id": credit.bank_txn_id,
            "value_date": credit.value_date.isoformat(),
            "narration": credit.narration,
            "utr_parsed_from_narration": normalize_utr(credit.narration),
            "credit_amount": str(credit.credit_amount),
        }

    def get_fee_schedules(self) -> dict[str, Any]:
        """Both schedules and the cutover date."""
        return {
            "cutover_date": FEE_CUTOVER_DATE.isoformat(),
            "rule": "settlements on or after the cutover use v2; before it, v1",
            "schedules": {
                name: {
                    method: {
                        "percent_of_gross": str(fee.percent),
                        "flat_fee": str(fee.flat),
                    }
                    for method, fee in schedule.fees.items()
                }
                for name, schedule in FEE_SCHEDULES.items()
            },
            "gst": "18% of MDR, quantized per fee row",
        }
