# 2026-08-25 — Stage 7: full pipeline hardening

Wiring Layers 2 and 3 into `pipeline.run()`, cost metrics, and a durable response cache
so the demo replays offline. Starts from `7d1c6f2` with 347 tests passing.

---

## Substep 1 — durable response cache and the offline client — 2026-08-25

**Done:** `closo/errors.py` (shared exception types), `closo/response_cache.py`
(`JSONResponseStore`, `CachedLLMClient`, `copy_into`, `demo_client`), and a `ResponseStore`
seam on `GeminiClient` so every live reply is written somewhere it survives the process.
27 new tests; full suite **371 passing**, still offline.

**Decisions:**

- **`QuotaExhausted` moved to a new `closo/errors.py`,** re-exported from `llm_client` so
  existing imports keep working. The pipeline has to *handle* a quota wall — it decides what
  becomes of the exceptions after it — but a test asserts `closo.pipeline` never imports
  `closo.llm_client` (11.3). An exception type is the one part of the provider seam a caller
  legitimately needs without needing the provider. `CacheMiss` lives there for the same
  reason.

- **The committed cache is a JSON file, not the SQLite table.** §9.1 puts cached responses in
  `api_cache`, and `AuditLog` does satisfy the store protocol — but `.gitignore` excludes
  `*.db`, and a cache that cannot be cloned does not help the person cloning. The frozen
  demo dataset already sets the precedent: a recorded fact about seed 42 that a fresh clone
  needs and cannot regenerate for free. `copy_into()` pours the file into the table for a
  session that wants it there.

- **The store is consulted before the budget guard, not after.** Replaying a response spends
  no request; charging one against a 500 RPD ceiling would misreport the day's remaining
  quota, and an offline replay would consume quota it never used.

- **A cache miss raises rather than falling back to the network.** `CachedLLMClient` has no
  key, no SDK handle and no code path to acquire either, and a test reads its source to
  prove it. That turns "works in airplane mode" from a promise into something checkable. A
  miss ends *that one* exception as `unresolvable` and the batch continues, which is the
  honest offline outcome — a demo that has drifted off its recorded path should say so on the
  scorecard rather than crash or invent a verdict.

- **`CacheMiss` is deliberately not transient.** The investigator retries transient API
  errors; there is nothing to retry here, and a retry would spend a second request in a mode
  meant to spend none. A test pins this against `_is_transient` directly.

- **`demo_client()` returns `None` when the cache is empty** rather than a client that can
  only miss. The latter would investigate ten exceptions in order to fail on all ten and
  report ten `unresolvable` verdicts that say nothing about the data.

- **Every put is flushed immediately.** A live run that dies at exception seven has still
  bought six answers; buffering until the end means paying for them twice.

**Surprises:**

- **The full suite failed on its very first run of the session** — both subprocess-based
  no-LLM-import tests, in `test_layer1.py` and `test_pipeline_e2e.py`, with a
  `CalledProcessError` from the spawn itself. Green on every run since (six so far, including
  the same command). Recorded rather than dismissed: those two tests are the enforcement
  mechanism for 11.3, and a spawn that intermittently fails could equally mask a real
  regression. Watch it; if it recurs, the fix is to have them report the subprocess's stderr
  rather than let `check=True` swallow it.

**Verified:** 371 passing. Mutation-tested four ways, all caught — replaying the original
token count on a cache hit, checking the store after spending a request instead of before,
returning an empty response on a miss instead of raising, and buffering writes instead of
flushing each put.

**Open:** the pipeline itself is still Layer 1 only; nothing consumes the cache yet.

---

## Substep 2 — Layers 2 and 3 wired into `pipeline.run()` — 2026-08-25

**Done:** `pipeline.run()` takes an optional investigator and, when given one, investigates
every exception and verifies every verdict before anything reaches a terminal state.
`metrics.py` now grades agent resolutions against ground truth the same way it grades
Layer 1's. `audit.finish_run()` stores a run summary so a replay can report what the
original cost. 30 new tests; full suite **401 passing**, still offline.

