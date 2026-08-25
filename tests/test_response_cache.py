"""Tests for the durable response cache and the offline replay client.

The property under test is not "caching works". It is that **a cached run
cannot quietly become a live one**, and that a cache which cannot answer
says so instead of guessing. Both failures are silent by nature: the first
spends quota nobody budgeted for, the second would put a fabricated verdict
on a scorecard in a room with no wifi.
"""

from __future__ import annotations

import json

import pytest

from closo.errors import CacheMiss
from closo.llm_client import (
    LLMResponse,
    RequestBudget,
    ToolCall,
    cache_key,
    from_payload,
    to_payload,
)
from closo.response_cache import (
    CachedLLMClient,
    JSONResponseStore,
    copy_into,
    demo_client,
)

SYSTEM = "you are a reconciliation analyst"
MESSAGES = [{"role": "user", "content": "exception EX-001"}]
TOOLS: list[dict] = []


def a_response() -> LLMResponse:
    return LLMResponse(
        text="checking the settlement",
        tool_calls=[
            ToolCall(name="get_payment", args={"payment_id": "pay_1"}),
            ToolCall(name="submit_verdict", args={"confidence": "resolved"}),
        ],
        tokens_used=1234,
    )


# --------------------------------------------------------------------------
# Payload round trip
# --------------------------------------------------------------------------


def test_round_trip_preserves_text_and_tool_calls() -> None:
    restored = from_payload(to_payload(a_response()))
    assert restored.text == "checking the settlement"
    assert [c.name for c in restored.tool_calls] == ["get_payment", "submit_verdict"]
    assert restored.tool_calls[0].args == {"payment_id": "pay_1"}


def test_a_cache_hit_reports_zero_tokens() -> None:
    """Replaying a reply spends nothing. Counting the original request's
    tokens again would make cost-per-record a claim about a request that
    was never made."""
    restored = from_payload(to_payload(a_response()))
    assert restored.tokens_used == 0
    assert restored.from_cache is True


def test_the_original_cost_is_still_recorded_in_the_payload() -> None:
    """Zero on the way out, but not forgotten - the run that paid for it
    must still be able to report what it spent."""
    assert to_payload(a_response())["tokens_used"] == 1234


def test_payload_is_json_serializable() -> None:
    """It is written to a file and to SQLite; a payload that needs a custom
    encoder would fail at the store rather than here."""
    json.dumps(to_payload(a_response()))


def test_an_empty_response_round_trips() -> None:
    restored = from_payload(to_payload(LLMResponse()))
    assert restored.text == "" and restored.tool_calls == []


def test_a_payload_missing_optional_keys_still_loads() -> None:
    """Hand-edited or older cache files must not crash a demo."""
    restored = from_payload({"text": "hello"})
    assert restored.tool_calls == []


# --------------------------------------------------------------------------
# JSON store
# --------------------------------------------------------------------------


def test_a_stored_response_survives_a_new_process(tmp_path) -> None:
    """The whole point: a fresh clone, or a fresh Streamlit boot, reads
    answers a previous run already paid for."""
    path = tmp_path / "api_cache.json"
    JSONResponseStore(path).cache_put("k1", to_payload(a_response()))

    reopened = JSONResponseStore(path)
    assert reopened.cache_get("k1") is not None
    assert len(reopened) == 1


def test_a_missing_file_is_an_empty_store_not_an_error(tmp_path) -> None:
    store = JSONResponseStore(tmp_path / "nothing.json")
    assert len(store) == 0
    assert store.cache_get("k1") is None


