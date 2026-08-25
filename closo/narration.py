"""Turning an audit log back into the story of a run (WORKFLOWS 10).

The Live-run screen shows Layer 1's counter, then one block per exception
streaming what the investigator did, ending with the verifier's verdict as
a *separate, later* step. All of that is reconstructed here, from the
`events` table alone.

**Reading the log rather than watching the run is the design, not a
shortcut.** A full pipeline run finishes in about 170 milliseconds, so
there is no live process to watch; what there is, is an append-only record
of exactly what happened, in order. Narrating that means a live run and a
replayed one take the identical path through the identical code - which is
what 10.1 asks for, and it is the difference between a demo that shows its
work and one that performs a re-enactment.

Pure functions over event dicts. No Streamlit, no pacing, no formatting
decisions that belong to a screen: the caller decides how slowly to reveal
these and what they look like.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

#: Events the investigator logs against the exception id itself, rather
#: than against a bank credit. Both conventions appear in one run.
EXCEPTION_REF = re.compile(r"^EX-\d+$")

StepKind = Literal["tool_call", "verdict", "verification", "note"]

#: Tool arguments are shown inline, so a long one is trimmed rather than
#: allowed to push the interesting part of the line off the screen.
MAX_ARG_CHARS = 60


@dataclass
class Step:
    """One thing that happened, in the order it happened."""

    kind: StepKind
    label: str
    detail: dict[str, Any] = field(default_factory=dict)

    #: True passed, False failed, None for steps that do not judge - a tool
    #: call is neither good nor bad news, and colouring it as either would
    #: be the screen inventing a conclusion.
    ok: bool | None = None


@dataclass
class ExceptionStory:
    """What happened to one exception, start to finish."""

    exception_id: str
    record_ref: str = ""
    reason: str = ""
    detail: str = ""
    steps: list[Step] = field(default_factory=list)
    confidence: str | None = None
    effective_confidence: str | None = None
    verified: bool | None = None
    rejection_reason: str | None = None
    schedule_anomaly: str | None = None
    investigated: bool = False
    skipped: bool = False

    @property
    def tool_calls(self) -> list[Step]:
        return [step for step in self.steps if step.kind == "tool_call"]

    @property
    def outcome_label(self) -> str:
        """The one line that closes the block on the Live-run screen.

        Four different endings, deliberately worded apart. "The verifier
        rejected this" and "the agent could not answer" are different
        facts about a run, and a screen that renders both as "unresolved"
        throws away the more interesting one (10.5).
        """
        if self.skipped:
            return "already resolved by an earlier verdict - not investigated again"
        if not self.investigated:
            return "not investigated"
        if self.verified and self.effective_confidence == "probable":
            return "verified - probable, needs human sign-off"
        if self.verified:
            return "verified - resolved"
        if self.rejection_reason:
            return f"agent proposed, verifier rejected: {self.rejection_reason}"
        return "agent could not resolve - escalated"


@dataclass
class Layer1Story:
    """The deterministic cascade's own numbers, for the fast counter."""

    total_bank_txns: int = 0
    settlements: int = 0
    matched: int = 0
    exceptions: int = 0
    match_rate: float = 0.0
    passes: dict[str, int] = field(default_factory=dict)


@dataclass
class RunStory:
    """A whole run, ready to be revealed at whatever pace the screen likes."""

    layer1: Layer1Story = field(default_factory=Layer1Story)
    exceptions: list[ExceptionStory] = field(default_factory=list)

    @property
    def investigated(self) -> list[ExceptionStory]:
        return [story for story in self.exceptions if story.investigated]


