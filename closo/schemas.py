"""Pydantic models for every record, verdict and result Closo passes around.

Two rules shape this module.

**Money is Decimal, always.** Amount fields are typed ``Decimal`` and
validated through :func:`closo.config.money`, so a float arriving from a
CSV reader or a JSON payload is rejected at the boundary rather than
three layers deep. Serialization emits strings, never floats, because a
JSON float would undo the whole point on the way out (CLAUDE.md 7.1).

**There are exactly three terminal states.** :class:`FinalStatus` has
three members and no fourth. A verdict the verifier rejected is never
shown as resolved (2).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

from closo.config import money


def _to_money(value: Any) -> Any:
    """Coerce an incoming amount to a quantized Decimal.

    Floats are refused outright. Everything else goes through
    :func:`closo.config.money` so there is one rounding rule in the repo.
    """
    if value is None or isinstance(value, Decimal):
        return money(value) if value is not None else None
    if isinstance(value, float):
        raise ValueError(
            "float is not allowed for a money field; pass Decimal, int or str"
        )
    if isinstance(value, (int, str)):
        try:
            return money(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"not a valid amount: {value!r}") from exc
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(_to_money),
    PlainSerializer(lambda v: str(v) if v is not None else None, return_type=str),
]
"""A rupee amount: Decimal in, quantized to 2dp, string out.

The serializer rides on the type rather than on the base model's config so
the guarantee travels with every field that uses it, including ones nested
in dicts. A JSON float here would silently undo the Decimal discipline the
rest of the repo is built on.
"""


class ClosoModel(BaseModel):
    """Base model. Strict about unknown fields; assignment is re-validated."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )


# --------------------------------------------------------------------------
# Source records
# --------------------------------------------------------------------------

PaymentMethod = Literal["upi", "card", "netbanking"]
PaymentStatus = Literal["captured", "refunded", "partial_refund", "disputed"]


class Payment(ClosoModel):
    """One captured Razorpay payment, with its settlement leg if settled.

    Settlement fields are optional because error class E9 is precisely a
    payment that was never settled - the money is genuinely absent, and
    the model has to be able to represent that rather than assume it away.
    """

    payment_id: str
    order_id: str
    amount_gross: Money
    method: PaymentMethod
    captured_at: date
    status: PaymentStatus = "captured"
    refund_amount: Money = Field(default=Decimal("0.00"))

    settlement_id: str | None = None
    settlement_utr: str | None = None
    settled_at: date | None = None
    fee_mdr: Money | None = None
    fee_gst: Money | None = None
    amount_settled: Money | None = None

    @property
    def net_of_refund(self) -> Decimal:
        """Gross less any refund. The figure a settlement actually nets on."""
        return money(self.amount_gross - self.refund_amount)


class Settlement(ClosoModel):
    """A Razorpay settlement: a batch of payments paid out as one credit."""

    settlement_id: str
    utr: str
    settled_at: date
    payment_ids: list[str]
    amount_gross: Money
    fee_mdr: Money
    fee_gst: Money
    amount_settled: Money
    fee_schedule: str
    rounding: Money = Field(default=Decimal("0.00"))


class BankTxn(ClosoModel):
    """One credit line on the bank statement.

    ``utr`` is the *normalized* UTR and is None whenever the narration
    yielded nothing parseable (error class E8) - the raw narration is kept
    so the investigator and the drill-down screen can show what it saw.
    """

    bank_txn_id: str
    txn_date: date
    value_date: date
    narration: str
    utr: str | None = None
    credit_amount: Money
    balance: Money | None = None


class Order(ClosoModel):
    """A row from the merchant's internal order ledger."""

    order_id: str
    sku: str
    order_amount: Money
    order_date: date
    channel: str
    expected_settlement: Money | None = None


# --------------------------------------------------------------------------
# Layer 1 output
# --------------------------------------------------------------------------

MatchPass = Literal["A_utr_exact", "B_amount_date_window", "C_netting_recompute"]


class MatchRecord(ClosoModel):
    """A deterministic match made by Layer 1.

    ``tolerance_applied`` is recorded on every match, not just the ones
    that needed it, so the audit log can answer "was this exact?" without
    re-deriving it (6).
    """

    bank_txn_id: str
    settlement_id: str
    payment_ids: list[str]
    pass_used: MatchPass
    tolerance_applied: Money = Field(default=Decimal("0.00"))


ExceptionReason = Literal[
    "no_utr_match",
    "amount_mismatch",
    "ambiguous_tie",
    "duplicate_utr",
    "no_utr_in_narration",
    "outside_date_window",
    "outside_tolerance",
    "no_counterpart",
]