def test_each_put_is_flushed_immediately(tmp_path) -> None:
    """A live run that dies at exception seven has still bought six
    answers. Buffering until the end means paying for them twice."""
    path = tmp_path / "api_cache.json"
    store = JSONResponseStore(path)
    store.cache_put("k1", {"text": "one"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"k1": {"text": "one"}}


def test_the_file_is_written_sorted_with_lf_endings(tmp_path) -> None:
    """Same reasoning as the frozen dataset (11.5): the cache is committed,
    so it has to diff like source and hash the same on every platform."""
    path = tmp_path / "api_cache.json"
    store = JSONResponseStore(path)
    store.cache_put("zzz", {"text": "last"})
    store.cache_put("aaa", {"text": "first"})

    raw = path.read_bytes().decode("utf-8")
    assert "\r\n" not in raw
    assert raw.index('"aaa"') < raw.index('"zzz"')


def test_rewriting_a_key_replaces_it(tmp_path) -> None:
    store = JSONResponseStore(tmp_path / "c.json")
    store.cache_put("k", {"text": "old"})
    store.cache_put("k", {"text": "new"})
    assert store.cache_get("k") == {"text": "new"}
    assert len(store) == 1


def test_copy_into_seeds_another_store(tmp_path) -> None:
    """The committed JSON is the portable form of the api_cache table
    (9.1); a session can pour it into its own database."""
    from closo.audit import AuditLog

    source = JSONResponseStore(tmp_path / "c.json")
    source.cache_put("k1", to_payload(a_response()))

    with AuditLog(":memory:") as log:
        assert copy_into(source, log) == 1
        assert log.cache_get("k1") == to_payload(a_response())


def test_the_audit_log_satisfies_the_store_protocol() -> None:
    """Structural, and load-bearing: 9.1 puts cached responses in SQLite,
    and an adapter between the two would be a place for them to disagree."""
    from closo.audit import AuditLog
    from closo.llm_client import ResponseStore

    with AuditLog(":memory:") as log:
        assert isinstance(log, ResponseStore)


# --------------------------------------------------------------------------
# The offline client
# --------------------------------------------------------------------------


def test_a_cached_conversation_is_replayed(tmp_path) -> None:
    store = JSONResponseStore(tmp_path / "c.json")
    store.cache_put(cache_key(SYSTEM, MESSAGES, None), to_payload(a_response()))

    client = CachedLLMClient(store)
    response = client.generate(SYSTEM, MESSAGES, TOOLS)
    assert [c.name for c in response.tool_calls] == ["get_payment", "submit_verdict"]
    assert client.cache_hits == 1


def test_an_uncached_conversation_raises_rather_than_inventing_one(tmp_path) -> None:
    """The failure that matters. A client that answered anyway - or fell
    back to the network - would turn an airplane-mode demo into a live
    one, or a miss into a fabricated verdict."""
    client = CachedLLMClient(JSONResponseStore(tmp_path / "c.json"))
    with pytest.raises(CacheMiss):
        client.generate(SYSTEM, MESSAGES, TOOLS)
    assert client.misses == 1


def test_a_forced_verdict_is_a_different_cache_entry(tmp_path) -> None:
    """force_tool changes what the model is allowed to do, so it must
    change the key - otherwise a forced turn replays a free one."""
    store = JSONResponseStore(tmp_path / "c.json")
    store.cache_put(cache_key(SYSTEM, MESSAGES, None), to_payload(a_response()))

    client = CachedLLMClient(store)
    with pytest.raises(CacheMiss):
        client.generate(SYSTEM, MESSAGES, TOOLS, force_tool="submit_verdict")


def test_a_changed_conversation_is_a_miss_not_a_stale_hit(tmp_path) -> None:
    """The same question after different evidence is a different question.
    A key on the last message alone would serve the wrong answer."""
    store = JSONResponseStore(tmp_path / "c.json")
    store.cache_put(cache_key(SYSTEM, MESSAGES, None), to_payload(a_response()))

    client = CachedLLMClient(store)
    longer = [*MESSAGES, {"role": "tool", "name": "get_payment", "content": "{}"}]
    with pytest.raises(CacheMiss):
        client.generate(SYSTEM, longer, TOOLS)


def test_a_cache_miss_is_not_treated_as_transient() -> None:
    """The investigator retries transient API errors. There is nothing to
    retry here, and a retry would spend a second request in a mode that is
    supposed to spend none."""
    from closo.layer2_investigator import _is_transient

    assert _is_transient(CacheMiss("no cached response for this request (abc123)")) is False


def test_the_offline_client_holds_nothing_that_could_reach_a_network(tmp_path) -> None:
    """The airplane-mode claim is checkable, not promised: this client has
    no key, no SDK handle, and no code path to acquire either."""
    client = CachedLLMClient(JSONResponseStore(tmp_path / "c.json"))
    assert not hasattr(client, "_client")
    assert client.requests_made == 0

    import inspect

    source = inspect.getsource(type(client))
    assert "genai" not in source and "api_key" not in source


def test_demo_client_is_none_when_nothing_is_cached(tmp_path) -> None:
    """A client that can only miss would investigate every exception in
    order to fail on every one, and report ten `unresolvable` verdicts that
    say nothing about the data. Running Layer 1 alone is the honest
    alternative."""
    assert demo_client(tmp_path / "absent.json") is None


def test_demo_client_appears_once_something_is_cached(tmp_path) -> None:
    path = tmp_path / "c.json"
    JSONResponseStore(path).cache_put("k", {"text": "x"})
    assert demo_client(path) is not None


# --------------------------------------------------------------------------
# The live client's use of a store
# --------------------------------------------------------------------------


def _client_without_an_sdk(store) -> object:
    """A GeminiClient with its constructor skipped.

    Constructing the real one needs an API key and imports the SDK, which
    would put ``google.genai`` in ``sys.modules`` for every later test in
    this process. The caching path being tested here runs entirely above
    the SDK, so it is exercised directly rather than through credentials.
    """
    from closo.llm_client import GeminiClient

    client = GeminiClient.__new__(GeminiClient)
    client.model = "test-model"
    client.budget = RequestBudget(rpd_limit=5, rpm_limit=6000)
    client._cache = {}
    client.store = store
    client.cache_hits = 0
    return client


def test_a_stored_response_is_served_without_spending_a_request(tmp_path) -> None:
    """The reason the store exists. If the budget were checked first, an
    offline replay would still count against a daily quota it never used."""
    store = JSONResponseStore(tmp_path / "c.json")
    store.cache_put(cache_key(SYSTEM, MESSAGES, None), to_payload(a_response()))

    client = _client_without_an_sdk(store)
    client._call_api = lambda *a, **k: pytest.fail("the API was called for a cached key")

    response = client.generate(SYSTEM, MESSAGES, TOOLS)
    assert response.from_cache is True
    assert client.budget.requests_made == 0
    assert client.cache_hits == 1


def test_a_fresh_response_is_written_to_the_store(tmp_path) -> None:
    path = tmp_path / "c.json"
    client = _client_without_an_sdk(JSONResponseStore(path))
    client._call_api = lambda *a, **k: a_response()

    client.generate(SYSTEM, MESSAGES, TOOLS)

    assert JSONResponseStore(path).cache_get(cache_key(SYSTEM, MESSAGES, None))
    assert client.budget.requests_made == 1


def test_the_client_works_with_no_store_at_all(tmp_path) -> None:
    """A store is optional everywhere. Without one the client still caches
    in memory; it just forgets at process exit."""
    client = _client_without_an_sdk(None)
    calls: list[int] = []
    client._call_api = lambda *a, **k: (calls.append(1), a_response())[1]

    client.generate(SYSTEM, MESSAGES, TOOLS)
    client.generate(SYSTEM, MESSAGES, TOOLS)
    assert len(calls) == 1, "the in-memory cache must still work without a store"
