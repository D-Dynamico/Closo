"""Layer 1 - the deterministic matching cascade (CLAUDE.md 6).

Three passes over a shrinking pool. Each consumes only what the previous
one left, and nothing consumed is ever reconsidered:

* **Pass A** joins bank credit to settlement on a normalized UTR and
  demands the amounts agree to the paisa.
* **Pass B** matches on exact amount inside a T+3 business-day window,
  for whatever had no usable UTR.
* **Pass C** recomputes the expected settlement from the constituent
  payments and matches within a two-rupee tolerance.

**Ambiguity is an exception, never a guess** (11.6). Every tie-break
opportunity in here is deliberately declined: two bank rows sharing a
UTR, two settlements at the same amount in overlapping windows, two
credits within tolerance of one settlement. A wrong match is far more
expensive than an unmatched row, because an unmatched row is an honest
exception and a wrong match is a confident lie that reconciles.

No LLM imports, enforced by test (11.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from closo.config import (
    PASS_C_TOLERANCE,
    SETTLEMENT_WINDOW_BUSINESS_DAYS,
    ZERO,
    active_schedule,
    add_business_days,
    normalize_utr,
)
from closo.schemas import BankTxn, ExceptionItem, MatchRecord, Payment, Settlement
from closo.taxonomy import GeneratedBatch, settlement_math


@dataclass
class Decision:
    """One audit event. Stage 4 persists these; Layer 1 only records them."""

    layer: str
    record_ref: str
    event_type: str
    payload: dict = field(default_factory=dict)


@dataclass
class Layer1Result:
    """What the cascade produced."""

    matches: list[MatchRecord] = field(default_factory=list)
    exceptions: list[ExceptionItem] = field(default_factory=list)
    events: list[Decision] = field(default_factory=list)
    total_bank_txns: int = 0

    @property
    def match_rate(self) -> float:
        """Fraction of bank credits matched deterministically."""
        if not self.total_bank_txns:
            return 0.0
        return len(self.matches) / self.total_bank_txns

    def matched_bank_ids(self) -> set[str]:
        return {m.bank_txn_id for m in self.matches}


class Layer1Matcher:
    """Runs the cascade over one batch."""

    def __init__(self, batch: GeneratedBatch) -> None:
        self.batch = batch
        self.payments: dict[str, Payment] = batch.payments_by_id()
        self.bank: dict[str, BankTxn] = {b.bank_txn_id: b for b in batch.bank_txns}
        self.settlements: dict[str, Settlement] = {
            s.settlement_id: s for s in batch.settlements
        }
        self.open_bank: set[str] = set(self.bank)
        self.open_settlements: set[str] = set(self.settlements)
        self.result = Layer1Result(total_bank_txns=len(self.bank))

        #: Normalized UTR per bank credit. Derived from the narration, never
        #: read from a column - the statement does not carry one (6).
        self.parsed_utr: dict[str, str | None] = {
            txn_id: normalize_utr(txn.narration) for txn_id, txn in self.bank.items()
        }

        #: Bank credits whose UTR found a settlement in Pass A, whether or
        #: not the amounts then agreed. Lets the final sweep tell "no such
        #: settlement exists" apart from "it exists but the money differs",
        #: which are different problems and need different escalation notes.
        self.utr_joined: set[str] = set()

        #: Every UTR seen anywhere in the statement. A settlement whose UTR
        #: appears here is not missing - it is part of some other open
        #: exception - so sweeping it as "no bank credit" would be false.
        self.utrs_in_statement: set[str] = {
            utr for utr in self.parsed_utr.values() if utr is not None
        }

    # -- bookkeeping -------------------------------------------------------

    def _log(self, ref: str, event_type: str, **payload: object) -> None:
        self.result.events.append(
            Decision(layer="layer1", record_ref=ref, event_type=event_type,
                     payload=dict(payload))
        )

    def _match(
        self,
        bank_txn_id: str,
        settlement_id: str,
        pass_used: str,
        tolerance: Decimal = ZERO,
    ) -> None:
        """Record a match and remove both sides from the pool."""
        settlement = self.settlements[settlement_id]
        self.result.matches.append(
            MatchRecord(
                bank_txn_id=bank_txn_id,
                settlement_id=settlement_id,
                payment_ids=list(settlement.payment_ids),
                pass_used=pass_used,
                tolerance_applied=tolerance,
            )
        )
        self.open_bank.discard(bank_txn_id)
        self.open_settlements.discard(settlement_id)
        self._log(
            bank_txn_id, "matched", settlement_id=settlement_id,
            pass_used=pass_used, tolerance=str(tolerance),
        )

    def _except(
        self,
        reason: str,
        detail: str,
        bank_txn_id: str | None = None,
        settlement_id: str | None = None,
        consume: bool = True,
    ) -> None:
        """Raise an exception for the investigator and optionally consume."""
        item = ExceptionItem(
            exception_id=f"EX-{len(self.result.exceptions) + 1:03d}",
            bank_txn_id=bank_txn_id,
            settlement_id=settlement_id,
            reason=reason,  # type: ignore[arg-type]
            detail=detail,
        )
        self.result.exceptions.append(item)
        if consume:
            if bank_txn_id:
                self.open_bank.discard(bank_txn_id)
            if settlement_id:
                self.open_settlements.discard(settlement_id)
        self._log(bank_txn_id or settlement_id or "?", "exception", reason=reason,
                  detail=detail, exception_id=item.exception_id)

    def _accept_unique(
        self, candidates: dict[str, list[str]], pass_used: str,
        tolerances: dict[tuple[str, str], Decimal] | None = None,
    ) -> None:
        """Accept only unambiguous 1:1 pairings; everything else is a tie.

        A bank credit with two candidate settlements is ambiguous, and so is
        a settlement claimed by two credits - the second case is easy to
        miss and would let one settlement be matched twice.
        """
        claims: dict[str, list[str]] = {}
        for bank_id, settlement_ids in candidates.items():
            for settlement_id in settlement_ids:
                claims.setdefault(settlement_id, []).append(bank_id)

        for bank_id, settlement_ids in candidates.items():
            if len(settlement_ids) != 1:
                self._except(
                    "ambiguous_tie",
                    f"{len(settlement_ids)} candidate settlements in {pass_used}: "
                    + ", ".join(sorted(settlement_ids)),
                    bank_txn_id=bank_id,
                )
                continue

            settlement_id = settlement_ids[0]
            if len(claims[settlement_id]) != 1:
                self._except(
                    "ambiguous_tie",
                    f"settlement {settlement_id} is claimed by "
                    f"{len(claims[settlement_id])} credits in {pass_used}",
                    bank_txn_id=bank_id,
                )
                continue

            tolerance = (tolerances or {}).get((bank_id, settlement_id), ZERO)
            self._match(bank_id, settlement_id, pass_used, tolerance)

    # -- Pass A ------------------------------------------------------------

    def pass_a(self) -> None:
        """Exact UTR join, with the amounts required to agree exactly.

        Built as a pandas join because that is genuinely what this is. The
        amount comparison stays in Decimal - pandas would happily compare
        these as objects, but the intent is clearer stated once here.
        """
        bank_frame = pd.DataFrame(
            [
                {"bank_txn_id": txn_id, "utr": self.parsed_utr[txn_id]}
                for txn_id in sorted(self.open_bank)
                if self.parsed_utr[txn_id] is not None
            ],
            columns=["bank_txn_id", "utr"],
        )
        settlement_frame = pd.DataFrame(
            [
                {"settlement_id": sid, "utr": self.settlements[sid].utr}
                for sid in sorted(self.open_settlements)
            ],
            columns=["settlement_id", "utr"],
        )
        if bank_frame.empty or settlement_frame.empty:
            return

        joined = bank_frame.merge(settlement_frame, on="utr", how="inner")

        for utr, rows in joined.groupby("utr"):
            bank_ids = sorted(set(rows["bank_txn_id"]))
            settlement_ids = sorted(set(rows["settlement_id"]))

            if len(bank_ids) > 1 or len(settlement_ids) > 1:
                # Error class E6. Refusing here is the point: a UTR join
                # yielding two candidates has no correct answer, and picking
                # one produces a match that reconciles and is wrong.
                for bank_id in bank_ids:
                    self._except(
                        "duplicate_utr",
                        f"UTR {utr} matches {len(bank_ids)} credits and "
                        f"{len(settlement_ids)} settlements",
                        bank_txn_id=bank_id,
                    )
                for settlement_id in settlement_ids:
                    self.open_settlements.discard(settlement_id)
                continue

            bank_id, settlement_id = bank_ids[0], settlement_ids[0]
            self.utr_joined.add(bank_id)
            credit = self.bank[bank_id].credit_amount
            recorded = self.settlements[settlement_id].amount_settled
            if credit == recorded:
                self._match(bank_id, settlement_id, "A_utr_exact")
            else:
                # Falls through to a later pass rather than becoming an
                # exception here (12.2): the UTR agreeing while the
                # amounts do not is exactly what Pass C exists to explain.
                self._log(
                    bank_id, "pass_a_amount_mismatch",
                    settlement_id=settlement_id, credit=str(credit),
                    recorded=str(recorded), difference=str(credit - recorded),
                )

    # -- Pass B ------------------------------------------------------------

    def pass_b(self) -> None:
        """Exact amount inside a T+3 business-day window.

        The window skips weekends. A naive three-calendar-day window
        silently rejects every Friday settlement, which surfaces as an
        unexplained dip in match rate rather than as an error.
        """
        candidates: dict[str, list[str]] = {}
        for bank_id in sorted(self.open_bank):
            credit = self.bank[bank_id]
            hits = [
                sid
                for sid in sorted(self.open_settlements)
                if self.settlements[sid].amount_settled == credit.credit_amount
                and self._in_window(self.settlements[sid], credit)
            ]
            if hits:
                candidates[bank_id] = hits

        self._accept_unique(candidates, "B_amount_date_window")

    def _in_window(self, settlement: Settlement, credit: BankTxn) -> bool:
        """True if the credit landed within T+3 business days of settling."""
        latest = add_business_days(
            settlement.settled_at, SETTLEMENT_WINDOW_BUSINESS_DAYS
        )
        return settlement.settled_at <= credit.value_date <= latest

    # -- Pass C ------------------------------------------------------------

    def expected_net(self, settlement: Settlement) -> Decimal | None:
        """Recompute a settlement's net from its payments.

        Uses the schedule active at ``settled_at``, not the one recorded on
        the settlement. Trusting the recorded schedule would make Pass C
        agree with a payout that ran on a stale one, which is error class
        E4 and must not resolve here.
        """
        members = [
            self.payments[pid]
            for pid in settlement.payment_ids
            if pid in self.payments
        ]
        if not members:
            return None
        _, _, _, net = settlement_math(members, active_schedule(settlement.settled_at))
        return net

    def pass_c(self) -> None:
        """Netting recomputation within a two-rupee tolerance."""
        candidates: dict[str, list[str]] = {}
        tolerances: dict[tuple[str, str], Decimal] = {}

        for bank_id in sorted(self.open_bank):
            credit = self.bank[bank_id].credit_amount
            hits = []
            for sid in sorted(self.open_settlements):
                net = self.expected_net(self.settlements[sid])
                if net is None:
                    continue
                drift = abs(credit - net)
                if drift <= PASS_C_TOLERANCE:
                    hits.append(sid)
                    tolerances[(bank_id, sid)] = drift
            if hits:
                candidates[bank_id] = hits

        self._accept_unique(candidates, "C_netting_recompute", tolerances)

    # -- residue -----------------------------------------------------------

    def sweep(self) -> None:
        """Everything still open becomes an honest exception.

        Both sides. An unmatched settlement with no bank credit is error
        class E9 - money that genuinely never arrived - and it has no bank
        transaction to hang off, so it would vanish entirely if only the
        bank side were swept.
        """
        for bank_id in sorted(self.open_bank):
            credit = self.bank[bank_id]
            utr = self.parsed_utr[bank_id]
            if utr is None:
                reason, detail = (
                    "no_utr_in_narration",
                    f"no parseable UTR in {credit.narration!r} and no amount match",
                )
            elif bank_id not in self.utr_joined:
                # No settlement carries this UTR at all. Distinct from an
                # amount disagreement, and it is the shape of a credit that
                # came from somewhere other than Razorpay (E10).
                reason, detail = (
                    "no_utr_match",
                    f"UTR {utr} appears on no settlement",
                )
            else:
                reason, detail = (
                    "outside_tolerance",
                    f"UTR {utr} matches a settlement but no fee schedule "
                    f"reproduces {credit.credit_amount}",
                )
            self._except(reason, detail, bank_txn_id=bank_id)

        for settlement_id in sorted(self.open_settlements):
            settlement = self.settlements[settlement_id]
            if settlement.utr in self.utrs_in_statement:
                # A credit carrying this UTR does exist, it just did not
                # reconcile. Reporting the settlement as missing too would
                # split one problem into two exceptions and tell the
                # investigator something plainly untrue.
                self._log(
                    settlement_id, "settlement_open_but_credit_present",
                    utr=settlement.utr,
                    amount_settled=str(settlement.amount_settled),
                )
                continue
            self._except(
                "no_counterpart",
                f"settlement of {settlement.amount_settled} on "
                f"{settlement.settled_at} has no bank credit at all",
                settlement_id=settlement_id,
            )

    def run(self) -> Layer1Result:
        """Execute the cascade and return the result."""
        self._log("batch", "layer1_started", bank_txns=len(self.bank),
                  settlements=len(self.settlements))
        self.pass_a()
        self.pass_b()
        self.pass_c()
        self.sweep()
        self._log(
            "batch", "layer1_finished",
            matched=len(self.result.matches),
            exceptions=len(self.result.exceptions),
            match_rate=round(self.result.match_rate, 4),
        )
        return self.result


def run_layer1(batch: GeneratedBatch) -> Layer1Result:
    """Run the deterministic cascade over ``batch``."""
    return Layer1Matcher(batch).run()
