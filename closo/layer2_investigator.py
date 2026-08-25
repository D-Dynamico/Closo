"""Layer 2 - the exception investigator (ARCHITECTURE 7).

One isolated conversation per exception. Isolation is deliberate: it keeps
each context small and cheap, and it stops a wrong turn on one exception
from contaminating the next.

Everything here is bounded. Eight tool calls, thirty seconds, one retry on
a malformed verdict, one retry on a transient API error, and a hard daily
request budget above all of it. Each bound produces `unresolvable` rather
than an exception, because **one bad exception must never kill the batch** -
a run that dies at record 12 of 20 produces a scorecard that is not wrong
so much as meaningless.

The model proposes. Nothing here decides anything is resolved; that is the
verifier's job, and it can overrule every word of what comes out of this
module.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from closo.config import (
    EXCEPTION_TIMEOUT_SECONDS,
    MAX_TOOL_CALLS_PER_EXCEPTION,
)
from closo.llm_client import LLMClient, LLMResponse, QuotaExhausted, ToolCall
from closo.schemas import (
    Arithmetic,
    Evidence,
    ExceptionItem,
    ProposedMatch,
    RejectedHypothesis,
    Verdict,
)
from closo.tool_schema import ALL_TOOLS, SUBMIT_VERDICT
from closo.tools import ToolBox

SYSTEM_PROMPT = """\
You are a reconciliation analyst investigating a single unmatched bank credit.

A deterministic matcher has already tried and failed on this one, so the
straightforward explanations are exhausted. Something non-obvious is going on:
a refund netted into the payout, a fee schedule that changed, one settlement
paid across two credits, a duplicated reference, or money that genuinely never
arrived.

HOW YOU WORK

1. Read the exception. Form two or three competing hypotheses before you touch
   a tool. Reconciliation errors look alike from the outside, and the first
   plausible story is often not the right one.
2. Use tools to rule hypotheses OUT. A tool returning nothing is evidence, not
   a dead end - "this payment has no refunds" positively eliminates an entire
   explanation.
3. Call compute_expected_settlement for every amount. Do not do arithmetic
   yourself, not even addition. An independent verifier recomputes all of it
   from the raw records and rejects any figure it cannot reproduce, so a number
   you worked out in your head fails verification even when it is correct.
4. Call submit_verdict exactly once, when you are done.

RULES THAT DECIDE THE OUTCOME

- You have at most 8 tool calls. Spend them on ruling things out, not on
  confirming what you already believe. Never repeat a call you have already
  made with the same arguments - you already have that answer.
- IF THE AMOUNT DOES NOT RECONCILE UNDER THE SCHEDULE RECORDED ON THE
  SETTLEMENT, COMPUTE IT UNDER THE OTHER ONE BEFORE CONCLUDING ANYTHING.
  A payout run on a superseded fee schedule is a specific, common failure,
  and it is invisible unless you actually compute the alternative. Citing a
  schedule you did not compute under is worse than saying unresolvable: the
  verifier will reject the figures and the exception is escalated anyway.
- If check_duplicate_utr shows ONE settlement paid across TWO credits, that
  is a split payout. Check whether the two credits SUM to the settlement net,
  and if they do, cite both - the second one goes in extra_bank_txn_ids.
  Two settlements sharing one UTR is a different problem: a duplicated
  reference, where each credit belongs to its own settlement by amount.
- Never invent an id. If a tool says not_found, that record does not exist and
  that fact is itself informative.
- If two explanations both fit the evidence, answer `probable`, not `resolved`.
- `unresolvable` is a correct and expected answer. Some of these exceptions are
  money that genuinely never arrived, or credits from an entirely different
  source. Saying so is the right result, and far more useful than a confident
  guess. You are not scored on how many you resolve.
- Whatever you conclude, list the hypotheses you rejected and why. A human
  reading your escalation needs to know what has already been tried.

