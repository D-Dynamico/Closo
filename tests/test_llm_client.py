"""Tests for the provider seam: budget guard, cache keys, and the mock.

The real Gemini call is not exercised here - it lands in Stage 6 and is
tested through the mock. What matters now is that the quota machinery
behaves at its boundaries, because the failure it guards against (a run
dying halfway and leaving a scorecard that looks complete) is silent.
"""

from __future__ import annotations

import sys

import pytest

from closo.llm_client import (
    GeminiClient,
    LLMResponse,
    MockLLMClient,
    QuotaExhausted,
    RequestBudget,
    ToolCall,
    cache_key,
)


# --------------------------------------------------------------------------
# Budget guard
# --------------------------------------------------------------------------


def test_budget_allows_exactly_its_limit() -> None:
    b = RequestBudget(rpd_limit=3, rpm_limit=1000)
    for _ in range(3):
        b.spend()
    assert b.requests_made == 3
    assert b.remaining == 0


def test_budget_raises_on_the_request_after_the_limit() -> None:
    """The boundary that decides whether a run ends honestly or crashes."""
    b = RequestBudget(rpd_limit=1, rpm_limit=1000)
    b.spend()
    with pytest.raises(QuotaExhausted, match="daily request budget"):
        b.spend()


def test_budget_remaining_never_goes_negative() -> None:
    b = RequestBudget(rpd_limit=1, rpm_limit=1000)
    b.spend()
    with pytest.raises(QuotaExhausted):
        b.spend()
    assert b.remaining == 0


def test_zero_budget_refuses_the_very_first_request() -> None:
    """Degenerate but reachable: a run started after the day's quota is
    already gone must fail immediately and clearly, not part-way through."""
    b = RequestBudget(rpd_limit=0, rpm_limit=1000)
    with pytest.raises(QuotaExhausted):
        b.spend()


def test_quota_exhausted_is_catchable_as_runtime_error() -> None:
    """The investigator catches this to wind the batch down, so it must not
    be something an over-broad handler elsewhere would swallow first."""
    assert issubclass(QuotaExhausted, RuntimeError)


def test_budget_defaults_match_the_measured_free_tier() -> None:
    """Section 7.4 records what AI Studio actually reported. A default that
    drifts above the real ceiling would turn a clean wind-down into a
    stream of 429s."""
    from closo.config import GEMINI_RPD_LIMIT, GEMINI_RPM_LIMIT

    b = RequestBudget()
    assert b.rpd_limit == GEMINI_RPD_LIMIT
    assert b.rpm_limit == GEMINI_RPM_LIMIT


def test_budget_is_thread_safe_under_contention() -> None:
    """Section 7.3 runs exceptions through a ThreadPoolExecutor outside demo
    mode. An unlocked counter would over-spend the daily quota."""
    from concurrent.futures import ThreadPoolExecutor

    b = RequestBudget(rpd_limit=50, rpm_limit=10_000)

    def try_spend() -> bool:
        try:
            b.spend()
            return True
        except QuotaExhausted:
            return False

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: try_spend(), range(80)))

    assert sum(results) == 50
    assert b.requests_made == 50


# --------------------------------------------------------------------------
# Cache keys
# --------------------------------------------------------------------------


def test_cache_key_is_stable_for_identical_requests() -> None:
    msgs = [{"role": "user", "content": "reconcile bt_001"}]
    assert cache_key("sys", msgs, None) == cache_key("sys", msgs, None)


def test_cache_key_changes_with_conversation_history() -> None:
    """Keys hash the whole conversation, not just the last turn. The same
    question asked after different evidence is a different question, and
    collapsing the two would serve a stale answer."""
    first = cache_key("sys", [{"role": "user", "content": "q"}], None)
    second = cache_key(
        "sys",
        [
            {"role": "user", "content": "q"},
            {"role": "tool", "content": "refund found"},
        ],
        None,
    )
    assert first != second


def test_cache_key_changes_with_system_prompt() -> None:
    msgs = [{"role": "user", "content": "q"}]
    assert cache_key("prompt A", msgs, None) != cache_key("prompt B", msgs, None)


def test_cache_key_changes_when_a_tool_is_forced() -> None:
    """Forcing submit_verdict produces a different answer than leaving the
    model free, so the two must not share a cache entry."""
    msgs = [{"role": "user", "content": "q"}]
    assert cache_key("sys", msgs, None) != cache_key("sys", msgs, "submit_verdict")


def test_cache_key_is_order_sensitive_across_messages() -> None:
    a = [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}]
    b = [{"role": "user", "content": "two"}, {"role": "user", "content": "one"}]
    assert cache_key("sys", a, None) != cache_key("sys", b, None)


