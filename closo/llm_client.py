"""The provider seam. The only module in Closo allowed to import the LLM SDK.

Everything Layer 2 knows about talking to a model is the :class:`LLMClient`
protocol below. That buys three things CLAUDE.md needs:

* **Free, offline tests.** :class:`MockLLMClient` satisfies the same protocol,
  so the 12.5 suite runs canned tool-call sequences with no network and no
  quota (12.5).
* **A contained provider swap.** This project moved from Anthropic to Gemini
  once already. The next move touches this file only.
* **An enforceable invariant.** Layer 1, the verifier and metrics must not
  import an LLM. The test asserts on ``google.genai`` and on this module, and
  that assertion only means something because the import lives in one place
  (11.3).

**Requests are the scarce resource, not tokens.** Free-tier quota is 15 RPM
and 500 RPD (7.4), against roughly a hundred requests per run. So the client
owns a budget guard and a response cache, and Layer 2 never counts for
itself.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from closo.config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_RPD_LIMIT,
    GEMINI_RPM_LIMIT,
)
from closo.errors import QuotaExhausted


#: ``QuotaExhausted`` is imported above rather than defined here, and
#: re-exported so ``from closo.llm_client import QuotaExhausted`` keeps
#: working. It lives in :mod:`closo.errors` because the pipeline has to
#: handle a quota wall - deciding what becomes of the exceptions after it -
#: without importing this module, which a test enforces (11.3).
__all__ = [
    "QuotaExhausted", "ToolCall", "LLMResponse", "LLMClient", "RequestBudget",
    "cache_key", "to_payload", "from_payload", "ResponseStore",
    "MockLLMClient", "GeminiClient",
]


@dataclass
class ToolCall:
    """A function call the model asked for."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """One model turn: either tool calls, or final text, or both.

    ``tool_calls`` being empty is how the investigator knows the model
    stopped asking for data. A turn with neither tool calls nor text is a
    malformed response and the caller retries once (12.5).
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_used: int = 0
    from_cache: bool = False


class LLMClient(Protocol):
    """What Layer 2 is allowed to assume about a model.

    Deliberately one method. Anything richer would leak provider concepts
    into the investigator and make the mock harder to keep honest.
    """

    def generate(
        self,
        system_prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        force_tool: str | None = None,
    ) -> LLMResponse:
        """Run one turn.

        Args:
            system_prompt: the investigator's standing instructions.
            messages: conversation so far, oldest first.
            tools: function declarations the model may call.
            force_tool: if set, the model must call this function and may
                not reply with prose. Used to force ``submit_verdict``.

        Returns:
            The model's turn.

        Raises:
            QuotaExhausted: if the request budget for this run is spent.
        """
        ...


# --------------------------------------------------------------------------
# Budget guard
# --------------------------------------------------------------------------


class RequestBudget:
    """Counts requests against the free-tier ceilings and paces them.

    Thread-safe because 7.3 runs exceptions through a ThreadPoolExecutor
    outside demo mode. The per-minute pacing is a sleep rather than an
    error: hitting RPM means waiting a moment, whereas hitting RPD means
    the day is over and the run must wind down honestly.
    """

    def __init__(
        self,
        rpd_limit: int = GEMINI_RPD_LIMIT,
        rpm_limit: int = GEMINI_RPM_LIMIT,
    ) -> None:
        self.rpd_limit = rpd_limit
        self.rpm_limit = rpm_limit
        self.requests_made = 0
        self._lock = threading.Lock()
        self._min_interval = 60.0 / max(rpm_limit, 1)
        self._last_request = 0.0

    @property
    def remaining(self) -> int:
        """Requests still available today."""
        return max(0, self.rpd_limit - self.requests_made)

    def spend(self) -> None:
        """Claim one request, waiting if the per-minute rate demands it.

        Raises:
            QuotaExhausted: if the daily budget is gone.
        """
        with self._lock:
            if self.requests_made >= self.rpd_limit:
                raise QuotaExhausted(
                    f"daily request budget of {self.rpd_limit} is spent"
                )

            # Even spacing rather than a fixed window. A fixed window permits
            # a burst of `rpm_limit` at the end of one minute and the same
            # again at the start of the next, which is twice the rate over
            # the boundary - the first live run earned a wall of 429s that
            # way and four exceptions failed on rate limiting rather than on
            # anything to do with reconciliation.
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)

            self._last_request = time.monotonic()
            self.requests_made += 1


