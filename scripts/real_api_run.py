"""One manual real-API run over the demo set (WORKFLOWS 13, Stages 6-7).

Runs the **whole pipeline** - Layer 1, the live investigator, the verifier -
and writes every model response into the committed cache at
``data/generated/demo/api_cache.json``. That file is what makes the demo
replay offline, and it is why this script drives ``pipeline.run()`` rather
than looping over exceptions itself: a cache key is a hash of the exact
conversation, so the run that fills the cache has to ask the same questions,
in the same order, that the offline run will later ask. A second code path
here would produce a cache that misses on every key and nobody would find
out until the demo.

Not part of the test suite. The suite stays free and offline; this is the
deliberate, occasional exception that answers the one question a mock
cannot: whether the model actually finds these explanations.

Usage::

    PYTHONPATH=. python scripts/real_api_run.py            # live, ~57 requests
    PYTHONPATH=. python scripts/real_api_run.py --offline  # replay the cache
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from closo.config import DEMO_DIR
from closo.dataset_io import load_batch, load_ground_truth
from closo.layer2_investigator import Investigator
from closo.llm_client import GeminiClient
from closo.metrics import score_demo
from closo.pipeline import run
from closo.response_cache import DEMO_CACHE_PATH, JSONResponseStore, CachedLLMClient
from closo.schemas import ExceptionItem, FinalStatus
from closo.tools import ToolBox

TRANSCRIPT = Path("data/generated/real_api_run.json")


class Announcing:
    """Wraps the investigator to narrate the queue as it is worked.

    ``pipeline.run()`` does not stream - Stage 8's UI is where that lands -
    and a four-minute live run with no output is indistinguishable from a
    hung one.
    """

    def __init__(self, inner: Investigator, truth: dict) -> None:
        self.inner = inner
        self.truth = truth
        self.client = inner.client  # the pipeline reads cost counters off this
        self.done = 0

    def investigate(self, item: ExceptionItem):
        self.done += 1
        ref = item.bank_txn_id or item.settlement_id
        print(f"[{self.done}] {item.exception_id} {self._class_of(item):4} {ref} "
              f"({item.reason}) ... ", end="", flush=True)
        outcome = self.inner.investigate(item)
        print(f"{outcome.verdict.confidence:12} "
              f"({outcome.tool_calls_used} tools, {outcome.tokens_used} tok)")
        return outcome

    def _class_of(self, item: ExceptionItem) -> str:
        entry = self.truth["bank_txns"].get(item.bank_txn_id or "", {})
        if entry:
            return entry.get("error_class", "?")
        return "E9" if item.settlement_id in self.truth["missing_settlements"] else "?"


def main(offline: bool = False) -> int:
    batch = load_batch(DEMO_DIR)
    truth = load_ground_truth(DEMO_DIR)  # reporting only, after the fact
    store = JSONResponseStore(DEMO_CACHE_PATH)

    if offline:
        client = CachedLLMClient(store)
        print(f"Offline: replaying {len(store)} cached responses\n")
    else:
        client = GeminiClient(store=store)
        print(f"Live on {client.model}; cache holds {len(store)} responses\n")

    agent = Announcing(Investigator(ToolBox(batch), client), truth)

    started = time.perf_counter()
    outcome = run(batch, investigator=agent)
    elapsed = time.perf_counter() - started
    card = score_demo(outcome, batch)

    print(f"\nmatch rate:        {card.match_rate:.1%} "
          f"({card.auto_matched} auto + {card.agent_verified} agent-verified)")
    print(f"verified accuracy: {card.verified_accuracy:.1%}")
    print(f"escalated:         {card.escalated} "
          f"({card.correct_escalations} correct, {card.false_escalations} false)")
    print(f"false resolutions: {card.false_resolutions}")
    print(f"awaiting sign-off: {card.awaiting_signoff}")
    print(f"requests spent:    {card.requests_made}")
    print(f"cache hits:        {card.cache_hits}")
    print(f"tokens:            {card.tokens_used:,}")
    print(f"elapsed:           {elapsed:.0f}s")
    print(f"cache now holds:   {len(JSONResponseStore(DEMO_CACHE_PATH))} responses")

    _write_transcript(outcome, card, truth, elapsed, getattr(client, "model", "cache"))
    print(f"transcript:        {TRANSCRIPT}")
    return 0 if card.false_resolutions == 0 else 1


def _write_transcript(outcome, card, truth, elapsed: float, model: str) -> None:
    """Every verdict and every verifier result, for inspection after the fact."""
    results = []
    for exception_id, verdict in outcome.verdicts.items():
        verification = outcome.verifications.get(exception_id)
        results.append({
            "exception_id": exception_id,
            "verdict": json.loads(verdict.model_dump_json()),
            "verifier": json.loads(verification.model_dump_json()) if verification else {},
        })

    TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT.write_text(
        json.dumps(
            {
                "model": model,
                "elapsed_seconds": round(elapsed, 1),
                "scorecard": card.stable_dict(),
                "cost": outcome.cost.as_dict(),
                "statuses": {k: v.value for k, v in sorted(outcome.statuses.items())},
                "escalated": {
                    txn: note for txn, note in sorted(outcome.notes.items())
                    if outcome.statuses[txn] is FinalStatus.ESCALATED
                },
                "truth_counts": truth["counts"],
                "results": results,
            },
            indent=2, sort_keys=True,
        ),
        encoding="utf-8", newline="\n",
    )


if __name__ == "__main__":
    sys.exit(main(offline="--offline" in sys.argv))