**On seed 42 with a scripted-but-honest model:** 47 credits, **95.7% match rate** (39
auto + 6 agent-verified), **100% verified accuracy**, zero false resolutions, **zero false
escalations** — the only two escalations left are the E10 credits, which is the designed
answer. ₹4,043,573.07 reconciled / ₹72,031.50 stuck. Two of the six agent resolutions are
E4 and carry a sign-off flag.

**Decisions:**

- **The investigator is injected, never constructed.** `pipeline.py` must not import
  `closo.llm_client` (11.3, and a test in `test_pipeline_e2e.py` enforces it). Injection
  keeps that true and lets the caller decide what a model is: live for the script, cached
  for the app, scripted for the tests. The pipeline's `Investigation` protocol is one
  method wide.

- **A `probable` verdict that passes verification is `AGENT_RESOLVED_VERIFIED` with a
  sign-off flag, not a fourth state.** §8 says "PASS → AGENT_RESOLVED_VERIFIED", and the
  Stage 1 decision already ruled that sign-off is a boolean on `Resolution`. The honesty
  §8 also asks for is carried by reporting it: `awaiting_signoff` and
  `money_awaiting_signoff` sit *inside* the resolved figures and are shown separately, so
  nobody reads "resolved" as "settled, nothing more to do". This is the E4 case, and it is
  the one the demo should drill into.

- **Investigation and verification are interleaved, not run as two phases.** Exclusivity
  has to be checked against resolutions that already passed, and a verdict covering a
  second credit has to remove it from the queue before it is investigated again.

- **The verifier starts with every payment Layer 1 already consumed marked as spent.**
  Without it, a verdict could cite payments belonging to an auto-matched settlement and the
  money view would count them twice — with every individual check passing.

- **The pipeline refuses a verified verdict that claims a credit already resolved, or that
  resolves a credit other than the one the exception was about.** Neither is something the
  verifier can see: it proves the arithmetic reproduces the credit, not that this was the
  credit anyone asked about, and it does not know what Layer 1 matched.

- **A rejection reads differently from a failure to answer.** "agent proposed, verifier
  rejected: `phantom_reference`" and "agent could not resolve: …" are different facts, and
  §10.5 puts the first on screen as the strongest thing in the demo.

- **Cost is stored in `runs.config_json` rather than a new column,** so the schema does not
  change under a database that already exists. A replay that could not say what the
  original spent would show a free reconciliation that was never free.

- **`tests/oracle_client.py` solves each exception from the records rather than reading
  ground truth.** Had it read the answers, "every agent resolution matches ground truth"
  would be true by construction and would prove nothing about the pipeline under it.

**Surprises:**

- **Every auto-matched credit was carrying a note reading "unhandled by Layer 1".** The
  fall-through sweep used `notes.setdefault` over *all* bank credits rather than only the
  ones that actually fell through, so 39 of 47 rows held a statement that was false about
  them. Nothing read a note without checking the status beside it, so nothing broke — it
  was waiting for the first screen that did, which is Stage 8's drill-down. Found while
  reading a smoke-test dump, not by a failing test.

- **The two double-count guards each hid the other on the demo dataset.** Disabling either
  one left the whole suite green, because on seed 42 the surviving guard caught the same
  cases — the identical shape of failure Stage 5 found in the verifier's two most important
  tests. Fixed with a four-record crafted batch that separates them: one settlement Layer 1
  matches, one with no credit at all whose net equals the matched credit, and one foreign
  credit. The second test asserts on the *rejection reason*, not merely that the verdict was
  refused, because without the seeding the verdict still fails — on arithmetic — and a test
  checking only "escalated" would not notice the guard had gone.

- **`pipeline.py` is 311 code lines** against §11.9's ~300. Also worth recording: the
  module sizes flagged in the last handoff were raw line counts including docstrings. By
  code lines the real over-runs are `generator.py` (392) and `layer2_investigator.py` (340);
  `llm_client.py` is 252 and was never over.

**Verified:** 401 passing. Mutation-tested seven ways, all caught and each by the test
written for it — dropping the credit-claim check, dropping the consumed-payment seeding,
never skipping a covered credit, dropping agent resolutions from the metrics grading,
never recording the sign-off flag, and forgetting agent resolutions on replay.
