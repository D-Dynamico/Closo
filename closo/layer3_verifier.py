"""Layer 3 - the independent verifier (ARCHITECTURE 8).

The load-bearing module. Closo's whole claim is that verification capacity,
not generation speed, is the bottleneck in finance ops - and that claim is
only worth anything if something here can actually overrule the model.

**The verdict's own arithmetic block is a claim, not evidence.** Every
figure is recomputed from raw records. A block that is internally
consistent, sums correctly, and matches the bank credit still fails if the
cited payments' real amounts disagree with it. That is the whole point: a
plausible wrong answer is the failure mode an LLM produces, and checking
its work against itself would catch none of them.

Pure functions over loaded records. No LLM imports, enforced by test (11.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from closo.config import FEE_SCHEDULES, PASS_C_TOLERANCE, ZERO, active_schedule, money
from closo.schemas import (
    CheckResult,
    Verdict,
    VerifierResult,
)
from closo.taxonomy import GeneratedBatch, settlement_math

#: A verdict may claim a small rounding adjustment, because real settlements
#: carry one. It is bounded at the Pass C tolerance so it cannot become a
#: free parameter that absorbs any discrepancy - an unbounded rounding field
#: would let a verdict reconcile anything to anything.
MAX_CLAIMED_ROUNDING = PASS_C_TOLERANCE


@dataclass
class Verifier:
    """Re-checks verdicts against raw records.

    Stateful in one respect only: it remembers which payments earlier
    verdicts consumed, so a payment cannot be spent twice across a run.
    """

    batch: GeneratedBatch
    consumed: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.payments = {p.payment_id: p for p in self.batch.payments}
        self.settlements = {s.settlement_id: s for s in self.batch.settlements}
        self.bank = {b.bank_txn_id: b for b in self.batch.bank_txns}

    # -- entry point -------------------------------------------------------

    def verify(self, verdict: Verdict) -> VerifierResult:
        """Check one verdict. The only public method.

        Returns a result carrying every check performed, passed or failed,
        so the drill-down screen can show the reasoning rather than a
        verdict about the verdict.
        """
        result = VerifierResult(exception_id=verdict.exception_id, passed=False)

        if verdict.confidence == "unresolvable":
            # Nothing to verify. Giving up is a valid outcome and is not a
            # failure of verification - it never reached the verifier's job.
            result.effective_confidence = "unresolvable"
            result.checks.append(
                CheckResult(check="existence", passed=True,
                            detail="unresolvable verdict; nothing claimed")
            )
            return result

        if verdict.proposed_match is None:
            return self._fail(
                result, "malformed_verdict", "existence",
                f"confidence is {verdict.confidence!r} but no match was proposed",
            )

        checks = (
            self._check_existence,
            self._check_refund_consistency,
            self._check_exclusivity,
            self._check_fee_schedule,
            self._check_arithmetic,
        )
        for check in checks:
            if not check(verdict, result):
                return result

        result.passed = True
        result.consumed_payment_ids = list(verdict.proposed_match.payment_ids)

        # The verifier may demote confidence, never promote it. A verdict
        # the model called `probable` stays probable however clean the math.
        if result.schedule_anomaly or verdict.confidence == "probable":
            result.effective_confidence = "probable"
            result.needs_human_signoff = True
        else:
            result.effective_confidence = "resolved"

        return result

    def commit(self, result: VerifierResult) -> None:
        """Mark a passing verdict's payments as spent."""
        if result.passed:
            self.consumed.update(result.consumed_payment_ids)

    # -- checks ------------------------------------------------------------

    def _fail(
        self, result: VerifierResult, reason: str, check: str, detail: str
    ) -> VerifierResult:
        result.passed = False
        result.rejection_reason = reason  # type: ignore[assignment]
        result.effective_confidence = "unresolvable"
        result.checks.append(
            CheckResult(check=check, passed=False, detail=detail)  # type: ignore[arg-type]
        )
        return result

    def _check_existence(self, verdict: Verdict, result: VerifierResult) -> bool:
        """Every cited id must exist. A phantom id is an invented record."""
        match = verdict.proposed_match
        assert match is not None

        missing: list[str] = [
            pid for pid in match.payment_ids if pid not in self.payments
        ]
        credit_ids = [match.bank_txn_id, *match.extra_bank_txn_ids]
        missing += [bid for bid in credit_ids if bid not in self.bank]

        if missing:
            self._fail(
                result, "phantom_reference", "existence",
                f"cited record(s) do not exist: {sorted(missing)}",
            )
            return False

        result.checks.append(
            CheckResult(
                check="existence", passed=True,
                detail=f"{len(match.payment_ids)} payment(s) and "
                       f"{len(credit_ids)} credit(s) all exist",
            )
        )
        return True

    def _check_refund_consistency(
        self, verdict: Verdict, result: VerifierResult
    ) -> bool:
        """A hypothesis mentioning a refund must be backed by one.

        Checked against the records, not against the verdict's own prose. An
        invented refund is the most natural way for a model to explain a
        credit that came up short, so it is the one to look for hardest.
        """
        match = verdict.proposed_match
        assert match is not None

        text = f"{verdict.hypothesis}".lower()
        mentions_refund = "refund" in text
        refunded = [
            pid for pid in match.payment_ids if self.payments[pid].refund_amount > 0
        ]

        if mentions_refund and not refunded:
            self._fail(
                result, "refund_fabrication", "refund_consistency",
                "hypothesis cites a refund but no cited payment carries one",
            )
            return False

        result.checks.append(
            CheckResult(
                check="refund_consistency", passed=True,
                detail=(
                    f"{len(refunded)} cited payment(s) carry a refund"
                    if refunded else "no refund claimed and none present"
                ),
            )
        )
        return True

    def _check_exclusivity(self, verdict: Verdict, result: VerifierResult) -> bool:
        """No payment may be spent by two resolutions.

        Without this, two exceptions can each 'resolve' using the same
        payment and the money view double-counts - and both resolutions look
        individually correct, which is what makes it worth checking.
        """
        match = verdict.proposed_match
        assert match is not None

        clash = sorted(set(match.payment_ids) & self.consumed)
        if clash:
            self._fail(
                result, "exclusivity_violation", "exclusivity",
                f"payment(s) already consumed by an earlier resolution: {clash}",
            )
            return False

        repeated = sorted(
            {pid for pid in match.payment_ids if match.payment_ids.count(pid) > 1}
        )
        if repeated:
            self._fail(
                result, "exclusivity_violation", "exclusivity",
                f"payment(s) cited twice within one verdict: {repeated}",
            )
            return False

        result.checks.append(
            CheckResult(check="exclusivity", passed=True,
                        detail="no cited payment was previously consumed")
        )
        return True

    def _check_fee_schedule(self, verdict: Verdict, result: VerifierResult) -> bool:
        """The cited schedule must exist; if inactive, cap rather than fail.

        Error class E4 *is* a payout on a superseded schedule, so the only
        correct verdict cites the inactive one. Failing that outright would
        make E4 unresolvable by construction. Instead the arithmetic still
        has to reproduce the credit exactly, and the verdict is capped at
        `probable` with the anomaly recorded (8.1).

        Closo can prove which schedule produces the amount that moved. It
        cannot know whether applying it was authorised, and a machine should
        not quietly decide that - it should hand a human the question.
        """
        match = verdict.proposed_match
        assert match is not None

        if match.fee_schedule not in FEE_SCHEDULES:
            self._fail(
                result, "wrong_fee_schedule", "fee_schedule",
                f"unknown fee schedule {match.fee_schedule!r}",
            )
            return False

        settlement_date = self._settlement_date(match.payment_ids)
        if settlement_date is None:
            result.checks.append(
                CheckResult(check="fee_schedule", passed=True,
                            detail="no settlement date on cited payments")
            )
            return True

        expected = active_schedule(settlement_date).name
        if match.fee_schedule != expected:
            result.schedule_anomaly = (
                f"cited {match.fee_schedule} but {expected} was active on "
                f"{settlement_date.isoformat()}"
            )
            result.checks.append(
                CheckResult(
                    check="fee_schedule", passed=True,
                    detail=f"{result.schedule_anomaly} - math still checked in "
                           f"full, verdict capped to probable",
                )
            )
            return True

        result.checks.append(
            CheckResult(check="fee_schedule", passed=True,
                        detail=f"{expected} was active on {settlement_date}")
        )
        return True

    def _settlement_date(self, payment_ids: list[str]):
        """The settlement date shared by the cited payments, if any."""
        dates = {
            self.payments[pid].settled_at
            for pid in payment_ids
            if self.payments[pid].settled_at is not None
        }
        return dates.pop() if len(dates) == 1 else None

    def _check_arithmetic(self, verdict: Verdict, result: VerifierResult) -> bool:
        """Recompute from raw records and require an exact match.

        The verdict's arithmetic block is not consulted for the answer, only
        compared against it. A block that is internally consistent and
        matches the credit still fails when the cited payments' real gross
        differs - which is exactly the shape of a confident wrong answer.
        """
        match = verdict.proposed_match
        assert match is not None

        claimed_rounding = match.arithmetic.rounding
        if abs(claimed_rounding) > MAX_CLAIMED_ROUNDING:
            self._fail(
                result, "arithmetic_mismatch", "arithmetic",
                f"claimed rounding of {claimed_rounding} exceeds the "
                f"{MAX_CLAIMED_ROUNDING} bound; rounding is not a free parameter",
            )
            return False

        schedule = FEE_SCHEDULES[match.fee_schedule]
        members = [self.payments[pid] for pid in match.payment_ids]
        gross, mdr, gst, net = settlement_math(members, schedule, claimed_rounding)

        credited = money(
            sum(
                (
                    self.bank[bid].credit_amount
                    for bid in (match.bank_txn_id, *match.extra_bank_txn_ids)
                ),
                ZERO,
            )
        )

        if net != credited:
            self._fail(
                result, "arithmetic_mismatch", "arithmetic",
                f"recomputed {net} from raw records under {match.fee_schedule}, "
                f"but the bank credited {credited} (difference {net - credited})",
            )
            return False

        block = match.arithmetic
        drift = self._block_drift(block, gross, mdr, gst)
        if drift:
            self._fail(
                result, "arithmetic_mismatch", "arithmetic",
                f"verdict's own figures disagree with the records: {drift}",
            )
            return False

        result.checks.append(
            CheckResult(
                check="arithmetic", passed=True,
                detail=f"gross {gross} - mdr {mdr} - gst {gst} "
                       f"{'+' if claimed_rounding >= 0 else '-'} rounding "
                       f"{abs(claimed_rounding)} = {net}, matching the credit",
            )
        )
        return True

    @staticmethod
    def _block_drift(
        block, gross: Decimal, mdr: Decimal, gst: Decimal
    ) -> str | None:
        """Where the verdict's claimed figures depart from the recomputation.

        Fees are compared as magnitudes: a verdict may quite reasonably write
        MDR as a negative, being a deduction. The size is what must agree.
        """
        problems = []
        if block.gross != gross:
            problems.append(f"gross claimed {block.gross}, records give {gross}")
        if abs(block.mdr) != mdr:
            problems.append(f"mdr claimed {block.mdr}, records give {mdr}")
        if abs(block.gst) != gst:
            problems.append(f"gst claimed {block.gst}, records give {gst}")
        return "; ".join(problems) or None


def verify_verdict(verdict: Verdict, batch: GeneratedBatch) -> VerifierResult:
    """Verify one verdict against a batch. Convenience for single checks."""
    return Verifier(batch).verify(verdict)