class ExceptionItem(ClosoModel):
    """An unmatched record handed from Layer 1 to the investigator."""

    exception_id: str
    bank_txn_id: str | None = None
    settlement_id: str | None = None
    reason: ExceptionReason
    detail: str = ""


# --------------------------------------------------------------------------
# Layer 2 verdicts
# --------------------------------------------------------------------------

Confidence = Literal["resolved", "probable", "unresolvable"]


class RejectedHypothesis(ClosoModel):
    """A hypothesis the investigator considered and ruled out.

    Kept because the escalation screen shows what was tried - an exception
    that lists three rejected hypotheses is far more trustworthy than one
    that just says "could not resolve" (10.5).
    """

    hypothesis: str
    reason: str


class Arithmetic(ClosoModel):
    """The investigator's claimed arithmetic breakdown.

    This is a *claim*, not evidence. The verifier recomputes all of it from
    raw records and compares; a block that is internally consistent but
    disagrees with the source data fails (8.2).
    """

    gross: Money
    mdr: Money
    gst: Money
    rounding: Money = Field(default=Decimal("0.00"))
    net: Money


class ProposedMatch(ClosoModel):
    """The match the investigator proposes, pending verification."""

    bank_txn_id: str
    payment_ids: list[str] = Field(min_length=1)
    fee_schedule: str
    arithmetic: Arithmetic
    extra_bank_txn_ids: list[str] = Field(default_factory=list)


class Evidence(ClosoModel):
    """One tool call the investigator made, and what came back."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result_summary: str


class Verdict(ClosoModel):
    """A complete investigator verdict (7.2).

    ``proposed_match`` is absent exactly when confidence is
    ``unresolvable`` - giving up is a valid, expected outcome and must not
    be forced to invent a match.
    """

    exception_id: str
    hypothesis: str
    hypotheses_rejected: list[RejectedHypothesis] = Field(default_factory=list)
    proposed_match: ProposedMatch | None = None
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list)
    tokens_used: int = 0


# --------------------------------------------------------------------------
# Layer 3 verification
# --------------------------------------------------------------------------

CheckName = Literal[
    "existence",
    "arithmetic",
    "fee_schedule",
    "exclusivity",
    "refund_consistency",
]

RejectionReason = Literal[
    "phantom_reference",
    "arithmetic_mismatch",
    "wrong_fee_schedule",
    "exclusivity_violation",
    "refund_fabrication",
    "malformed_verdict",
]


class CheckResult(ClosoModel):
    """One verifier check: did it pass, and what did it actually compute."""

    check: CheckName
    passed: bool
    detail: str = ""


class VerifierResult(ClosoModel):
    """The outcome of independently re-checking one verdict (8).

    ``passed`` means the arithmetic was reproduced from raw records. It does
    not mean the intent behind the transaction was validated - that is what
    ``needs_human_signoff`` is for.
    """

    exception_id: str
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    rejection_reason: RejectionReason | None = None

    #: Set when the cited fee schedule was not the one active on the
    #: settlement date, yet still reproduces the bank credit exactly. The
    #: math is proven; whether applying that schedule was authorised is not
    #: something a machine should decide, so it is handed to a human with
    #: the specific question attached (8.1).
    schedule_anomaly: str | None = None

    #: True when the math verified but intent did not - a `probable` verdict,
    #: or one capped to probable by a schedule anomaly. These land in the
    #: sign-off sub-list, never in AGENT_RESOLVED_VERIFIED silently.
    needs_human_signoff: bool = False

    #: Confidence after verification, which may be lower than the verdict
    #: claimed. The verifier can demote; it can never promote.
    effective_confidence: Confidence | None = None

    #: Payments this verdict consumes, once it passes. Fed back into the
    #: verifier so a later verdict citing the same payment is caught.
    consumed_payment_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Terminal states
# --------------------------------------------------------------------------


class FinalStatus(str, Enum):
    """The only three states a bank transaction can end in (2).

    There is no fourth. An agent verdict that failed verification is
    ESCALATED, never AGENT_RESOLVED_VERIFIED.
    """

    AUTO_MATCHED = "AUTO_MATCHED"
    AGENT_RESOLVED_VERIFIED = "AGENT_RESOLVED_VERIFIED"
    ESCALATED = "ESCALATED"


class Resolution(ClosoModel):
    """The final disposition of one bank transaction."""

    bank_txn_id: str
    final_status: FinalStatus
    match: MatchRecord | None = None
    verdict: Verdict | None = None
    verifier_result: VerifierResult | None = None
    needs_human_signoff: bool = False
    escalation_note: str = ""


class RunResult(ClosoModel):
    """Everything one pipeline run produced."""

    run_id: str
    seed: int
    started_at: datetime
    finished_at: datetime | None = None
    resolutions: list[Resolution] = Field(default_factory=list)
    total_bank_txns: int = 0
