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
from typing import Any, Protocol, Sequence

from closo.config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_RPD_LIMIT,
    GEMINI_RPM_LIMIT,
)


class QuotaExhausted(RuntimeError):
    """Raised when the run has spent its request budget.

    The investigator catches this and marks the remaining exceptions
    unresolvable rather than letting the batch die. A quota wall must
    degrade a run honestly, never truncate it silently (7.4).
    """


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
        self._window_start = time.monotonic()
        self._window_count = 0

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

            elapsed = time.monotonic() - self._window_start
            if elapsed >= 60.0:
                self._window_start = time.monotonic()
                self._window_count = 0
            elif self._window_count >= self.rpm_limit:
                wait = 60.0 - elapsed
                time.sleep(wait)
                self._window_start = time.monotonic()
                self._window_count = 0

            self.requests_made += 1
            self._window_count += 1


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
    """Real Gemini client, behind the budget guard and an in-memory cache.

    The SDK import is deferred to construction time on purpose. Importing
    this module must stay cheap and side-effect free so the no-LLM-import
    tests can inspect ``sys.modules`` meaningfully.
    """

    def __init__(
        self,
        model: str = GEMINI_MODEL,
        api_key: str | None = None,
        budget: RequestBudget | None = None,
    ) -> None:
        from google import genai  # deferred: see class docstring

        key = api_key or GEMINI_API_KEY
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add "
                "a key from https://aistudio.google.com/apikey, or run with "
                "DEMO_MODE=1 and replay a cached run."
            )
        self.model = model
        self.budget = budget or RequestBudget()
        self._client = genai.Client(api_key=key)
        self._cache: dict[str, LLMResponse] = {}

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
            return LLMResponse(
                text=cached.text,
                tool_calls=list(cached.tool_calls),
                tokens_used=0,  # a cache hit costs nothing; do not double-count
                from_cache=True,
            )

        self.budget.spend()
        raw = self._call_api(system_prompt, messages, tools, force_tool)
        self._cache[key] = raw
        return raw

    def _call_api(
        self,
        system_prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        force_tool: str | None,
    ) -> LLMResponse:
        """Issue the actual request. Wired in Stage 6."""
        raise NotImplementedError(
            "GeminiClient._call_api lands in Stage 6 with the investigator"
        )
