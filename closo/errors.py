"""Exception types shared across layer boundaries.

These live apart from ``llm_client.py`` for one structural reason. The
pipeline has to *handle* a quota wall - it decides what happens to the
exceptions after it - but the pipeline must not import the LLM seam, and a
test asserts exactly that (11.3). An exception type is the one part of the
provider seam a caller legitimately needs without needing the provider.

Nothing here imports anything. That is the point.
"""

from __future__ import annotations


class QuotaExhausted(RuntimeError):
    """Raised when the run has spent its daily request budget.

    Caught by the investigator, which marks the remaining exceptions
    unresolvable, and by the pipeline, which records why the batch wound
    down. A quota wall must degrade a run honestly, never truncate it
    silently (7.4).
    """


class CacheMiss(LookupError):
    """No cached response exists for a request, and none can be fetched.

    Raised only by :class:`closo.llm_client.CachedLLMClient`, the offline
    replay client. Deliberately *not* a transient error: there is nothing
    to retry, so the investigation ends as ``unresolvable`` and the run
    continues. An airplane-mode demo that has drifted off its cached path
    should say so on the scorecard rather than crash or invent a verdict.
    """
