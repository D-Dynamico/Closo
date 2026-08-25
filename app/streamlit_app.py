"""Closo — Streamlit entry point.

All five screens (WORKFLOWS 10). Ingest and Scorecard read the run in
session state; Live run, the drill-down and the escalation queue are built
on the audit log instead, through `closo.narration`, so a replayed run and
a live one render through identical code - and so a settlement-side
exception, which has no bank credit and therefore no resolutions row, still
has its investigation to show.

The colour code is global and fixed (WORKFLOWS 10): green for
deterministic matches, amber for agent proposals that passed verification,
red for escalations. One colour per terminal state, everywhere, so a
glance at any screen means the same thing.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from closo.audit import AuditLog
from closo.config import DB_PATH, DEMO_DIR, DEMO_MODE, DEMO_SEED
from closo.dataset_io import load_batch
from closo.layer2_investigator import Investigator
from closo.metrics import Scorecard, score_demo
from closo.narration import (
    ExceptionStory,
    RunStory,
    Step,
    escalations,
    narrate,
    unblock_hint,
)
from closo.pipeline import RunOutcome, replay, run_demo
from closo.response_cache import DEMO_CACHE_PATH, JSONResponseStore, CachedLLMClient
from closo.tools import ToolBox

COLOR_AUTO = "#C0DD97"       # green  — Layer 1, deterministic
COLOR_VERIFIED = "#FAC775"   # amber  — Layer 2 proposal that passed Layer 3
COLOR_ESCALATED = "#F09595"  # red    — unresolved, honestly reported

SCREENS = ["Ingest", "Live run", "Scorecard", "Exception drill-down", "Escalation queue"]

# Pacing for the Live-run screen. The verifier's line is deliberately the
# slow one (10): verification has to *read* as a separate step, because the
# claim this whole project makes is that it is one. Set CLOSO_PACING=0 to
# render instantly - the scripted UI tests do, or they would spend a minute
# each watching sleeps.
_PACING = float(os.getenv("CLOSO_PACING", "1") or 0)
STEP_DELAY = 0.06 * _PACING
VERIFIER_DELAY = 0.30 * _PACING
COUNTER_DELAY = 0.05 * _PACING


@st.cache_resource
def get_audit_log() -> AuditLog:
    """One audit log per server process."""
    return AuditLog(DB_PATH)


@st.cache_resource
def get_response_cache() -> JSONResponseStore:
    """The recorded model responses the demo replays from."""
    return JSONResponseStore(DEMO_CACHE_PATH)


def build_investigator(batch) -> Investigator | None:
    """Layer 2, backed by cached responses. None when nothing is cached.

    The client holds no API key and no SDK handle, so pressing Run here
    cannot reach a network however the room's wifi is behaving. With an
    empty cache this returns None and the run is Layer 1 only, reported as
    such - which is honest, where an investigator that could only miss
    would produce a queue of `unresolvable` verdicts that say nothing
    about the data.
    """
    store = get_response_cache()
    if not len(store):
        return None
    return Investigator(ToolBox(batch), CachedLLMClient(store))


@st.cache_data
def load_sources() -> dict:
    """Source record counts and date ranges for the Ingest cards."""
    batch = load_batch(DEMO_DIR)
    return {
        "Razorpay payments": {
            "records": len(batch.payments),
            "from": min(p.captured_at for p in batch.payments),
            "to": max(p.captured_at for p in batch.payments),
        },
        "Bank statement": {
            "records": len(batch.bank_txns),
            "from": min(b.value_date for b in batch.bank_txns),
            "to": max(b.value_date for b in batch.bank_txns),
        },
        "Order ledger": {
            "records": len(batch.orders),
            "from": min(o.order_date for o in batch.orders),
            "to": max(o.order_date for o in batch.orders),
        },
    }


def rupees(amount: Decimal) -> str:
    """Format an amount for display. Never used for arithmetic."""
    return f"₹{amount:,.2f}"


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------


def screen_ingest() -> None:
    """Three source cards and one button. Nothing else (WORKFLOWS 10)."""
    st.subheader("Sources")
    sources = load_sources()
    for column, (name, info) in zip(st.columns(3), sources.items()):
        with column:
            st.metric(name, f"{info['records']} records")
            st.caption(f"{info['from']} → {info['to']}")

    st.divider()
    left, right = st.columns([1, 1])

    with left:
        if st.button("Run reconciliation", type="primary", width="stretch"):
            _execute_run()

    with right:
        log = get_audit_log()
        previous = log.latest_run_id()
        disabled = previous is None
        if st.button(
            "Replay last run", width="stretch", disabled=disabled,
            help="Rebuilds the last run from the audit log — no network, no re-run",
        ):
            _execute_replay(previous)
        if disabled:
            st.caption("No previous run to replay yet.")
        else:
            st.caption(f"Last run: `{previous}`")

    if DEMO_MODE:
        cached = len(get_response_cache())
        layer2 = (
            f"Layers 2 and 3 replay {cached} recorded model responses"
            if cached else
            "No recorded responses cached, so this run is Layer 1 only"
        )
        st.info(
            f"Demo mode: seed {DEMO_SEED}, no network calls. {layer2}. Every "
            "number is reproducible run to run.",
            icon="🔒",
        )


def _execute_run() -> None:
    """Run the pipeline and stash the result for the Scorecard screen."""
    log = get_audit_log()
    with st.spinner("Reconciling…"):
        batch = load_batch(DEMO_DIR)
        outcome = run_demo(audit=log, investigator=build_investigator(batch))
        card = score_demo(outcome, batch)

    st.session_state["outcome"] = outcome
    st.session_state["scorecard"] = card
    st.session_state["run_id"] = outcome.run_id
    st.session_state["replayed"] = False

    st.success(
        f"Reconciled {card.total_bank_txns} bank credits in "
        f"{outcome.elapsed_seconds * 1000:.0f} ms — {card.auto_matched} auto-matched, "
        f"{card.agent_verified} agent-resolved and verified, "
        f"{card.escalated} escalated. See the Scorecard.",
        icon="✅",
    )


def _execute_replay(run_id: str) -> None:
    """Rebuild a past run from the audit log.

    Deliberately does not re-execute the cascade. If the network dies on
    stage, a replay has to be the same run played back, not a second run
    that might disagree with the first.
    """
    log = get_audit_log()
    outcome = replay(run_id, log)
    card = score_demo(outcome, load_batch(DEMO_DIR))

    st.session_state["outcome"] = outcome
    st.session_state["scorecard"] = card
    st.session_state["run_id"] = run_id
    st.session_state["replayed"] = True

    st.success(
        f"Replayed `{run_id}` from the audit log — {card.total_bank_txns} credits, "
        f"{card.auto_matched} auto-matched, {card.agent_verified} agent-resolved "
        f"and verified, {card.escalated} escalated.",
        icon="⏮️",
    )


def screen_scorecard() -> None:
    """Headline metrics, the tier bar, and the exception taxonomy."""
    card: Scorecard | None = st.session_state.get("scorecard")
    outcome: RunOutcome | None = st.session_state.get("outcome")
    if card is None or outcome is None:
        st.info("No run yet. Head to **Ingest** and press Run reconciliation.", icon="👈")
        return

    if st.session_state.get("replayed"):
        st.caption("⏮️ Replayed from the audit log — not a live run.")

    top = st.columns(4)
    top[0].metric("Match rate", f"{card.match_rate:.1%}")
    top[1].metric(
        "Verified accuracy", f"{card.verified_accuracy:.1%}",
        help="Of everything resolved, the share matching ground truth exactly.",
    )
    top[2].metric("₹ reconciled", rupees(card.money_reconciled))
    top[3].metric(
        "₹ stuck", rupees(card.money_stuck),
        delta=f"{card.escalated} credits", delta_color="inverse",
    )

    st.plotly_chart(_tier_bar(card), width="stretch")

    if card.false_resolutions:
        st.error(
            f"{card.false_resolutions} designed-unresolvable record(s) were marked "
            "resolved. That is a critical bug, not a good run.",
            icon="🚨",
        )

    st.subheader("Escalations")
    left, right = st.columns(2)
    left.metric(
        "Correctly escalated", card.correct_escalations,
        help="Genuinely unresolvable by anyone — money absent, or a foreign credit.",
    )
    right.metric(
        "Falsely escalated", card.false_escalations,
        help="Resolvable in principle. Reported in full, never discounted.",
    )
    if card.pending_investigation:
        st.warning(
            f"{card.pending_investigation} of those {card.false_escalations} are "
            "waiting on the Layer 2 investigator, which did not run. They are "
            "still counted as false escalations — the work is undone either way.",
            icon="🚧",
        )

    if card.awaiting_signoff:
        st.info(
            f"{card.awaiting_signoff} of the {card.agent_verified} agent resolutions "
            f"carry {rupees(card.money_awaiting_signoff)} of **verified math and "
            "unverified intent** — the arithmetic reproduces the credit exactly, but "
            "the fee schedule that produced it was not the one active on the date. "
            "Counted as resolved, and flagged for a human to approve.",
            icon="✍️",
        )

    st.subheader("Exception taxonomy")
    st.dataframe(_taxonomy_frame(card), width="stretch", hide_index=True)

    _cost_line(card, outcome)


def _cost_line(card: Scorecard, outcome: RunOutcome) -> None:
    """Throughput and cost (9.2).

    Requests are shown before tokens because requests are the scarce
    resource on this quota, and shown even when zero: a run replaying
    cached responses spent nothing, and that is a fact about the run worth
    stating rather than an empty field.
    """
    st.caption(
        f"Run `{card.run_id}` · seed {card.seed} · "
        f"{card.total_bank_txns} credits in {outcome.elapsed_seconds * 1000:.0f} ms "
        f"({card.records_per_minute:,.0f} records/min overall, "
        f"{card.layer1_records_per_minute:,.0f} for Layer 1 alone) · "
        f"₹ reconciled + ₹ stuck = {rupees(card.money_reconciled + card.money_stuck)}"
    )
    st.caption(
        f"Cost · {card.requests_made} API request(s) and {card.cache_hits} cache hit(s) "
        f"for {card.exceptions_investigated} investigation(s), "
        f"{card.exceptions_skipped} skipped as already covered · "
        f"{card.tokens_used:,} tokens ({card.tokens_per_record:,.0f}/record) · "
        f"{rupees(card.rupees_spent)} total, {rupees(card.rupees_per_record)}/record "
        + ("(free tier)" if card.rupees_spent == 0 else "")
    )
    if card.quota_exhausted:
        st.warning(
            "The daily request quota ran out partway through this run. The "
            "remaining exceptions are marked unresolvable for that reason and "
            "not because anything was concluded about them.",
            icon="⏳",
        )


def _tier_bar(card: Scorecard) -> go.Figure:
    """Horizontal stacked bar: how the batch split across terminal states."""
    figure = go.Figure()
    tiers = [
        ("Auto-matched", card.auto_matched, COLOR_AUTO),
        ("Agent + verified", card.agent_verified, COLOR_VERIFIED),
        ("Escalated", card.escalated, COLOR_ESCALATED),
    ]
    for label, value, color in tiers:
        figure.add_bar(
            y=["Batch"], x=[value], name=label, orientation="h",
            marker_color=color, text=[str(value) if value else ""],
            textposition="inside", hovertemplate=f"{label}: %{{x}}<extra></extra>",
        )
    figure.update_layout(
        barmode="stack", height=140,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.05),
        xaxis_title=None, yaxis_title=None, showlegend=True,
    )
    figure.update_yaxes(showticklabels=False)
    return figure


def _taxonomy_frame(card: Scorecard) -> pd.DataFrame:
    """Per-error-class outcome table, ordered E1…E10."""
    rows = []
    for name, breakdown in card.taxonomy.items():
        rows.append(
            {
                "Class": name,
                "Records": breakdown.total,
                "Auto-matched": breakdown.auto_matched,
                "Agent + verified": breakdown.agent_verified,
                "Escalated": breakdown.escalated,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["_order"] = frame["Class"].str.extract(r"(\d+)").astype(float)
    return frame.sort_values("_order").drop(columns="_order")


# --------------------------------------------------------------------------
# Live run (WORKFLOWS 10, screen 2)
# --------------------------------------------------------------------------


def current_story() -> RunStory | None:
    """The story of the run on screen, read from the audit log.

    Every screen below is built on this rather than on the in-memory
    outcome, so a replayed run and a live one render through identical
    code. The log is the only source that carries a settlement-side
    exception's investigation at all (9.1).
    """
    run_id = st.session_state.get("run_id")
    if not run_id:
        return None
    return narrate(get_audit_log().read_events(run_id))


def screen_live_run() -> None:
    """Layer 1's counter, then the exception queue, paced.

    The pacing is deliberate and the verifier's line is deliberately late
    (10): verification has to *read* as a separate step, because the claim
    this project makes is that it is one. It plays once per run - replaying
    the animation on every sidebar click would be theatre rather than
    information - and the button puts it back.
    """
    story = current_story()
    if story is None:
        st.info("No run yet. Head to **Ingest** and press Run reconciliation.", icon="👈")
        return

    run_id = st.session_state["run_id"]
    played = st.session_state.get("played_run") == run_id
    if played and st.button("Play again"):
        played = False
    st.session_state["played_run"] = run_id

    _render_layer1(story, animate=not played)

    if not story.investigated and not any(t.skipped for t in story.exceptions):
        st.warning(
            "Layer 2 did not run, so the exceptions below carry no "
            "investigation. They are escalated as unexamined, not as unsolvable.",
            icon="🚧",
        )

    st.subheader(f"Exception queue — {len(story.exceptions)}")
    for told in story.exceptions:
        _render_exception(told, animate=not played)


def _render_layer1(story: RunStory, animate: bool) -> None:
    """The fast counter. Layer 1's speed is part of the argument (9.2)."""
    st.subheader("Layer 1 — deterministic cascade")
    counter = st.empty()
    if animate:
        for shown in _counter_steps(story.layer1.matched):
            counter.metric("Matched", f"{shown} / {story.layer1.total_bank_txns}")
            time.sleep(COUNTER_DELAY)
    counter.metric(
        "Matched", f"{story.layer1.matched} / {story.layer1.total_bank_txns}",
        delta=f"{story.layer1.match_rate:.1%} auto-matched",
    )
    passes = " · ".join(
        f"{name.split('_')[0]}: {count}"
        for name, count in sorted(story.layer1.passes.items())
    )
    st.caption(f"{passes} — {story.layer1.exceptions} exceptions left for Layer 2")