def test_cache_key_survives_non_json_types() -> None:
    """Tool results carry Decimals and dates. The key builder must not blow
    up on them mid-run."""
    from datetime import date
    from decimal import Decimal

    msgs = [{"role": "tool", "content": {"amount": Decimal("10.00"), "on": date(2026, 3, 4)}}]
    assert isinstance(cache_key("sys", msgs, None), str)


# --------------------------------------------------------------------------
# Mock client
# --------------------------------------------------------------------------


def test_mock_returns_scripted_responses_in_order() -> None:
    mock = MockLLMClient(
        [
            LLMResponse(tool_calls=[ToolCall("get_refunds", {"payment_id": "pay_001"})]),
            LLMResponse(text="done", tokens_used=42),
        ]
    )
    first = mock.generate("sys", [], [], None)
    second = mock.generate("sys", [], [], "submit_verdict")
    assert first.tool_calls[0].name == "get_refunds"
    assert second.text == "done"


def test_mock_records_what_it_was_asked() -> None:
    """Section 12.5 asserts on the forced tool and the prompt, so the mock
    has to remember the request, not just answer it."""
    mock = MockLLMClient([LLMResponse(text="ok")])
    mock.generate("the system prompt", [{"role": "user", "content": "q"}], [], "submit_verdict")
    assert mock.calls[0]["force_tool"] == "submit_verdict"
    assert mock.calls[0]["system_prompt"] == "the system prompt"


def test_mock_running_dry_fails_loudly() -> None:
    """A mock that quietly returned an empty turn would let a test pass for
    entirely the wrong reason."""
    mock = MockLLMClient([LLMResponse(text="only one")])
    mock.generate("sys", [], [], None)
    with pytest.raises(AssertionError, match="ran out of scripted responses"):
        mock.generate("sys", [], [], None)


def test_mock_satisfies_the_protocol_shape() -> None:
    """Both clients must be interchangeable or the mocked suite proves
    nothing about the real one."""
    assert hasattr(MockLLMClient([]), "generate")
    assert hasattr(GeminiClient, "generate")


def test_empty_response_is_representable() -> None:
    """A turn with neither tool calls nor text is malformed output, which
    12.5 requires the investigator to retry once. The type must be able to
    represent it rather than making it unconstructable."""
    r = LLMResponse()
    assert r.text == "" and r.tool_calls == [] and not r.from_cache


# --------------------------------------------------------------------------
# Real client guardrails
# --------------------------------------------------------------------------


def test_gemini_client_without_a_key_explains_the_fix() -> None:
    """A missing key is the single most likely first-run failure, so the
    message names the file, the URL and the offline alternative."""
    with pytest.raises(RuntimeError) as exc:
        GeminiClient(api_key=None if not _key_present() else "")
    message = str(exc.value)
    assert "GEMINI_API_KEY" in message
    assert "DEMO_MODE" in message


def _key_present() -> bool:
    from closo.config import GEMINI_API_KEY

    return bool(GEMINI_API_KEY)


# --------------------------------------------------------------------------
# Import invariant (11.3)
# --------------------------------------------------------------------------


def _modules_after_importing(*targets: str) -> set[str]:
    """Modules present after importing ``targets`` in a clean interpreter.

    A subprocess rather than an in-process sys.modules read: within one
    pytest process an unrelated earlier test can import the SDK and make
    this assertion pass or fail for reasons that have nothing to do with
    the module under test. That order-dependence is exactly how an import
    invariant quietly stops being enforced.
    """
    import subprocess

    imports = "; ".join(f"import {t}" for t in targets)
    code = (
        f"import sys; {imports}; "
        "print(','.join(m for m in sys.modules "
        "if m.startswith('closo') or m.startswith('google.genai')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(_repo_root()),
    )
    return {m for m in out.stdout.strip().split(",") if m}


def _repo_root() -> object:
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


def test_importing_the_seam_does_not_import_the_sdk() -> None:
    """The SDK import is deferred into GeminiClient's constructor. Without
    that, the sys.modules checks Layer 1, the verifier and metrics rely on
    would be meaningless."""
    loaded = _modules_after_importing("closo.llm_client")
    assert "closo.llm_client" in loaded
    assert not any(m.startswith("google.genai") for m in loaded), (
        "importing closo.llm_client pulled in the Gemini SDK"
    )


def test_config_and_schemas_stay_llm_free() -> None:
    """These are imported by the LLM-free modules, so they must not become a
    back door to the SDK (11.3)."""
    loaded = _modules_after_importing("closo.config", "closo.schemas")
    assert "closo.llm_client" not in loaded
    assert not any(m.startswith("google.genai") for m in loaded)