Every number in your arithmetic block must be copied from a tool result.
"""

RETRY_PROMPT = (
    "You did not call submit_verdict. Reply with a submit_verdict function "
    "call and nothing else. If you could not determine an answer, submit it "
    "with confidence `unresolvable` - that is a valid outcome."
)

TRANSIENT_ERRORS = (
    "429", "503", "529", "overloaded", "rate limit", "unavailable",
    "resource_exhausted",
)

#: Pause before retrying a transient error. Comfortably longer than one
#: request slot at the free-tier rate, so a retry lands in clear air.
TRANSIENT_BACKOFF_SECONDS = 8.0


@dataclass
class InvestigationOutcome:
    """One exception's verdict plus how it was reached."""

    verdict: Verdict
    events: list[dict[str, Any]] = field(default_factory=list)
    tool_calls_used: int = 0
    tokens_used: int = 0


def _unresolvable(
    exception_id: str, why: str, rejected: list[RejectedHypothesis] | None = None
) -> Verdict:
    """A verdict for every path that ends without an answer."""
    return Verdict(
        exception_id=exception_id,
        hypothesis=why,
        hypotheses_rejected=rejected or [],
        confidence="unresolvable",
    )


def _is_transient(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in TRANSIENT_ERRORS)


class Investigator:
    """Runs one bounded conversation per exception."""

    def __init__(
        self,
        toolbox: ToolBox,
        client: LLMClient,
        max_tool_calls: int = MAX_TOOL_CALLS_PER_EXCEPTION,
        timeout_seconds: float = EXCEPTION_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.toolbox = toolbox
        self.client = client
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds
        self.clock = clock

    # -- public ------------------------------------------------------------

    def investigate(self, item: ExceptionItem) -> InvestigationOutcome:
        """Investigate one exception. Never raises for a per-exception fault.

        Raises:
            QuotaExhausted: the one failure that is not per-exception. The
                caller stops the batch and marks the remainder honestly,
                rather than issuing another hundred doomed requests.
        """
        outcome = InvestigationOutcome(
            verdict=_unresolvable(item.exception_id, "investigation did not complete")
        )
        deadline = self.clock() + self.timeout_seconds
        messages: list[dict[str, Any]] = [{"role": "user", "content": self._brief(item)}]
        evidence: list[Evidence] = []
        retried_malformed = False

        while True:
            if self.clock() > deadline:
                self._log(outcome, item, "timeout",
                          seconds=self.timeout_seconds,
                          tool_calls_used=outcome.tool_calls_used)
                outcome.verdict = _unresolvable(
                    item.exception_id,
                    f"investigation exceeded {self.timeout_seconds}s",
                )
                break

            budget_spent = outcome.tool_calls_used >= self.max_tool_calls
            try:
                response = self._ask(messages, force_verdict=budget_spent, item=item,
                                     outcome=outcome)
            except QuotaExhausted:
                raise
            except Exception as error:  # noqa: BLE001 - deliberately broad
                self._log(outcome, item, "api_error_final", error=str(error))
                outcome.verdict = _unresolvable(
                    item.exception_id, f"API error: {error}"
                )
                break

            outcome.tokens_used += response.tokens_used

            verdict_call = self._find_verdict_call(response)
            if verdict_call is not None:
                outcome.verdict = self._build_verdict(item, verdict_call, evidence)
                self._log(outcome, item, "verdict_submitted",
                          confidence=outcome.verdict.confidence,
                          tool_calls_used=outcome.tool_calls_used)
                break

            # Anything that is not the verdict is dispatched, including names
            # that do not exist. Filtering unknown names out would leave the
            # turn looking empty, so the model would be told nothing and would
            # very likely call the same imaginary tool again.
            read_calls = [c for c in response.tool_calls if c.name != SUBMIT_VERDICT]
            if read_calls:
                if budget_spent:
                    # The model ignored a forced submit_verdict and asked for
                    # more data. Stop rather than negotiate.
                    self._log(outcome, item, "tool_budget_exhausted",
                              limit=self.max_tool_calls)
                    outcome.verdict = _unresolvable(
                        item.exception_id,
                        f"tool budget of {self.max_tool_calls} calls exhausted",
                    )
                    break
                messages = self._run_tools(read_calls, messages, evidence, outcome, item)
                continue

            # Neither a verdict nor a tool call: prose, or nothing at all.
            if retried_malformed:
                self._log(outcome, item, "malformed_verdict_final")
                outcome.verdict = _unresolvable(
                    item.exception_id,
                    "model did not submit a verdict after one corrective retry",
                )
                break
            retried_malformed = True
            self._log(outcome, item, "malformed_verdict_retry",
                      text=response.text[:200])
            messages = [*messages, {"role": "user", "content": RETRY_PROMPT}]

        outcome.verdict.evidence = evidence
        outcome.verdict.tokens_used = outcome.tokens_used
        return outcome

    def investigate_all(
        self, items: list[ExceptionItem]
    ) -> list[InvestigationOutcome]:
        """Investigate a batch, winding down cleanly if quota runs out.

        Exceptions after the wall are marked quota-exhausted rather than left
        missing, so the scorecard reports a partial run as a partial run.
        """
        outcomes: list[InvestigationOutcome] = []
        exhausted = False
        for item in items:
            if exhausted:
                outcomes.append(
                    InvestigationOutcome(
                        verdict=_unresolvable(
                            item.exception_id,
                            "unresolvable - daily request quota exhausted",
                        )
                    )
                )
                continue
            try:
                outcomes.append(self.investigate(item))
            except QuotaExhausted as wall:
                exhausted = True
                outcomes.append(
                    InvestigationOutcome(
                        verdict=_unresolvable(
                            item.exception_id,
                            f"unresolvable - daily request quota exhausted ({wall})",
                        )
                    )
                )
        return outcomes

    # -- internals ---------------------------------------------------------

    def _ask(
        self,
        messages: list[dict[str, Any]],
        force_verdict: bool,
        item: ExceptionItem,
        outcome: InvestigationOutcome,
    ) -> LLMResponse:
        """One model turn, retried once on a transient API error."""
        force = SUBMIT_VERDICT if force_verdict else None
        try:
            return self.client.generate(SYSTEM_PROMPT, messages, ALL_TOOLS, force)
        except QuotaExhausted:
            raise
        except Exception as error:  # noqa: BLE001
            if not _is_transient(error):
                raise
            self._log(outcome, item, "api_error_retry", error=str(error))
            # Back off first. Retrying a 429 immediately is how one rate-limit
            # error becomes two, and the budget guard cannot pace a retry it
            # was never told about.
            time.sleep(TRANSIENT_BACKOFF_SECONDS)
            return self.client.generate(SYSTEM_PROMPT, messages, ALL_TOOLS, force)

    def _run_tools(
        self,
        calls: list[ToolCall],
        messages: list[dict[str, Any]],
        evidence: list[Evidence],
        outcome: InvestigationOutcome,
        item: ExceptionItem,
    ) -> list[dict[str, Any]]:
        """Execute the model's tool calls and append the results."""
        updated = list(messages)
        for call in calls:
            if outcome.tool_calls_used >= self.max_tool_calls:
                break
            outcome.tool_calls_used += 1
            result = self._invoke(call)
            summary = json.dumps(result, sort_keys=True)[:400]
            evidence.append(
                Evidence(tool=call.name, args=call.args, result_summary=summary)
            )
            self._log(outcome, item, "tool_call", tool=call.name, args=call.args,
                      call_number=outcome.tool_calls_used)
            updated.append(
                {"role": "tool", "name": call.name,
                 "content": json.dumps(result, sort_keys=True)}
            )
        return updated

    def _invoke(self, call: ToolCall) -> dict[str, Any]:
        """Call one tool. A bad argument is data, not a crash."""
        method = getattr(self.toolbox, call.name, None)
        if method is None:
            return {"error": "unknown_tool", "detail": call.name}
        try:
            return method(**call.args)
        except TypeError as error:
            return {"error": "bad_argument", "detail": str(error)}

    @staticmethod
    def _find_verdict_call(response: LLMResponse) -> ToolCall | None:
        for call in response.tool_calls:
            if call.name == SUBMIT_VERDICT:
                return call
        return None

    def _build_verdict(
        self, item: ExceptionItem, call: ToolCall, evidence: list[Evidence]
    ) -> Verdict:
        """Turn the submitted arguments into a Verdict.

        A structurally invalid submission becomes `unresolvable` here rather
        than reaching the verifier. The verifier's job is checking arithmetic
        against records, not repairing malformed input, and a verdict that
        cannot be parsed has claimed nothing to check.
        """
        args = call.args or {}
        rejected = [
            RejectedHypothesis(
                hypothesis=str(entry.get("hypothesis", "")),
                reason=str(entry.get("reason", "")),
            )
            for entry in args.get("hypotheses_rejected") or []
            if isinstance(entry, dict)
        ]
        hypothesis = str(args.get("hypothesis") or "no hypothesis given")
        confidence = args.get("confidence")

        if confidence not in ("resolved", "probable", "unresolvable"):
            return _unresolvable(
                item.exception_id,
                f"verdict rejected: unknown confidence {confidence!r}",
                rejected,
            )
        if confidence == "unresolvable":
            return Verdict(
                exception_id=item.exception_id, hypothesis=hypothesis,
                hypotheses_rejected=rejected, confidence="unresolvable",
            )

        match = self._build_match(args)
        if match is None:
            return _unresolvable(
                item.exception_id,
                f"verdict rejected: {confidence!r} but the proposed match was "
                "incomplete",
                rejected,
            )
        return Verdict(
            exception_id=item.exception_id, hypothesis=hypothesis,
            hypotheses_rejected=rejected, confidence=confidence,
            proposed_match=match,
        )

    @staticmethod
    def _build_match(args: dict[str, Any]) -> ProposedMatch | None:
        """Assemble the proposed match, or None if it is unusable."""
        block = args.get("arithmetic")
        payment_ids = args.get("payment_ids")
        if not isinstance(block, dict) or not payment_ids:
            return None
        try:
            arithmetic = Arithmetic(
                gross=block["gross"], mdr=block["mdr"], gst=block["gst"],
                rounding=block.get("rounding") or "0.00", net=block["net"],
            )
            return ProposedMatch(
                bank_txn_id=str(args["bank_txn_id"]),
                extra_bank_txn_ids=[str(b) for b in args.get("extra_bank_txn_ids") or []],
                payment_ids=[str(p) for p in payment_ids],
                fee_schedule=str(args["fee_schedule"]),
                arithmetic=arithmetic,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _brief(self, item: ExceptionItem) -> str:
        """The opening message: what the matcher saw and why it gave up."""
        lines = [
            f"Exception {item.exception_id}.",
            f"The deterministic matcher stopped with reason: {item.reason}.",
            f"Detail: {item.detail}" if item.detail else "",
        ]
        if item.bank_txn_id:
            lines.append(f"Unmatched bank credit: {item.bank_txn_id}")
            lines.append(json.dumps(self.toolbox.get_bank_txn(item.bank_txn_id),
                                    sort_keys=True))
        if item.settlement_id:
            lines.append(f"Unmatched settlement: {item.settlement_id}")
            lines.append(json.dumps(self.toolbox.get_settlement(item.settlement_id),
                                    sort_keys=True))
        lines.append("Investigate, then call submit_verdict.")
        return "\n".join(line for line in lines if line)

    @staticmethod
    def _log(
        outcome: InvestigationOutcome, item: ExceptionItem, event_type: str,
        **payload: Any,
    ) -> None:
        outcome.events.append(
            {"layer": "layer2", "record_ref": item.exception_id,
             "event_type": event_type, "payload": payload}
        )