def _counter_steps(target: int, steps: int = 8) -> list[int]:
    """A handful of intermediate values, never more than the target."""
    if target <= 0:
        return []
    stride = max(1, target // steps)
    return [*range(stride, target, stride)]


def _render_exception(told: ExceptionStory, animate: bool) -> None:
    """One exception, as an expandable block that ends on the verifier."""
    header = (
        f"{told.exception_id} · {told.record_ref} · {told.reason}"
        f" — {_status_summary(told)}"
    )
    with st.status(header, expanded=True, state=_status_state(told)):
        if told.detail:
            st.caption(told.detail)
        for step in told.steps:
            if animate:
                time.sleep(VERIFIER_DELAY if step.kind == "verification" else STEP_DELAY)
            _render_step(step)
        st.markdown(f"**{told.outcome_label}**")


def _render_step(step: Step) -> None:
    if step.kind == "tool_call":
        st.markdown(f"`{step.label}`")
    elif step.kind == "verdict":
        st.markdown(f"→ {step.label}")
    elif step.kind == "verification":
        mark = {True: "✓", False: "✗", None: "—"}[step.ok]
        st.markdown(f"**{mark} verifier** · {step.label}")
    else:
        st.markdown(f"_{step.label}_")


def _status_summary(told: ExceptionStory) -> str:
    """The outcome, in words, in the block's own header.

    `st.status` draws its own tick for a completed block, meaning "this
    finished" - which on a screen where ✓ and ✗ are the *verifier's*
    vocabulary reads as "this reconciled". Two of them side by side was
    worse still. So the mark is left to Streamlit and the meaning is
    carried by words, which cannot be misread as a verdict.
    """
    if told.skipped:
        return "already covered"
    if told.verified and told.effective_confidence == "probable":
        return "verified, needs sign-off"
    if told.verified:
        return "verified"
    if told.rejection_reason:
        return "verifier rejected"
    return "escalated"


def _status_state(told: ExceptionStory) -> str:
    """`st.status` state. Only a verifier rejection is an error.

    An exception nobody could explain is not a failure of the run - E9 and
    E10 are supposed to end here - so they are marked complete, not errored.
    """
    return "error" if told.rejection_reason else "complete"


# --------------------------------------------------------------------------
# Exception drill-down (WORKFLOWS 10, screen 4)
# --------------------------------------------------------------------------


def screen_drilldown() -> None:
    """One exception, in full: hypothesis, evidence, arithmetic, verification.

    This is the screen the pitch rests on. It has to show the whole chain
    for a single record - what was considered and dropped, what was
    actually called and what came back, the arithmetic, and then a
    verifier's checklist that was computed from the raw records rather than
    from any of the above.
    """
    story = current_story()
    if story is None:
        st.info("No run yet. Head to **Ingest** and press Run reconciliation.", icon="👈")
        return

    investigated = [told for told in story.exceptions if told.investigated]
    if not investigated:
        st.warning(
            "Layer 2 did not run for this run, so there is no investigation to "
            "show. Every exception is escalated as unexamined.",
            icon="🚧",
        )
        return

    labels = {
        f"{t.exception_id} · {t.record_ref} · {t.reason}": t for t in investigated
    }
    chosen = st.selectbox("Exception", list(labels), index=_default_index(labels))
    told = labels[chosen]
    verdict = _verdict_of(told)

    st.markdown(f"### {told.exception_id} — {told.record_ref}")
    st.caption(f"Layer 1 stopped here: **{told.reason}** — {told.detail}")

    st.markdown("#### Hypothesis")
    st.markdown(verdict.get("hypothesis") or "_none recorded_")

    rejected = verdict.get("hypotheses_rejected") or []
    if rejected:
        st.markdown("#### Considered and ruled out")
        for entry in rejected:
            st.markdown(
                f"- ~~{entry.get('hypothesis', '')}~~ — {entry.get('reason', '')}"
            )

    st.markdown("#### Evidence")
    evidence = verdict.get("evidence") or []
    if evidence:
        for item in evidence:
            with st.expander(f"{item.get('tool')}({_args(item.get('args'))})"):
                st.code(item.get("result_summary", ""), language="json")
    else:
        for step in told.tool_calls:
            st.markdown(f"`{step.label}`")

    _render_arithmetic(verdict)
    _render_checklist(told)


def _default_index(labels: dict) -> int:
    """Open on a sign-off case when there is one.

    An E4 tells the best story on this screen - proven math, unproven
    intent - and it is what §13's Stage 9 says to show on stage.
    """
    for index, told in enumerate(labels.values()):
        if told.schedule_anomaly:
            return index
    return 0


def _verdict_of(told: ExceptionStory) -> dict:
    for step in told.steps:
        if step.kind == "verdict":
            return step.detail
    return {}


def _args(args: dict | None) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted((args or {}).items()))


