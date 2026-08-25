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