# --------------------------------------------------------------------------
# Response cache
# --------------------------------------------------------------------------


def cache_key(
    system_prompt: str,
    messages: Sequence[dict[str, Any]],
    force_tool: str | None,
) -> str:
    """Stable key for one request.

    Hashes the full conversation, not just the last message, because the
    same question asked after different evidence is a different question.
    """
    payload = json.dumps(
        {"system": system_prompt, "messages": list(messages), "force": force_tool},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def to_payload(response: LLMResponse) -> dict[str, Any]:
    """Render a response as plain JSON-safe data for a store.

    Token count is kept because a cached run should be able to report what
    the original run actually cost, even though serving the reply again
    costs nothing (see ``from_payload``).
    """
    return {
        "text": response.text,
        "tool_calls": [{"name": c.name, "args": dict(c.args)} for c in response.tool_calls],
        "tokens_used": response.tokens_used,
    }


def from_payload(payload: dict[str, Any]) -> LLMResponse:
    """Rebuild a response from stored data.

    ``tokens_used`` comes back as zero and ``from_cache`` as True. A cache
    hit spent nothing, and counting the original request's tokens again
    would make cost-per-record on the scorecard a claim about a request
    that was never made. The stored figure is still there for anyone
    reporting on the run that paid for it.
    """
    return LLMResponse(
        text=str(payload.get("text", "")),
        tool_calls=[
            ToolCall(name=str(c["name"]), args=dict(c.get("args") or {}))
            for c in payload.get("tool_calls") or []
        ],
        tokens_used=0,
        from_cache=True,
    )


@runtime_checkable
class ResponseStore(Protocol):
    """Somewhere responses survive between processes.

    Deliberately the shape :class:`closo.audit.AuditLog` already has, so
    the ``api_cache`` table backs this with no adapter. A store is optional
    everywhere: without one the client still caches in memory, it just
    forgets at process exit.
    """

    def cache_get(self, cache_key: str) -> dict | None: ...

    def cache_put(self, cache_key: str, response: dict) -> None: ...


# --------------------------------------------------------------------------
# Mock client
# --------------------------------------------------------------------------


class MockLLMClient:
    """A scripted client for tests. Never touches the network.

    Hands back queued responses in order. Running out is a loud failure
    rather than a silent empty turn, because a test that quietly stops
    getting responses tends to pass for the wrong reason.
    """

    def __init__(self, responses: Sequence[LLMResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        system_prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        force_tool: str | None = None,
    ) -> LLMResponse:
        """Return the next scripted response, recording the request."""
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "messages": list(messages),
                "tools": list(tools),
                "force_tool": force_tool,
            }
        )
        if not self.responses:
            raise AssertionError(
                f"MockLLMClient ran out of scripted responses after "
                f"{len(self.calls)} call(s)"
            )
        return self.responses.pop(0)


# --------------------------------------------------------------------------
# Gemini client
# --------------------------------------------------------------------------