def _render_arithmetic(verdict: dict) -> None:
    """The claimed arithmetic, monospaced (10).

    Labelled a claim on the screen as well as in the code, because the next
    section is what happens when someone checks it.
    """
    match = verdict.get("proposed_match") or {}
    block = match.get("arithmetic") or {}
    if not block:
        return

    st.markdown("#### Arithmetic — the agent's claim")
    st.code(
        f"payments   {', '.join(match.get('payment_ids', []))}\n"
        f"schedule   {match.get('fee_schedule')}\n"
        f"gross      {block.get('gross'):>14}\n"
        f"mdr        {block.get('mdr'):>14}\n"
        f"gst        {block.get('gst'):>14}\n"
        f"rounding   {block.get('rounding', '0.00'):>14}\n"
        f"net        {block.get('net'):>14}",
        language="text",
    )


def _render_checklist(told: ExceptionStory) -> None:
    """The verifier's own checks, one line each.

    Recomputed from raw records - not read off the block above. That is the
    whole claim, so the screen says it in the heading rather than leaving
    it to be inferred.
    """
    st.markdown("#### Verification — recomputed from raw records")
    stamp = next((s for s in told.steps if s.kind == "verification"), None)
    if stamp is None:
        st.markdown("_this verdict never reached the verifier_")
        return

    checks = (stamp.detail.get("result") or {}).get("checks") or []
    for check in checks:
        mark = "✓" if check.get("passed") else "✗"
        st.markdown(f"{mark} **{check.get('check')}** — {check.get('detail')}")

    if told.schedule_anomaly:
        st.warning(
            f"{told.schedule_anomaly}. The arithmetic is proven; whether "
            "applying that schedule was authorised is not something Closo can "
            "know, so this is capped at **probable** and handed to a human.",
            icon="✍️",
        )
    st.markdown(f"**{told.outcome_label}**")


