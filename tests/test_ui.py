"""Scripted UI tests for all five screens (TEST_PLAN 12.7).

These exist because booting the server and asserting HTTP 200 proves
almost nothing. The app loaded fine while the first click of "Run
reconciliation" raised a threading error, and the Replay button rendered
perfectly while doing nothing at all. Both failures waited behind a
button, which is where a demo finds them.

``AppTest`` drives the real script, so these press the buttons.
"""

from __future__ import annotations

from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = st_testing.AppTest

APP = str(Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py")
TIMEOUT = 120


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A fresh app against a throwaway audit database.

    Without redirecting the database each test would append to the real
    closo.db and "Replay last run" would find a run from a previous test.

    Clearing Streamlit's caches is the other half, and it was missing.
    `get_audit_log` is a `@st.cache_resource`, and that cache lives for the
    life of the *process* rather than the AppTest - so every test after the
    first was quietly handed the first test's database, redirected path or
    not. Nothing failed, because each test was self-consistent inside its
    own run; it surfaced only when a test seeded a database and the app
    read a different one.
    """
    import streamlit as st

    st.cache_resource.clear()
    st.cache_data.clear()
    monkeypatch.setenv("CLOSO_DB", str(tmp_path / "test.db"))
    # Render instantly. The Live-run screen paces itself on purpose, and a
    # suite that sat through every sleep would take minutes to tell us
    # nothing - the ordering of the steps is what these tests check, and
    # that is independent of how slowly they are revealed.
    monkeypatch.setenv("CLOSO_PACING", "0")
    import closo.config as config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    return AppTest.from_file(APP, default_timeout=TIMEOUT)


def labels(elements) -> list[str]:
    return [element.label for element in elements]


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------


def test_app_loads_without_error(app) -> None:
    at = app.run()
    assert not list(at.exception)
    assert at.title[0].value.endswith("Ingest")


def test_sidebar_shows_all_five_screens(app) -> None:
    """The stubs stay visible so the shape of the product is honest about
    what is built and what is not."""
    at = app.run()
    assert len(at.sidebar.radio[0].options) == 5


def test_ingest_shows_three_source_cards(app) -> None:
    at = app.run()
    assert labels(at.metric) == [
        "Razorpay payments", "Bank statement", "Order ledger"
    ]
    assert at.metric[0].value == "150 records"
    assert at.metric[1].value == "47 records"


def test_replay_is_disabled_before_any_run(app) -> None:
    """Offering an escape hatch that leads nowhere is worse than offering
    none, so the button is disabled until there is something to replay."""
    at = app.run()
    assert at.button[1].disabled is True


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def test_run_button_completes_without_error(app) -> None:
    """The regression test for the threading bug. The page loaded fine; it
    was this click that raised."""
    at = app.run()
    at.button[0].click().run()
    assert not list(at.exception), list(at.exception)
    assert at.success


def test_run_enables_replay(app) -> None:
    at = app.run()
    at.button[0].click().run()
    assert at.button[1].disabled is False


def test_scorecard_shows_headline_numbers(app) -> None:
    """Pressing Run drives all three layers, with Layer 2 replaying the
    committed responses. 95.7% is the recorded live run's figure, so this
    also catches the cache drifting out of step with the pipeline."""
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()

    values = {m.label: m.value for m in at.metric}
    assert values["Match rate"] == "95.7%"
    assert values["Verified accuracy"] == "100.0%"
    assert values["₹ reconciled"].startswith("₹")
    assert values["Correctly escalated"] == "2"
    assert values["Falsely escalated"] == "0"


def test_scorecard_renders_the_tier_bar_and_taxonomy(app) -> None:
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    assert len(at.get("plotly_chart")) == 1
    assert len(at.dataframe) == 1


def test_scorecard_declares_the_sign_off_sub_list(app) -> None:
    """The E4 cases resolve on proven math and unproven intent (8.1). The
    screen has to say so: counted as resolved, flagged for a human."""
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    assert any("unverified intent" in i.value for i in at.info)


def test_scorecard_shows_what_the_run_cost(app) -> None:
    """9.2's cost line. Requests before tokens, because requests are what
    is scarce - and a replayed run showing zero requests is stating a fact
    about itself, not leaving a field empty."""
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    cost = [c.value for c in at.caption if c.value.startswith("Cost ")]
    assert cost, [c.value for c in at.caption]
    assert "API request(s)" in cost[0] and "cache hit(s)" in cost[0]
    assert "tokens" in cost[0] and "free tier" in cost[0]


def test_the_run_makes_no_api_requests(app) -> None:
    """The airplane-mode guarantee at the button, not just in the library.
    A cached Layer 2 that quietly reached for the network would produce an
    identical screen - the request count is the only thing that shows it."""
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    cost = next(c.value for c in at.caption if c.value.startswith("Cost "))
    assert cost.startswith("Cost · 0 API request(s)")


def test_all_three_terminal_states_appear_on_the_tier_bar(app) -> None:
    """12.7 asks for all three colours on screen. Amber only exists once
    Layer 2 resolves something a verifier passed, so this is the check
    that the middle tier is real rather than a legend entry."""
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()

    import json

    spec = json.loads(at.get("plotly_chart")[0].proto.spec)
    tiers = {bar["name"]: (bar["marker"]["color"], bar["x"][0]) for bar in spec["data"]}
    assert {color for color, _ in tiers.values()} == {
        "#C0DD97", "#FAC775", "#F09595"
    }
    assert all(count > 0 for _, count in tiers.values()), (
        f"every tier must carry records, or a colour is decoration: {tiers}"
    )


def test_no_critical_banner_on_a_clean_run(app) -> None:
    """The red banner is reserved for a designed-unresolvable being marked
    resolved. On a correct run it must stay silent, or it means nothing."""
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    assert not list(at.error)


def test_scorecard_before_any_run_explains_itself(app) -> None:
    at = app.run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    assert not list(at.exception)
    assert any("Ingest" in i.value for i in at.info)


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


def test_replay_reproduces_the_live_scorecard(app) -> None:
    """The airplane-mode guarantee, exercised through the actual UI."""
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    live = {m.label: m.value for m in at.metric}

    at.sidebar.radio[0].set_value("Ingest").run()
    at.button[1].click().run()
    assert not list(at.exception), list(at.exception)

    at.sidebar.radio[0].set_value("Scorecard").run()
    assert {m.label: m.value for m in at.metric} == live


def test_replay_reports_all_three_states(app) -> None:
    """A replay that named only the matched and escalated counts would
    silently drop Layer 2's six resolutions from the one sentence a
    presenter reads aloud - and Layer 2 is the point."""
    at = app.run()
    at.button[0].click().run()
    at.button[1].click().run()
    message = at.success[0].value
    assert "auto-matched" in message and "agent-resolved and verified" in message


def test_replay_is_labelled_as_a_replay(app) -> None:
    """A replayed scorecard must not be mistakable for a live one."""
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Ingest").run()
    at.button[1].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    assert any("Replayed" in c.value for c in at.caption)


# --------------------------------------------------------------------------
# Live run (10 screen 2)
# --------------------------------------------------------------------------


def after_a_run(app):
    """An app with a completed run behind it, on the Ingest screen."""
    at = app.run()
    at.button[0].click().run()
    return at


def test_live_run_shows_layer_1_before_the_queue(app) -> None:
    at = after_a_run(app)
    at.sidebar.radio[0].set_value("Live run").run()
    assert not list(at.exception), list(at.exception)

    matched = {m.label: m.value for m in at.metric}
    assert matched["Matched"] == "39 / 47"


def test_live_run_gives_every_exception_its_own_block(app) -> None:
    """10 wants the queue visible, all of it. A screen showing only the
    interesting ones is the same screen that would quietly drop an
    exception nobody investigated."""
    at = after_a_run(app)
    at.sidebar.radio[0].set_value("Live run").run()
    assert len(at.get("status")) == 10


def test_the_verifier_line_comes_after_the_tool_calls(app) -> None:
    """The pacing is cosmetic; the ordering is not. Verification has to
    read as a step that happened after the investigation, because that is
    the claim being made."""
    at = after_a_run(app)
    at.sidebar.radio[0].set_value("Live run").run()

    block = at.get("status")[0]
    rendered = [m.value for m in block.get("markdown")]
    verifier = next(i for i, text in enumerate(rendered) if "verifier" in text)
    tools = [i for i, text in enumerate(rendered) if text.startswith("`")]
    assert tools and verifier > max(tools)


def test_an_honest_escalation_is_not_shown_as_an_error(app) -> None:
    """E9 and E10 are supposed to end here. Marking their blocks as errors
    would tell a judge the run failed on the four records where it did
    exactly what it was designed to do."""
    at = after_a_run(app)
    at.sidebar.radio[0].set_value("Live run").run()
    states = [block.state for block in at.get("status")]
    assert states and all(state != "error" for state in states)


def test_live_run_before_any_run_explains_itself(app) -> None:
    at = app.run()
    at.sidebar.radio[0].set_value("Live run").run()
    assert not list(at.exception)
    assert any("Ingest" in i.value for i in at.info)


# --------------------------------------------------------------------------
# Exception drill-down (10 screen 4)
# --------------------------------------------------------------------------


def drilldown(app):
    at = after_a_run(app)
    at.sidebar.radio[0].set_value("Exception drill-down").run()
    return at


def test_drilldown_opens_on_a_sign_off_case(app) -> None:
    """12.7 asks for a full evidence trail on an E4, and 13's Stage 9 says
    that is the one to show on stage. It should be what loads."""
    at = drilldown(app)
    assert not list(at.exception), list(at.exception)
    assert any(w.value for w in at.warning if "was active on" in w.value)


def test_drilldown_renders_the_whole_chain_for_one_record(app) -> None:
    """The screen the pitch rests on: what was considered and dropped,
    what was called and what came back, the arithmetic, and a checklist
    computed from the records rather than from any of it."""
    at = drilldown(app)
    headings = [m.value for m in at.markdown]
    for section in ("#### Hypothesis", "#### Considered and ruled out",
                    "#### Evidence", "#### Arithmetic — the agent's claim",
                    "#### Verification — recomputed from raw records"):
        assert any(text.startswith(section) for text in headings), section


def test_rejected_hypotheses_are_struck_through(app) -> None:
    """10 asks for strikethrough specifically. A hypothesis that was ruled
    out has to look ruled out, or the screen reads as five competing
    explanations the agent could not choose between."""
    at = drilldown(app)
    struck = [m.value for m in at.markdown if m.value.startswith("- ~~")]
    assert struck


def test_the_arithmetic_is_monospaced_and_complete(app) -> None:
    at = drilldown(app)
    blocks = [c.value for c in at.code if c.value.startswith("payments")]
    assert len(blocks) == 1, "exactly one arithmetic block, and it is the claim"
    for field in ("schedule", "gross", "mdr", "gst", "rounding", "net"):
        assert field in blocks[0]


def test_the_verifier_checklist_shows_every_check(app) -> None:
    """Per-check ✓/✗ (10), not a single verdict about the verdict. Five
    checks run; a screen showing one boolean throws away the argument."""
    at = drilldown(app)
    checks = [m.value for m in at.markdown if m.value.startswith(("✓ **", "✗ **"))]
    assert len(checks) == 5


def test_the_drilldown_can_reach_every_investigated_exception(app) -> None:
    at = drilldown(app)
    assert len(at.selectbox[0].options) == 9


# --------------------------------------------------------------------------
# Escalation queue (10 screen 5)
# --------------------------------------------------------------------------


def escalation_screen(app):
    at = after_a_run(app)
    at.sidebar.radio[0].set_value("Escalation queue").run()
    return at


def test_escalation_queue_lists_what_is_still_open(app) -> None:
    at = escalation_screen(app)
    assert not list(at.exception), list(at.exception)
    assert len(at.expander) == 4, (
        "two E10 credits and two E9 settlements. The scorecard counts four "
        "escalations as two, because it counts bank credits and an E9 has "
        "none - but money that never arrived is exactly what belongs in a "
        "queue a human works through"
    )


def test_every_escalation_says_what_would_unblock_it(app) -> None:
    """10.5. "Could not resolve" is a dead end; "ask the bank for the full
    remittance details" is a task someone can pick up."""
    at = escalation_screen(app)
    hints = [i.value for i in at.info if "What would unblock this" in i.value]
    assert len(hints) == 4
    assert any("Razorpay support" in hint for hint in hints)
    assert any("came from somewhere other than Razorpay" in hint for hint in hints)


def test_the_queue_says_an_empty_list_would_be_the_suspicious_result(app) -> None:
    at = escalation_screen(app)
    assert any("unresolvable by construction" in c.value for c in at.caption)


# --------------------------------------------------------------------------
# A verifier rejection, which a clean run does not contain (12.7)
# --------------------------------------------------------------------------


def seed_rejected_run(db_path, run_id: str = "run_rejected") -> None:
    """Write a run whose verdict the verifier threw out.

    The cached demo run contains no rejection - every verdict either
    verified or was honestly unresolvable - so the one screen element 12.7
    names specifically has nothing to render. Rather than ship a fake
    rejection in the demo data, the test writes one into its own throwaway
    audit database and drives the real screen over it.
    """
    from closo.audit import AuditLog

    verdict = {
        "exception_id": "EX-001",
        "hypothesis": "settlement setl_9 net of a partial refund",
        "confidence": "resolved",
        "hypotheses_rejected": [
            {"hypothesis": "settlement lag", "reason": "no settlement in the window"},
        ],
        "evidence": [
            {"tool": "get_payment", "args": {"payment_id": "pay_1"},
             "result_summary": '{"error": "not_found"}'},
        ],
        "proposed_match": {
            "bank_txn_id": "bt_9", "payment_ids": ["pay_ghost"],
            "fee_schedule": "v1", "extra_bank_txn_ids": [],
            "arithmetic": {"gross": "100.00", "mdr": "0.00", "gst": "0.00",
                           "rounding": "0.00", "net": "100.00"},
        },
    }
    with AuditLog(db_path) as log:
        log.start_run(run_id, 42, 47)
        log.record_event(run_id, "layer1", "batch", "layer1_started",
                         {"bank_txns": 47, "settlements": 46})
        log.record_event(run_id, "layer1", "bt_9", "exception",
                         {"reason": "amount_mismatch", "detail": "off by 12.00",
                          "exception_id": "EX-001"})
        log.record_event(run_id, "layer1", "batch", "layer1_finished",
                         {"matched": 39, "exceptions": 1, "match_rate": 0.83})
        log.record_event(run_id, "layer2", "EX-001", "tool_call",
                         {"tool": "get_payment", "args": {"payment_id": "pay_ghost"}})
        log.record_event(run_id, "layer2", "bt_9", "verdict_recorded",
                         {"exception_id": "EX-001", "verdict": verdict})
        log.record_event(run_id, "layer3", "bt_9", "verified",
                         {"exception_id": "EX-001", "passed": False,
                          "rejection_reason": "phantom_reference",
                          "effective_confidence": "unresolvable",
                          "schedule_anomaly": None})
        log.record_event(run_id, "layer3", "bt_9", "verification_recorded",
                         {"exception_id": "EX-001", "result": {
                             "passed": False, "rejection_reason": "phantom_reference",
                             "checks": [{"check": "existence", "passed": False,
                                         "detail": "cited record(s) do not exist: "
                                                   "['pay_ghost']"}]}})
        log.commit()
        log.finish_run(run_id)


def test_the_queue_shows_a_verifier_rejected_verdict_as_such(app, tmp_path) -> None:
    """12.7's specific requirement. This is the strongest row in the demo:
    the agent proposed a match, and something independent refused it."""
    seed_rejected_run(tmp_path / "test.db")
    at = app.run()
    at.session_state["run_id"] = "run_rejected"
    at.sidebar.radio[0].set_value("Escalation queue").run()

    assert not list(at.exception), list(at.exception)
    assert any("verifier rejected" in e.value for e in at.error)
    assert any("phantom_reference" in e.value for e in at.error)


def test_a_rejection_is_told_apart_from_a_non_answer(app, tmp_path) -> None:
    """The distinction the whole escalation screen exists to preserve. A
    rejected verdict sends a human to check cited records; an unanswered
    exception sends them somewhere else entirely."""
    seed_rejected_run(tmp_path / "test.db")
    at = app.run()
    at.session_state["run_id"] = "run_rejected"
    at.sidebar.radio[0].set_value("Escalation queue").run()

    hint = next(i.value for i in at.info if "What would unblock this" in i.value)
    assert "verifier rejected it" in hint
    assert "check the cited records" in hint


def test_a_rejected_block_is_marked_as_an_error_on_the_live_screen(
    app, tmp_path
) -> None:
    """The one case that *is* a failure state, as against E9 and E10."""
    seed_rejected_run(tmp_path / "test.db")
    at = app.run()
    at.session_state["run_id"] = "run_rejected"
    at.sidebar.radio[0].set_value("Live run").run()

    assert [block.state for block in at.get("status")] == ["error"]


def test_a_block_header_says_the_outcome_in_words(app) -> None:
    """`st.status` draws its own tick for a finished block, and on this
    screen ✓ is the verifier's vocabulary - so a completed block reads as
    a reconciled one. The words are what carry the meaning; an escalation
    must not be able to look like a success at a glance."""
    at = after_a_run(app)
    at.sidebar.radio[0].set_value("Live run").run()

    headers = [block.label for block in at.get("status")]
    assert sum("verified, needs sign-off" in h for h in headers) == 2
    assert sum(h.endswith("— escalated") for h in headers) == 4
    assert sum("already covered" in h for h in headers) == 1
    assert not any("✓" in h or "✗" in h for h in headers)
