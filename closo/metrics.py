"""Scorecard computation (ARCHITECTURE 9.2).

**This is the only module permitted to read ``ground_truth.json``** (11.4),
and only after a run has finished. The pipeline has no import path to it at
all; the quarantine is what makes the accuracy numbers mean anything, since a
reconciler that peeked at the answers would score perfectly and prove nothing.

The headline figure is **verified accuracy**, reported against ground truth
even when it is below 100 percent. So is the escalation breakdown: false
escalations and false resolutions are counted separately, because they are
very different failures. A false escalation wastes an analyst's afternoon; a
false resolution means money silently went unreconciled and nobody will look
at it again.

No LLM imports here (11.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from closo.config import DEMO_DIR, INR_PER_MILLION_TOKENS, ZERO, money
from closo.dataset_io import load_ground_truth
from closo.pipeline import PENDING_NOTE, RunOutcome
from closo.schemas import FinalStatus
from closo.taxonomy import DESIGNED_UNRESOLVABLE, GeneratedBatch

RESOLVED_STATES = (FinalStatus.AUTO_MATCHED, FinalStatus.AGENT_RESOLVED_VERIFIED)


@dataclass
class ClassBreakdown:
    """How one error class fared."""

    error_class: str
    total: int = 0
    auto_matched: int = 0
    agent_verified: int = 0
    escalated: int = 0

    @property
    def resolved(self) -> int:
        return self.auto_matched + self.agent_verified


@dataclass
class Scorecard:
    """Everything the Scorecard screen shows."""

    run_id: str
    seed: int
    total_bank_txns: int = 0

    auto_matched: int = 0
    agent_verified: int = 0
    escalated: int = 0

    correct_resolutions: int = 0
    incorrect_resolutions: int = 0

    false_escalations: int = 0   # escalated, but genuinely resolvable
    false_resolutions: int = 0   # resolved, but designed unresolvable
    correct_escalations: int = 0

    #: Of the false escalations, how many are parked because a later layer
    #: is not built yet. Reported *alongside* false_escalations, never
    #: subtracted from it - the work is genuinely undone either way, and a
    #: metric that discounts its own gaps stops being a measurement.
    pending_investigation: int = 0

    #: Resolved on verified math but unverified intent (8.1) - a `probable`
    #: verdict, or one capped there by a fee-schedule anomaly. Counted
    #: inside agent_verified, never instead of it: the arithmetic really
    #: was reproduced. Reported separately so nobody reads "resolved" as
    #: "settled, nothing more to do".
    awaiting_signoff: int = 0

    money_reconciled: Decimal = ZERO
    money_stuck: Decimal = ZERO
    money_total: Decimal = ZERO
    money_awaiting_signoff: Decimal = ZERO

    taxonomy: dict[str, ClassBreakdown] = field(default_factory=dict)

    elapsed_seconds: float = 0.0

    # -- what the run spent (9.2) -----------------------------------------
    #
    # Requests, not tokens, are the scarce resource on this quota (7.4), so
    # both are reported and requests is the one to read. A cached or
    # replayed run shows zero requests and that is the true figure: it
    # spent none.
    tokens_used: int = 0
    requests_made: int = 0
    cache_hits: int = 0
    exceptions_investigated: int = 0
    exceptions_skipped: int = 0
    layer2_seconds: float = 0.0
    quota_exhausted: bool = False

    # -- derived -----------------------------------------------------------

    @property
    def match_rate(self) -> float:
        """Share of credits that reached a resolved state."""
        if not self.total_bank_txns:
            return 0.0
        return (self.auto_matched + self.agent_verified) / self.total_bank_txns

    @property
    def verified_accuracy(self) -> float:
        """Of everything resolved, the share matching ground truth exactly.

        The headline number. Reported even when below 100 percent - a
        reconciliation tool that only publishes its accuracy when the figure
        is flattering is not reporting accuracy.
        """
        decided = self.correct_resolutions + self.incorrect_resolutions
        return self.correct_resolutions / decided if decided else 0.0

    @property
    def records_per_minute(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.total_bank_txns / self.elapsed_seconds * 60

    @property
    def layer1_records_per_minute(self) -> float:
        """Throughput of the deterministic cascade alone (9.2).

        Reported separately because it is enormous, and averaging it with
        Layer 2 hides both facts: that 83% of the batch is reconciled in
        milliseconds, and that the remainder costs seconds per record
        because it is talking to a model.
        """
        layer1_seconds = self.elapsed_seconds - self.layer2_seconds
        if layer1_seconds <= 0:
            return 0.0
        return self.total_bank_txns / layer1_seconds * 60

    @property
    def tokens_per_record(self) -> float:
        if not self.total_bank_txns:
            return 0.0
        return self.tokens_used / self.total_bank_txns

    @property
    def rupees_spent(self) -> Decimal:
        """What the tokens cost at the configured rate.

        Zero on the free tier, which is the honest figure rather than a
        placeholder - see ``INR_PER_MILLION_TOKENS``.
        """
        return money(
            Decimal(self.tokens_used) * INR_PER_MILLION_TOKENS / Decimal(1_000_000)
        )

    @property
    def rupees_per_record(self) -> Decimal:
        if not self.total_bank_txns:
            return ZERO
        return money(self.rupees_spent / Decimal(self.total_bank_txns))

    def stable_dict(self) -> dict:
        """The scorecard minus anything that legitimately varies per run.

        Timing and run id change between two runs of the same seed by
        definition, so the determinism test in 12.6 compares this rather
        than the whole object. Excluding them is not weakening the check:
        every *measured reconciliation* quantity is still in here.

        Cost is excluded for the same reason, and it is worth being
        explicit about why: a live run and a replay of that same run
        produce byte-identical reconciliation figures while spending
        different numbers of requests, because the replay spends none.
        What the run cost is a fact about the run, not about the batch. It
        is asserted directly instead, including across a replay.
        """
        return {
            "seed": self.seed,
            "total_bank_txns": self.total_bank_txns,
            "auto_matched": self.auto_matched,
            "agent_verified": self.agent_verified,
            "escalated": self.escalated,
            "correct_resolutions": self.correct_resolutions,
            "incorrect_resolutions": self.incorrect_resolutions,
            "false_escalations": self.false_escalations,
            "pending_investigation": self.pending_investigation,
            "false_resolutions": self.false_resolutions,
            "correct_escalations": self.correct_escalations,
            "awaiting_signoff": self.awaiting_signoff,
            "money_awaiting_signoff": str(self.money_awaiting_signoff),
            "match_rate": round(self.match_rate, 6),
            "verified_accuracy": round(self.verified_accuracy, 6),
            "money_reconciled": str(self.money_reconciled),
            "money_stuck": str(self.money_stuck),
            "money_total": str(self.money_total),
            "taxonomy": {
                cls: {
                    "total": b.total,
                    "auto_matched": b.auto_matched,
                    "agent_verified": b.agent_verified,
                    "escalated": b.escalated,
                }
                for cls, b in sorted(self.taxonomy.items())
            },
        }


def score(
    outcome: RunOutcome, batch: GeneratedBatch, data_dir: Path = DEMO_DIR
) -> Scorecard:
    """Grade a finished run against ground truth.

    Args:
        outcome: the completed run. Must be finished; grading a live run
            would race the pipeline.
        batch: the source records, for amounts.
        data_dir: where ``ground_truth.json`` lives.
    """
    truth = load_ground_truth(data_dir)["bank_txns"]
    credits = {b.bank_txn_id: b for b in batch.bank_txns}

    card = Scorecard(
        run_id=outcome.run_id,
        seed=outcome.seed,
        total_bank_txns=len(outcome.statuses),
        elapsed_seconds=outcome.elapsed_seconds,
        tokens_used=outcome.cost.tokens_used,
        requests_made=outcome.cost.requests_made,
        cache_hits=outcome.cost.cache_hits,
        exceptions_investigated=outcome.cost.exceptions_investigated,
        exceptions_skipped=outcome.cost.exceptions_skipped,
        layer2_seconds=outcome.cost.layer2_seconds,
        quota_exhausted=outcome.cost.quota_exhausted,
    )

    # Both layers' claims, graded the same way. An agent resolution that
    # was not checked against ground truth would count toward the match
    # rate on the strength of having passed verification - and the verifier
    # proves the arithmetic reproduces the credit, not that the credit came
    # from those payments.
    claims: dict[str, list[str]] = {
        m.bank_txn_id: list(m.payment_ids)
        for m in (outcome.layer1.matches if outcome.layer1 else [])
    }
    claims.update({k: list(v) for k, v in outcome.agent_matches.items()})

    for txn_id, status in outcome.statuses.items():
        entry = truth.get(txn_id, {})
        error_class = entry.get("error_class", "unknown")
        breakdown = card.taxonomy.setdefault(error_class, ClassBreakdown(error_class))
        breakdown.total += 1

        amount = credits[txn_id].credit_amount if txn_id in credits else ZERO
        card.money_total = money(card.money_total + amount)

        if status is FinalStatus.AUTO_MATCHED:
            card.auto_matched += 1
            breakdown.auto_matched += 1
        elif status is FinalStatus.AGENT_RESOLVED_VERIFIED:
            card.agent_verified += 1
            breakdown.agent_verified += 1
            if txn_id in outcome.needs_signoff:
                card.awaiting_signoff += 1
                card.money_awaiting_signoff = money(
                    card.money_awaiting_signoff + amount
                )
        else:
            card.escalated += 1
            breakdown.escalated += 1

        designed_unresolvable = error_class in DESIGNED_UNRESOLVABLE

        if status in RESOLVED_STATES:
            card.money_reconciled = money(card.money_reconciled + amount)
            claimed = sorted(claims.get(txn_id, []))
            expected = sorted(entry.get("source_payment_ids", []))
            if claimed == expected and not designed_unresolvable:
                card.correct_resolutions += 1
            else:
                card.incorrect_resolutions += 1
            if designed_unresolvable:
                # Critical: a class that cannot be resolved was resolved.
                card.false_resolutions += 1
        else:
            card.money_stuck = money(card.money_stuck + amount)
            if designed_unresolvable:
                card.correct_escalations += 1
            else:
                card.false_escalations += 1
                if PENDING_NOTE in outcome.notes.get(txn_id, ""):
                    card.pending_investigation += 1

    return card


def score_demo(outcome: RunOutcome, batch: GeneratedBatch) -> Scorecard:
    """Grade a run over the frozen demo set."""
    return score(outcome, batch, DEMO_DIR)