# --------------------------------------------------------------------------
# Escalation queue (WORKFLOWS 10, screen 5)
# --------------------------------------------------------------------------


def screen_escalations() -> None:
    """What is still open, what was tried, and what would unblock it.

    Rejections sort first. "The agent proposed this and the verifier threw
    it out" is the most informative row here and the one a human should
    read before anything else (10.5).
    """
    story = current_story()
    if story is None:
        st.info("No run yet. Head to **Ingest** and press Run reconciliation.", icon="👈")
        return

    open_items = escalations(story)
    if not open_items:
        st.success("Nothing escalated in this run.", icon="✅")
        return

    rejected = [t for t in open_items if t.rejection_reason]
    st.markdown(
        f"**{len(open_items)}** open — "
        f"{len(rejected)} proposed and rejected by the verifier, "
        f"{len(open_items) - len(rejected)} nobody could explain."
    )
    st.caption(
        "An empty list would be the suspicious result. Two error classes in "
        "this dataset are unresolvable by construction; a run that resolves "
        "one has a bug."
    )

    for told in open_items:
        _render_escalation(told)


def _render_escalation(told: ExceptionStory) -> None:
    icon = "✗" if told.rejection_reason else "—"
    label = f"{icon} {told.exception_id} · {told.record_ref} · {told.reason}"
    with st.expander(label, expanded=bool(told.rejection_reason)):
        if told.rejection_reason:
            st.error(
                f"Agent proposed, verifier rejected: **{told.rejection_reason}**",
                icon="🛑",
            )
        verdict = _verdict_of(told)
        if verdict.get("hypothesis"):
            st.markdown(f"**What the agent concluded:** {verdict['hypothesis']}")

        rejected = verdict.get("hypotheses_rejected") or []
        if rejected:
            st.markdown("**Tried and ruled out:**")
            for entry in rejected:
                st.markdown(
                    f"- ~~{entry.get('hypothesis', '')}~~ — {entry.get('reason', '')}"
                )
        elif not told.investigated:
            st.markdown("_Not investigated._")

        st.info(f"**What would unblock this:** {unblock_hint(told)}", icon="🔑")


def main() -> None:
    st.set_page_config(page_title="Closo", page_icon="🧾", layout="wide")

    screen = st.sidebar.radio("Closo", SCREENS)
    st.sidebar.caption("Self-verifying three-way reconciliation")
    st.sidebar.divider()
    st.sidebar.caption(
        "The agent proposes; a separate deterministic verifier disposes. "
        "Nothing is reported resolved unless the math was re-checked independently."
    )

    st.title(f"Closo — {screen}")

    if screen == "Ingest":
        screen_ingest()
    elif screen == "Live run":
        screen_live_run()
    elif screen == "Scorecard":
        screen_scorecard()
    elif screen == "Exception drill-down":
        screen_drilldown()
    else:
        screen_escalations()


if __name__ == "__main__":
    main()
