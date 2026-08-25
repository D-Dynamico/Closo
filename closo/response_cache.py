"""A durable home for model responses, and the client that replays them.

Two pieces, both in service of one requirement: **a fresh clone must demo
offline** (14). Layer 2's answers cost real requests against a finite free
tier, so a run that has already paid for them should never pay again - and
a demo on a stage with no wifi should not depend on the API being up.

:class:`JSONResponseStore` is the committed form of the ``api_cache`` table
(9.1). Same shape, same keys; a file rather than a database because
``.gitignore`` excludes ``*.db``, and a cache that cannot be cloned does
not help the person cloning. :class:`closo.audit.AuditLog` satisfies the
same protocol for a live session's own database.

:class:`CachedLLMClient` serves those responses and nothing else. It has no
API key, no SDK and no way to acquire one, which is what makes an
airplane-mode claim checkable rather than promised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from closo.config import DEMO_DIR
from closo.errors import CacheMiss
from closo.llm_client import LLMResponse, cache_key, from_payload

#: The committed cache the demo replays from. Lives beside the frozen demo
#: dataset because it is the same kind of artifact: a recorded fact about
#: seed 42 that a fresh clone needs and cannot regenerate for free.
DEMO_CACHE_PATH = DEMO_DIR / "api_cache.json"


class JSONResponseStore:
    """A response cache in one JSON file.

    Written sorted and indented with LF endings, so two runs that cached
    the same responses produce the same bytes and the file diffs like
    source rather than like a blob (11.5).
    """

    def __init__(self, path: Path | str = DEMO_CACHE_PATH) -> None:
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        if self.path.exists():
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))

    # -- the ResponseStore protocol ---------------------------------------

    def cache_get(self, cache_key: str) -> dict | None:
        return self.entries.get(cache_key)

    def cache_put(self, cache_key: str, response: dict) -> None:
        """Store a response and flush immediately.

        Flushing on every put rather than at the end is deliberate: a live
        run that dies at exception seven has still bought six answers, and
        losing them means paying for them twice.
        """
        self.entries[cache_key] = response
        self.flush()

    # -- file -------------------------------------------------------------

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", newline="\n", encoding="utf-8") as handle:
            json.dump(self.entries, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def __len__(self) -> int:
        return len(self.entries)


def copy_into(source: JSONResponseStore, destination: Any) -> int:
    """Copy every cached response into another store. Returns the count.

    Lets a committed JSON cache seed a session's SQLite ``api_cache``
    table, which is where 9.1 says these belong.
    """
    for key, payload in source.entries.items():
        destination.cache_put(key, payload)
    return len(source.entries)


class CachedLLMClient:
    """Serves cached responses only. No key, no SDK, no network.

    A miss raises :class:`closo.errors.CacheMiss`, which the investigator
    treats like any other non-transient failure: that one exception ends
    ``unresolvable`` and the batch carries on. That is the honest offline
    outcome. The alternative - falling back to a live call - would mean a
    demo billed as airplane-mode quietly reaching for the network, which
    is precisely the failure mode this class exists to rule out.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.cache_hits = 0
        self.misses = 0
        #: Present so callers can report cost uniformly with the real
        #: client. A replay spends nothing, by construction.
        self.requests_made = 0

    def generate(
        self,
        system_prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        force_tool: str | None = None,
    ) -> LLMResponse:
        """Return the cached reply for this exact conversation.

        Raises:
            CacheMiss: if this conversation was never recorded.
        """
        key = cache_key(system_prompt, messages, force_tool)
        payload = self.store.cache_get(key)
        if payload is None:
            self.misses += 1
            raise CacheMiss(
                f"no cached response for this request ({key[:12]}); the run "
                "has diverged from what was recorded, and nothing offline "
                "can answer it"
            )
        self.cache_hits += 1
        return from_payload(payload)


def demo_client(path: Path | str = DEMO_CACHE_PATH) -> CachedLLMClient | None:
    """The offline investigator client, or None if nothing is cached.

    None rather than an empty client: a pipeline handed a client that can
    only miss would investigate ten exceptions to fail ten times and report
    ten spurious `unresolvable` verdicts. With no cache the honest thing is
    to run Layer 1 alone and say Layer 2 did not run.
    """
    store = JSONResponseStore(path)
    return CachedLLMClient(store) if len(store) else None