class GeminiClient:
    """Real Gemini client, behind the budget guard and two caches.

    An in-memory cache serves repeats within a process. An optional
    :class:`ResponseStore` - the SQLite ``api_cache`` table, or the
    committed JSON file beside the demo dataset - serves repeats across
    processes, which is what lets a demo replay offline from answers an
    earlier run already paid for (7.3). Both are consulted **before** the
    budget guard: replaying costs no request, and counting one would
    misreport the day's quota.

    The SDK import is deferred to construction time on purpose. Importing
    this module must stay cheap and side-effect free so the no-LLM-import
    tests can inspect ``sys.modules`` meaningfully.
    """

    def __init__(
        self,
        model: str = GEMINI_MODEL,
        api_key: str | None = None,
        budget: RequestBudget | None = None,
        store: ResponseStore | None = None,
    ) -> None:
        key = api_key or GEMINI_API_KEY
        if not key:
            # Checked before the import so a missing key does not pull the SDK
            # into sys.modules - the 11.3 invariant tests read that.
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add "
                "a key from https://aistudio.google.com/apikey, or run with "
                "DEMO_MODE=1 and replay a cached run."
            )

        from google import genai  # deferred: see class docstring

        self.model = model
        self.budget = budget or RequestBudget()
        self._client = genai.Client(api_key=key)
        self._cache: dict[str, LLMResponse] = {}
        #: Optional durable cache. Every reply is written here as it
        #: arrives, so the demo can be replayed offline from responses a
        #: live run already paid for (7.3).
        self.store = store
        self.cache_hits = 0

    def generate(
        self,
        system_prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        force_tool: str | None = None,
    ) -> LLMResponse:
        """Run one turn, serving from cache when the request repeats.

        Raises:
            QuotaExhausted: if the daily request budget is spent.
        """
        key = cache_key(system_prompt, messages, force_tool)
        if key in self._cache:
            cached = self._cache[key]
            self.cache_hits += 1
            return LLMResponse(
                text=cached.text,
                tool_calls=list(cached.tool_calls),
                tokens_used=0,  # a cache hit costs nothing; do not double-count
                from_cache=True,
            )

        if self.store is not None:
            stored = self.store.cache_get(key)
            if stored is not None:
                # A reply this key already paid for, from a previous
                # process. Checked before the budget is touched: serving it
                # costs no request, which is the entire point of the store.
                self.cache_hits += 1
                response = from_payload(stored)
                self._cache[key] = response
                return response

        self.budget.spend()
        raw = self._call_api(system_prompt, messages, tools, force_tool)
        self._cache[key] = raw
        if self.store is not None:
            self.store.cache_put(key, to_payload(raw))
        return raw

    def _call_api(
        self,
        system_prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        force_tool: str | None,
    ) -> LLMResponse:
        """Issue the request and normalize the reply.

        This is the only function in the codebase that knows what a Gemini
        response looks like. Everything above it sees :class:`LLMResponse`,
        which is what lets the mocked client stand in faithfully.
        """
        from google.genai import types  # deferred with the client itself

        config: dict[str, Any] = {
            "system_instruction": system_prompt,
            # Temperature 0: the same exception must investigate the same way
            # twice, or the determinism guarantee stops at Layer 1.
            "temperature": 0,
            "tools": [types.Tool(function_declarations=list(tools))],
        }
        if force_tool is not None:
            # Forces a submit_verdict call instead of prose (7.2). Without
            # it the model can end a turn with an apology, which parses as
            # nothing at all.
            config["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY", allowed_function_names=[force_tool]
                )
            )

        response = self._client.models.generate_content(
            model=self.model,
            contents=_to_contents(messages),
            config=types.GenerateContentConfig(**config),
        )
        return _from_response(response)


# --------------------------------------------------------------------------
# Gemini wire format
# --------------------------------------------------------------------------
#
# Both functions live at module level so they can be tested without an API
# key or a client. The translation is the part most likely to be quietly
# wrong, and it is untestable if it hides inside a method that needs
# credentials to reach.


def _to_contents(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the neutral message list into Gemini `contents`.

    Gemini has no "tool" role: a tool result is a `function_response` part
    carried on a `user` turn. Getting this wrong does not raise - the model
    simply never sees the tool output and starts guessing, which looks like
    a bad model rather than a bad adapter.
    """
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        if role == "tool":
            payload = message.get("content")
            try:
                parsed = json.loads(payload) if isinstance(payload, str) else payload
            except json.JSONDecodeError:
                parsed = {"result": payload}
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": message.get("name", "tool"),
                                "response": parsed
                                if isinstance(parsed, dict)
                                else {"result": parsed},
                            }
                        }
                    ],
                }
            )
        else:
            contents.append(
                {
                    "role": "model" if role == "assistant" else "user",
                    "parts": [{"text": str(message.get("content", ""))}],
                }
            )
    return contents


def _from_response(response: Any) -> LLMResponse:
    """Normalize a Gemini reply into :class:`LLMResponse`.

    Defensive throughout. A response can legitimately carry no candidates,
    no parts, or text with no function call - and a crash here would end an
    investigation over a shape the API is entitled to return.
    """
    text_parts: list[str] = []
    calls: list[ToolCall] = []

    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            function_call = getattr(part, "function_call", None)
            if function_call is not None and getattr(function_call, "name", None):
                calls.append(
                    ToolCall(
                        name=function_call.name,
                        args=dict(getattr(function_call, "args", None) or {}),
                    )
                )
            part_text = getattr(part, "text", None)
            if part_text:
                text_parts.append(part_text)

    usage = getattr(response, "usage_metadata", None)
    tokens = int(getattr(usage, "total_token_count", 0) or 0)

    return LLMResponse(
        text="\n".join(text_parts), tool_calls=calls, tokens_used=tokens
    )
