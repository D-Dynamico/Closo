"""A stand-in for a competent model, for the end-to-end tests (12.6).

Not a fixture of canned strings: this client *solves* each exception from
the records, the same way the real investigator is meant to, and submits
the verdict through the same `submit_verdict` function call. That keeps
`test_pipeline_e2e.py` exercising the whole of Layers 2 and 3 - the tool
loop, verdict parsing, verification, the terminal-state assignment - with
no network and nothing canned to drift out of date.

**It does not read ground truth.** It searches settlements for one whose
recomputed net reproduces the credit, which is what a good investigation
would establish. If it read the answers, "every resolution matches ground
truth" would be true by construction and would prove nothing about the
pipeline that produced it.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Sequence

from closo.llm_client import LLMResponse, ToolCall
from closo.schemas import ExceptionItem
from closo.taxonomy import GeneratedBatch
from closo.tools import ToolBox

#: A fixed cost per turn, so the token figures a test asserts on are the
#: test's own numbers rather than an accident of a real tokenizer.
TOKENS_PER_TURN = 100

EXCEPTION_RE = re.compile(r"Exception (EX-\d+)")


def solve(batch: GeneratedBatch, exceptions: list[ExceptionItem]) -> dict[str, dict]:
    """Work out the correct verdict arguments for each exception.

    Returns exception_id -> submit_verdict arguments, omitting any
    exception with no defensible answer. Those are E9 and E10, and leaving
    them out is the point: the client then submits `unresolvable`, which is
    the correct outcome and the one the escalation story depends on.
    """
    toolbox = ToolBox(batch)
    credits = {b.bank_txn_id: b for b in batch.bank_txns}
    open_credits = [i.bank_txn_id for i in exceptions if i.bank_txn_id]
    plan: dict[str, dict] = {}
    claimed: set[str] = set()

    for item in exceptions:
        if item.bank_txn_id is None:
            continue
        amount = credits[item.bank_txn_id].credit_amount
        found = _search(
            toolbox, batch, amount, item.bank_txn_id, open_credits, credits, claimed
        )
        if found is None:
            continue
        settlement_id, schedule, block, extra = found
        claimed.add(settlement_id)
        plan[item.exception_id] = {
            "hypothesis": (
                f"settlement {settlement_id} under fee schedule {schedule}"
                + (" paid across two credits" if extra else "")
            ),
            "hypotheses_rejected": [
                {"hypothesis": "a partial refund was netted into the payout",
                 "reason": "no refund is recorded against the cited payments"},
            ],
            "confidence": "resolved",
            "bank_txn_id": item.bank_txn_id,
            "extra_bank_txn_ids": extra,
            "payment_ids": list(block["payment_ids"]),
            "fee_schedule": schedule,
            "arithmetic": {
                "gross": block["gross"], "mdr": block["mdr"],
                "gst": block["gst"], "rounding": block.get("rounding", "0.00"),
                "net": block["net"],
            },
        }
    return plan


def _search(
    toolbox: ToolBox,
    batch: GeneratedBatch,
    amount: Decimal,
    bank_txn_id: str,
    open_credits: list[str],
    credits: dict,
    claimed: set[str],
) -> tuple[str, str, dict, list[str]] | None:
    """Find a settlement that reproduces this credit, alone or split in two."""
    for settlement in batch.settlements:
        if settlement.settlement_id in claimed:
            continue
        for schedule in ("v1", "v2"):
            block = toolbox.compute_expected_settlement(
                list(settlement.payment_ids), schedule
            )
            if "error" in block:
                continue
            net = Decimal(block["net"])
            if net == amount:
                return settlement.settlement_id, schedule, block, []
            for other in open_credits:
                if other == bank_txn_id:
                    continue
                if net == amount + credits[other].credit_amount:
                    return settlement.settlement_id, schedule, block, [other]
    return None


class OracleClient:
    """A model that investigates correctly and stops when it cannot.

    Two turns per exception: one `compute_expected_settlement` call, then
    the verdict. The tool turn is not decoration - it is what puts real
    evidence on the verdict and exercises the investigator's tool loop, so
    a break in the loop shows up here rather than only in Stage 6's mocked
    unit tests.
    """

    def __init__(self, plan: dict[str, dict], tokens: int = TOKENS_PER_TURN) -> None:
        self.plan = plan
        self.tokens = tokens
        self.turns = 0
        self.requests_made = 0
        self.cache_hits = 0

    def generate(
        self,
        system_prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        force_tool: str | None = None,
    ) -> LLMResponse:
        self.turns += 1
        self.requests_made += 1
        args = self.plan.get(self._exception_id(messages))

        if args is None:
            return self._verdict({
                "hypothesis": "no settlement reproduces this credit under either "
                              "fee schedule; the money is not accounted for here",
                "hypotheses_rejected": [
                    {"hypothesis": "settlement lag", "reason": "no settlement matches"},
                ],
                "confidence": "unresolvable",
            })

        if not any(m.get("role") == "tool" for m in messages):
            return LLMResponse(
                text="checking the arithmetic",
                tool_calls=[ToolCall(
                    name="compute_expected_settlement",
                    args={"payment_ids": args["payment_ids"],
                          "fee_schedule": args["fee_schedule"]},
                )],
                tokens_used=self.tokens,
            )
        return self._verdict(args)

    def _verdict(self, args: dict) -> LLMResponse:
        return LLMResponse(
            tool_calls=[ToolCall(name="submit_verdict", args=args)],
            tokens_used=self.tokens,
        )

    @staticmethod
    def _exception_id(messages: Sequence[dict[str, Any]]) -> str:
        match = EXCEPTION_RE.search(str(messages[0].get("content", "")))
        return match.group(1) if match else ""
