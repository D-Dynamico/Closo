"""Scripted UI smoke tests (TEST_PLAN 12.7).

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
    """
    monkeypatch.setenv("CLOSO_DB", str(tmp_path / "test.db"))
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
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()

    values = {m.label: m.value for m in at.metric}
    assert values["Match rate"] == "83.0%"
    assert values["Verified accuracy"] == "100.0%"
    assert values["₹ reconciled"].startswith("₹")
    assert values["Correctly escalated"] == "2"


def test_scorecard_renders_the_tier_bar_and_taxonomy(app) -> None:
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    assert len(at.get("plotly_chart")) == 1
    assert len(at.dataframe) == 1


def test_scorecard_declares_the_pending_layer(app) -> None:
    """The false-escalation count is shown in full; this only explains it."""
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    assert any("Layer 2" in w.value for w in at.warning)


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


def test_replay_is_labelled_as_a_replay(app) -> None:
    """A replayed scorecard must not be mistakable for a live one."""
    at = app.run()
    at.button[0].click().run()
    at.sidebar.radio[0].set_value("Ingest").run()
    at.button[1].click().run()
    at.sidebar.radio[0].set_value("Scorecard").run()
    assert any("Replayed" in c.value for c in at.caption)


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "screen", ["Live run", "Exception drill-down", "Escalation queue"]
)
def test_unbuilt_screens_name_their_stage(app, screen: str) -> None:
    at = app.run()
    at.sidebar.radio[0].set_value(screen).run()
    assert not list(at.exception)
    assert any("Stage" in i.value for i in at.info)