#: What a human would have to go and do to unstick each kind of exception
#: (10.5). Keyed by the reason Layer 1 gave, because that is the fact the
#: run actually established - "no settlement carries this UTR" is a
#: different errand from "the amount will not reconcile".
UNBLOCK_HINTS: dict[str, str] = {
    "no_counterpart": (
        "the settlement is genuinely absent from the statement - raise it with "
        "Razorpay support, quoting the settlement id and expected amount"
    ),
    "no_utr_match": (
        "no settlement carries this UTR - confirm with the bank whether this "
        "credit came from somewhere other than Razorpay"
    ),
    "no_utr_in_narration": (
        "the narration carries no recoverable reference - ask the bank for the "
        "full remittance details for this credit"
    ),
    "duplicate_utr": (
        "two records share one reference - ask Razorpay which settlement the "
        "UTR belongs to"
    ),
    "outside_tolerance": (
        "the amount does not reconcile under either fee schedule - confirm "
        "which schedule was applied to this payout"
    ),
    "amount_mismatch": (
        "the amount does not reconcile under either fee schedule - confirm "
        "which schedule was applied to this payout"
    ),
    "outside_date_window": (
        "the amount matches but the credit landed outside the T+3 settlement "
        "window - check with the bank whether the payout was delayed"
    ),
    "ambiguous_tie": (
        "two candidates fit equally well and guessing is forbidden - a "
        "stronger identifier is needed to tell them apart"
    ),
}

FALLBACK_HINT = "needs a human to look at the source records"


def unblock_hint(story: "ExceptionStory") -> str:
    """What would unblock this exception, in one sentence.

    A verifier rejection outranks the Layer 1 reason: the useful fact is no
    longer "this did not match" but "something was proposed and did not
    survive checking", which sends a human to a different place.
    """
    if story.rejection_reason:
        return (
            f"the agent proposed a match and the verifier rejected it "
            f"({story.rejection_reason}) - a human should check the cited "
            f"records before anything is accepted"
        )
    return UNBLOCK_HINTS.get(story.reason, FALLBACK_HINT)


def escalations(story: RunStory) -> list["ExceptionStory"]:
    """Everything still open, rejections first.

    A verdict the verifier threw out is the most informative row on the
    screen (10.5) and the one a human should read first, so it is not left
    to sort alphabetically among the credits nobody could explain.
    """
    open_items = [
        told for told in story.exceptions
        if not told.skipped and told.verified is not True
    ]
    return sorted(open_items, key=lambda t: (t.rejection_reason is None, t.exception_id))


def narrate(events: Iterable[dict]) -> RunStory:
    """Rebuild a run's story from its audit events.

    Args:
        events: rows from :meth:`closo.audit.AuditLog.read_events`, in the
            order the log returns them. Order is the whole point - the log
            is ordered by event id rather than timestamp precisely because
            several events share a millisecond, and a re-ordered story is a
            different story from the one that happened.

    Returns:
        The run, or an empty story if the events carry no run at all.
        Unknown event types are ignored rather than raising: the log is
        append-only and older runs will not have every event a later
        version of this code knows about.
    """
    story = RunStory()
    by_id: dict[str, ExceptionStory] = {}

    for event in events:
        payload = event.get("payload") or {}
        kind = event.get("event_type", "")
        ref = str(event.get("record_ref", ""))

        if event.get("layer") == "layer1":
            _absorb_layer1(story, kind, ref, payload, by_id)
            continue

        exception_id = _exception_id(payload, ref)
        if exception_id is None:
            continue
        current = by_id.get(exception_id)
        if current is None:
            current = ExceptionStory(exception_id=exception_id, record_ref=ref)
            by_id[exception_id] = current
            story.exceptions.append(current)
        _absorb_investigation(current, kind, payload)

    return story


# --------------------------------------------------------------------------
# Layer 1
# --------------------------------------------------------------------------


def _absorb_layer1(
    story: RunStory,
    kind: str,
    ref: str,
    payload: dict,
    by_id: dict[str, ExceptionStory],
) -> None:
    if kind == "layer1_started":
        story.layer1.total_bank_txns = int(payload.get("bank_txns", 0) or 0)
        story.layer1.settlements = int(payload.get("settlements", 0) or 0)
    elif kind == "layer1_finished":
        story.layer1.matched = int(payload.get("matched", 0) or 0)
        story.layer1.exceptions = int(payload.get("exceptions", 0) or 0)
        story.layer1.match_rate = float(payload.get("match_rate", 0.0) or 0.0)
    elif kind == "matched":
        pass_used = str(payload.get("pass_used", "?"))
        story.layer1.passes[pass_used] = story.layer1.passes.get(pass_used, 0) + 1
    elif kind == "exception":
        exception_id = str(payload.get("exception_id") or ref)
        current = ExceptionStory(
            exception_id=exception_id,
            record_ref=ref,
            reason=str(payload.get("reason", "")),
            detail=str(payload.get("detail", "")),
        )
        by_id[exception_id] = current
        story.exceptions.append(current)


