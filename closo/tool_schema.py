"""Provider-neutral declarations for the tools the model may call.

Kept apart from ``tools.py`` (which implements them) and from
``llm_client.py`` (which translates these into whatever shape the SDK
wants). The investigator never sees a provider-specific schema, which is
what lets the mocked client be a faithful stand-in for the real one.

Descriptions here are written for the model, not for a developer. They say
when to reach for a tool and what an empty answer means, because "no
refunds exist" is the finding that rules out a hypothesis and a model that
reads it as a failure will draw the wrong conclusion.
"""

from __future__ import annotations

from typing import Any

SUBMIT_VERDICT = "submit_verdict"

#: A rupee amount, always passed as a string. Floats are refused by the
#: tools; JSON has no Decimal and a float loses paise.
_AMOUNT = {"type": "string", "description": "Rupee amount as a string, e.g. \"4720.00\""}
_DATE = {"type": "string", "description": "ISO date, e.g. \"2026-03-09\""}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


READ_TOOLS: list[dict[str, Any]] = [
    _fn(
        "get_payment",
        "Fetch one payment by id, including its settlement leg if it has one. "
        "Returns {\"error\": \"not_found\"} for an unknown id - try a different "
        "id rather than giving up.",
        {"payment_id": {"type": "string"}},
        ["payment_id"],
    ),
    _fn(
        "list_payments",
        "Find payments by amount, capture date or method. All bounds are "
        "INCLUSIVE, so pass the same value as amount_min and amount_max to "
        "look up an exact amount. An empty result is a real answer meaning no "
        "such payment exists, not an error.",
        {
            "amount_min": _AMOUNT, "amount_max": _AMOUNT,
            "date_from": _DATE, "date_to": _DATE,
            "method": {"type": "string", "enum": ["upi", "card", "netbanking"]},
        },
        [],
    ),
    _fn(
        "get_refunds",
        "Refunds recorded against a payment. A count of 0 is a real answer and "
        "positively rules out any refund-based explanation - use it that way.",
        {"payment_id": {"type": "string"}},
        ["payment_id"],
    ),
    _fn(
        "get_settlement",
        "Fetch one settlement, including both the fee schedule recorded on it "
        "and the schedule that was actually active on its settlement date. "
        "When those two differ, that is worth investigating.",
        {"settlement_id": {"type": "string"}},
        ["settlement_id"],
    ),
    _fn(
        "list_settlements",
        "Find settlements by date or settled amount. Bounds are inclusive.",
        {
            "date_from": _DATE, "date_to": _DATE,
            "amount_min": _AMOUNT, "amount_max": _AMOUNT,
        },
        [],
    ),
    _fn(
        "get_bank_txn",
        "Fetch one bank credit: its narration, value date, amount, and the UTR "
        "parsed from the narration. A null parsed UTR means the narration "
        "contained no recoverable reference.",
        {"bank_txn_id": {"type": "string"}},
        ["bank_txn_id"],
    ),
    _fn(
        "compute_expected_settlement",
        "Compute what a set of payments should settle to under one fee "
        "schedule. USE THIS FOR ALL ARITHMETIC - never calculate an amount "
        "yourself. Every number you put in your verdict must come from this "
        "tool's output, because an independent verifier recomputes the same "
        "way and will reject anything it cannot reproduce. One schedule per "
        "call: to compare v1 against v2, call it twice.",
        {
            "payment_ids": {"type": "array", "items": {"type": "string"}},
            "fee_schedule": {"type": "string", "enum": ["v1", "v2"]},
        },
        ["payment_ids", "fee_schedule"],
    ),
    _fn(
        "check_duplicate_utr",
        "How many settlements and bank credits carry a UTR. Two settlements "
        "sharing one UTR is a duplicate-reference problem; two credits against "
        "ONE settlement is a split payout. Use this to tell them apart.",
        {"utr": {"type": "string"}},
        ["utr"],
    ),
    _fn(
        "get_fee_schedules",
        "Both fee schedules, their rates per payment method, and the cutover "
        "date that decides which one applies.",
        {},
        [],
    ),
]

#: The verdict is submitted as a function call, never as prose. Forcing it
#: is what makes the output parseable without guessing (7.2).
SUBMIT_VERDICT_TOOL: dict[str, Any] = _fn(
    SUBMIT_VERDICT,
    "Submit your final verdict. Call this exactly once, when you have "
    "finished investigating. Returning `unresolvable` is a correct and "
    "expected outcome for some exceptions - prefer it over a guess.",
    {
        "hypothesis": {
            "type": "string",
            "description": "The explanation you settled on, in one sentence.",
        },
        "hypotheses_rejected": {
            "type": "array",
            "description": "Explanations you considered and ruled out, with "
                           "the evidence that ruled each one out. An exception "
                           "listing what was tried is far more useful to a "
                           "human than one that only says it failed.",
            "items": {
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["hypothesis", "reason"],
            },
        },
        "confidence": {
            "type": "string",
            "enum": ["resolved", "probable", "unresolvable"],
            "description": "`resolved` only when one explanation fits and the "
                           "arithmetic reproduces the credit exactly. If two "
                           "explanations both fit, say `probable`.",
        },
        "bank_txn_id": {"type": "string"},
        "extra_bank_txn_ids": {
            "type": "array", "items": {"type": "string"},
            "description": "Additional credits, for a split payout.",
        },
        "payment_ids": {"type": "array", "items": {"type": "string"}},
        "fee_schedule": {"type": "string", "enum": ["v1", "v2"]},
        "arithmetic": {
            "type": "object",
            "description": "Copied verbatim from compute_expected_settlement.",
            "properties": {
                "gross": _AMOUNT, "mdr": _AMOUNT, "gst": _AMOUNT,
                "rounding": _AMOUNT, "net": _AMOUNT,
            },
            "required": ["gross", "mdr", "gst", "net"],
        },
    },
    ["hypothesis", "confidence"],
)

ALL_TOOLS: list[dict[str, Any]] = [*READ_TOOLS, SUBMIT_VERDICT_TOOL]

READ_TOOL_NAMES = frozenset(tool["name"] for tool in READ_TOOLS)
