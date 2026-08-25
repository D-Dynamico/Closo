"""Closo — Streamlit entry point.

Two screens are live: Ingest and Scorecard. The rest are stubs that name
the stage they arrive in, so the sidebar shows the whole shape of the
product rather than pretending the built half is all there is.

The colour code is global and fixed (WORKFLOWS 10): green for
deterministic matches, amber for agent proposals that passed verification,
red for escalations. One colour per terminal state, everywhere, so a
glance at any screen means the same thing.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from closo.audit import AuditLog
from closo.config import DB_PATH, DEMO_DIR, DEMO_MODE, DEMO_SEED
from closo.dataset_io import load_batch
from closo.layer2_investigator import Investigator
from closo.metrics import Scorecard, score_demo
from closo.pipeline import RunOutcome, replay, run_demo
from closo.response_cache import DEMO_CACHE_PATH, JSONResponseStore, CachedLLMClient
from closo.tools import ToolBox

COLOR_AUTO = "#C0DD97"       # green  — Layer 1, deterministic
COLOR_VERIFIED = "#FAC775"   # amber  — Layer 2 proposal that passed Layer 3
COLOR_ESCALATED = "#F09595"  # red    — unresolved, honestly reported

SCREENS = ["Ingest", "Live run", "Scorecard", "Exception drill-down", "Escalation queue"]
PENDING_STAGE = {
    "Live run": "Stage 8",
    "Exception drill-down": "Stage 8",
    "Escalation queue": "Stage 8",
}


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
    elif screen == "Scorecard":
        screen_scorecard()
    else:
        st.info(f"Placeholder. This screen lands in {PENDING_STAGE[screen]}.", icon="🚧")


if __name__ == "__main__":
    main()