# --------------------------------------------------------------------------
# Layers 2 and 3
# --------------------------------------------------------------------------


def _exception_id(payload: dict, ref: str) -> str | None:
    """Which exception an event belongs to.

    Two conventions meet here. The investigator logs against the exception
    id; the pipeline logs against the bank credit or settlement and names
    the exception in the payload. Both are correct for their own purpose,
    so the narrator accepts either rather than making one of them change.
    """
    named = payload.get("exception_id")
    if named:
        return str(named)
    return ref if EXCEPTION_REF.match(ref) else None


def _absorb_investigation(story: ExceptionStory, kind: str, payload: dict) -> None:
    if kind == "tool_call":
        story.investigated = True
        story.steps.append(
            Step(
                kind="tool_call",
                label=_tool_label(payload),
                detail={"tool": payload.get("tool"), "args": payload.get("args") or {}},
            )
        )
    elif kind == "verdict_recorded":
        verdict = payload.get("verdict") or {}
        story.investigated = True
        story.confidence = verdict.get("confidence")
        story.steps.append(
            Step(
                kind="verdict",
                label=str(verdict.get("hypothesis") or "no hypothesis given"),
                detail=verdict,
            )
        )
    elif kind == "verified":
        # An `unresolvable` verdict claimed nothing, so it was neither
        # passed nor rejected: `verified` stays None and the step carries no
        # mark. Rendering it as a failure would put a ✗ against the one
        # outcome this project argues is honest, and would credit the
        # verifier with catching something nobody proposed.
        claimed = story.confidence != "unresolvable"
        story.verified = bool(payload.get("passed")) if claimed else None
        story.rejection_reason = payload.get("rejection_reason")
        story.effective_confidence = payload.get("effective_confidence")
        story.schedule_anomaly = payload.get("schedule_anomaly")
        story.steps.append(
            Step(
                kind="verification",
                label=_verification_label(payload, story.confidence),
                detail=dict(payload),
                ok=story.verified,
            )
        )
    elif kind == "verification_recorded":
        # The full checklist rides on the step already appended, so the
        # drill-down and the Live-run block read the same object.
        for step in reversed(story.steps):
            if step.kind == "verification":
                step.detail = {**step.detail, "result": payload.get("result") or {}}
                break
    elif kind == "skipped_already_resolved":
        story.skipped = True
        story.steps.append(
            Step(kind="note", label="covered by an earlier verdict; skipped")
        )
    elif kind == "quota_exhausted":
        story.steps.append(
            Step(
                kind="note",
                label="daily request quota exhausted before this exception",
                ok=False,
            )
        )
    elif kind in ("timeout", "tool_budget_exhausted", "malformed_verdict_retry",
                  "malformed_verdict_final", "api_error_retry", "api_error_final",
                  "verdict_rejected_by_pipeline"):
        story.steps.append(
            Step(kind="note", label=_note_label(kind, payload), detail=dict(payload))
        )


def _tool_label(payload: dict) -> str:
    args = payload.get("args") or {}
    rendered = ", ".join(f"{k}={_trim(v)}" for k, v in sorted(args.items()))
    return f"{payload.get('tool', 'tool')}({rendered})"


def _trim(value: Any) -> str:
    text = str(value)
    return text if len(text) <= MAX_ARG_CHARS else text[: MAX_ARG_CHARS - 1] + "…"


def _verification_label(payload: dict, confidence: str | None) -> str:
    """What the verifier's line says.

    An `unresolvable` verdict claimed nothing, so it was never rejected -
    calling it rejected would credit the verifier with catching something
    that was never proposed, and would put a ✗ against the one outcome the
    project argues is honest.
    """
    if confidence == "unresolvable":
        return "nothing claimed - no arithmetic to verify"
    if payload.get("passed"):
        anomaly = payload.get("schedule_anomaly")
        if anomaly:
            return f"recomputed from raw records and matched - {anomaly}"
        return "recomputed from raw records and matched"
    return f"rejected: {payload.get('rejection_reason') or 'failed verification'}"


def _note_label(kind: str, payload: dict) -> str:
    if kind == "verdict_rejected_by_pipeline":
        return f"verdict refused: {payload.get('detail', 'unusable')}"
    return kind.replace("_", " ")
