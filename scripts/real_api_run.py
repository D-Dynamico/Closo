"""One manual real-API run over the demo set (WORKFLOWS 13, Stage 6 exit).

Runs Layer 1, hands every exception to the live investigator, and verifies
each verdict. Writes the transcript to disk so the outcome can be inspected
- and re-read - without spending quota twice.

Not part of the test suite. The suite must stay free and offline; this is
the deliberate, occasional exception that answers the one question a mock
cannot: whether the model actually finds these explanations.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from closo.config import DEMO_DIR
from closo.dataset_io import load_batch, load_ground_truth
from closo.layer1_matcher import run_layer1
from closo.layer2_investigator import Investigator
from closo.layer3_verifier import Verifier
from closo.llm_client import GeminiClient, QuotaExhausted
from closo.tools import ToolBox

OUT = Path("data/generated/real_api_run.json")


def main() -> int:
    batch = load_batch(DEMO_DIR)
    truth = load_ground_truth(DEMO_DIR)  # reporting only, after the fact
    layer1 = run_layer1(batch)

    client = GeminiClient()
    agent = Investigator(ToolBox(batch), client)
    verifier = Verifier(batch)

    exceptions = layer1.exceptions
    print(f"Layer 1: {len(layer1.matches)} matched, {len(exceptions)} exceptions\n")

    records = []
    started = time.perf_counter()

    for index, item in enumerate(exceptions, 1):
        ref = item.bank_txn_id or item.settlement_id
        cls = truth["bank_txns"].get(item.bank_txn_id or "", {}).get("error_class")
        if cls is None and item.settlement_id in truth["missing_settlements"]:
            cls = "E9"
        print(f"[{index}/{len(exceptions)}] {item.exception_id} {cls or '?':4} {ref} "
              f"({item.reason}) ... ", end="", flush=True)

        try:
            outcome = agent.investigate(item)
        except QuotaExhausted as wall:
            print(f"QUOTA EXHAUSTED: {wall}")
            break

        result = verifier.verify(outcome.verdict)
        verifier.commit(result)

        print(
            f"{outcome.verdict.confidence:12} "
            f"verified={'PASS' if result.passed else 'FAIL'} "
            f"({outcome.tool_calls_used} tools, {outcome.tokens_used} tok)"
        )
        if result.rejection_reason:
            print(f"        rejected: {result.rejection_reason}")
        if result.schedule_anomaly:
            print(f"        anomaly:  {result.schedule_anomaly}")

        records.append({
            "exception_id": item.exception_id,
            "error_class": cls,
            "reason": item.reason,
            "verdict": json.loads(outcome.verdict.model_dump_json()),
            "verifier": json.loads(result.model_dump_json()),
            "tool_calls_used": outcome.tool_calls_used,
            "tokens_used": outcome.tokens_used,
            "events": outcome.events,
        })

    elapsed = time.perf_counter() - started
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "model": client.model,
                "requests_spent": client.budget.requests_made,
                "elapsed_seconds": round(elapsed, 1),
                "results": records,
            },
            indent=2, sort_keys=True,
        ),
        encoding="utf-8", newline="\n",
    )

    print(f"\nrequests spent: {client.budget.requests_made} of {client.budget.rpd_limit}")
    print(f"tokens: {sum(r['tokens_used'] for r in records):,}")
    print(f"elapsed: {elapsed:.0f}s")
    print(f"transcript: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
