"""Closo — Streamlit entry point.

Placeholder shell for Stage 0. The five screens of CLAUDE.md §10 are
stubbed so navigation exists from the first commit; each is filled in
as its backing stage lands.
"""

import streamlit as st

# Global colour code (CLAUDE.md §10) — one terminal state, one colour, everywhere.
COLOR_AUTO_MATCHED = "#C0DD97"  # green  — Layer 1, deterministic
COLOR_AGENT_VERIFIED = "#FAC775"  # amber  — Layer 2 proposal that passed Layer 3
COLOR_ESCALATED = "#F09595"  # red    — unresolved, honestly reported

SCREENS = [
    "Ingest",
    "Live run",
    "Scorecard",
    "Exception drill-down",
    "Escalation queue",
]

STAGE_FOR_SCREEN = {
    "Ingest": "Stage 4",
    "Live run": "Stage 8",
    "Scorecard": "Stage 4",
    "Exception drill-down": "Stage 8",
    "Escalation queue": "Stage 8",
}


def main() -> None:
    """Render the placeholder shell."""
    st.set_page_config(page_title="Closo", page_icon="🧾", layout="wide")

    screen = st.sidebar.radio("Closo", SCREENS)
    st.sidebar.caption("Self-verifying three-way reconciliation")

    st.title(f"Closo — {screen}")
    st.info(
        f"Placeholder. This screen lands in {STAGE_FOR_SCREEN[screen]}.",
        icon="🚧",
    )

    st.caption(
        "The agent proposes; a separate deterministic verifier disposes. "
        "Nothing is reported resolved unless the math was re-checked independently."
    )


if __name__ == "__main__":
    main()
