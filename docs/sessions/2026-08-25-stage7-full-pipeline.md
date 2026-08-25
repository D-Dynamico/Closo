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

---

## Substep 3 — cost metrics — 2026-08-25

**Done:** the Scorecard now reports what a run spent — tokens, requests, cache hits,
exceptions investigated and skipped — plus tokens and rupees per record and Layer 1's
throughput on its own. 8 new tests; full suite **409 passing**.

**Decisions:**

- **The default price is zero rupees per million tokens, and that is the honest figure.**
  This runs on the free tier: the run costs nothing. Inventing a plausible per-token price
  for a judged scorecard would be a fabricated number, not a conservative one. The rate is
  `INR_PER_MILLION_TOKENS` in `.env` for anyone who wants to see the same batch priced at a
  published paid rate, and a test sets it to prove the line computes rather than only ever
  printing zero.

- **Requests are reported next to tokens, and are the figure to read** (7.4). A cost line
  showing only tokens would be measuring the one resource that never runs out on this quota.
  A cached or replayed run shows zero requests, which is the true number: it spent none.

- **Layer 1's throughput is reported separately** (9.2), because averaging it with Layer 2
  hides both halves of the story — most of the batch clears in milliseconds, and the
  remainder costs seconds per record because it is talking to a model.

- **Cost is deliberately outside `stable_dict()`**, and the docstring now says why: a live
  run and a replay of that same run produce byte-identical reconciliation figures while
  spending different numbers of requests. What a run cost is a fact about the run, not about
  the batch, so it is asserted directly — including across a replay — rather than diffed.

---

## Substep 4 — the live run, cached for offline replay — 2026-08-25

**Done:** `scripts/real_api_run.py` rewritten to drive `pipeline.run()` and write every
response into `data/generated/demo/api_cache.json` (48 entries, 25 KB, committed). One live
run on `gemini-3.5-flash-lite`, then the same run offline from the cache. 7 new tests; full
suite **416 passing**.

**Live run — every Stage 7 exit criterion met:**

| | |
|---|---|
| Match rate | **95.7%** — 39 auto-matched + 6 agent-verified |
| Verified accuracy | **100%** |
| False resolutions | **0** |
| Escalated | 2, both E10 — **zero false escalations** |
| Requests | 48 (9 exceptions investigated, 1 skipped as already covered) |
| Tokens / elapsed | 159,239 / 190s |
| ₹ reconciled / stuck | 4,043,573.07 / 72,031.50 |

Per class: E6 ×2 resolved and verified; E5 resolved in **one** verdict citing both legs;
E4 ×2 resolved, verified, and capped to `probable` with the anomaly named — *"cited v1 but
v2 was active on 2026-03-18"* and *"…on 2026-03-25"*. E9 ×2 and E10 ×2 all `unresolvable`.
Summary in [`docs/real_api_run_2026-08-25-stage7.json`](../real_api_run_2026-08-25-stage7.json).

**The offline replay is identical** — same scorecard, same statuses, same verdicts, same
verifier results, 0 requests and 48 cache hits. The only difference anywhere is token
counts, which are zero on a cache hit by design: replaying a response spends nothing, and
reporting the original run's tokens again would be a claim about a request never made.

**Decisions:**

- **The script drives `pipeline.run()` instead of looping over exceptions itself.** A cache
  key is a hash of the exact conversation, so the run that fills the cache has to ask the
  same questions, in the same order, that the offline run will later ask. A second code path
  would have produced a cache that misses on every key — and nobody would have found out
  until the demo, because a miss degrades quietly to `unresolvable` rather than failing.

- **The airplane-mode claim is now a test, not a procedure.** `test_pipeline_e2e.py` runs
  the real Layer 2 against the recorded responses with no key and no SDK, asserts zero
  requests, and asserts **zero cache misses** — that last one is what catches the cache
  drifting away from a changed prompt or a reordered brief, which is otherwise silent. A
  subprocess check confirms the whole offline path never imports `google.genai`.

- **The tests fail rather than skip when the cache is missing.** An untested airplane-mode
  claim is the one that gets discovered on stage.

**Surprises:**

- **The model tried to resolve an E10 and was stopped by the shape of its own verdict.** On
  `bt_0393` it submitted `resolved` with an incomplete proposed match; the investigator
  downgraded it to `unresolvable` before the verifier ever saw it. The escalation note says
  so exactly — *"verdict rejected: 'resolved' but the proposed match was incomplete"*. The
  designed-unresolvable guard held, but this is the first time a live model has actually
  pushed on it.

- **The SDK emitted `UserWarning: MALFORMED_RESPONSE is not a valid FinishReason`** on the
  first exception, from `google/genai/_common.py`. The turn parsed fine and the exception
  resolved, so `_from_response`'s defensive reading did its job — but it means the API
  returns finish reasons this SDK version does not know about, and a stricter parser would
  have died there.

- **This run resolved 5 of 9 exceptions; the two Stage 6 runs resolved 3 each, and not the
  same three.** Same model, same temperature 0, same prompt. The variance is real and is
  exactly why the cache exists: the demo now replays a specific good run rather than hoping
  for one. Worth saying plainly in the pitch rather than claiming determinism through
  Layer 2.

---

## Substep 5 — the Scorecard shows all three layers — 2026-08-25

**Done:** pressing **Run reconciliation** now runs all three layers, with Layer 2 replaying
the committed responses. The Scorecard gained the sign-off explanation and a cost line; the
tier bar finally has an amber segment. 5 new UI tests; full suite **420 passing**.

Driven in a real browser as well as through `AppTest`: *"Reconciled 47 bank credits in
171 ms — 39 auto-matched, 6 agent-resolved and verified, 2 escalated"*, then a Scorecard
reading 95.7% / 100% / ₹4,043,573.07 / ₹72,031.50, all three colours on the bar, E4/E5/E6
sitting in the *Agent + verified* column of the taxonomy, and *"Cost · 0 API request(s) and
48 cache hit(s) for 9 investigation(s), 1 skipped as already covered · 0 tokens (0/record) ·
₹0.00 total, ₹0.00/record (free tier)"*.

**Decisions:**

- **The app builds a `CachedLLMClient`, never a `GeminiClient`.** The button cannot reach a
  network whatever the room's wifi is doing, because the object behind it has no key and no
  SDK handle. With an empty cache `build_investigator` returns `None` and the run is Layer 1
  only, labelled as such on Ingest — honest, where an investigator that could only miss
  would fill the queue with `unresolvable` verdicts that say nothing about the data.

- **The cost line leads with requests, and prints zero rather than hiding.** A replayed run
  spending nothing is a fact about that run worth stating. It is also the only thing on
  screen that would reveal a "cached" Layer 2 quietly calling the API — the scorecard itself
  would look identical — so a UI test asserts the line starts `Cost · 0 API request(s)`.

**Surprises:**

- **The replay message named only two of the three terminal states.** It read "39 matched,
  2 escalated" and silently dropped Layer 2's six resolutions — from the one sentence a
  presenter reads aloud, about the layer the whole pitch is built on. The run message had
  been updated and the replay message had not. Found by clicking the button in a browser;
  every test still passed, because the tests compared the *metrics* after a replay and
  nothing asserted on that sentence. There is one now.

- **A UI test that only checks the tier bar renders would not notice a missing colour.** The
  new test reads the plotly spec and asserts all three colours are present *and that every
  tier carries a non-zero count* — before this stage amber was legend-only, and §12.7 asks
  for three colours on screen rather than three entries in a legend.

---

## Substep 6 — docs, and a hole in the section guard — 2026-08-25

**Done:** `CLAUDE.md` (status, commands, the cache convention, corrected module sizes),
`README.md`, `docs/ARCHITECTURE.md` (module map, `api_cache`, §8 sign-off and the two
pipeline-level checks, §7.3 cache-before-budget), `docs/WORKFLOWS.md` (§10.1 demo mode,
Stage 7 marked done with what it met). 2 new tests; full suite **422 passing**.

**Surprises:**

- **Mutating the docs found the section-reference guard had a blind spot** — the same habit
  that caught the toothless version of this test two sessions ago. Renaming
  `### 8.1 The fee-schedule anomaly` to `### 8.4` passed cleanly, because §8's *checks* are
  a numbered list and item 1 also resolves as "8.1". Every docstring citing 8.1 would have
  gone on resolving — to **"Existence"**, a different check entirely. Silent, and exactly
  what the guard exists to prevent. `### 10.1 Demo mode` has the same shadow (§10's screen 1
  is Ingest), and code cites 10.1 four times meaning the heading.

  Removing the overlap by renumbering is not free: `8.2` and `8.3` are cited *as list
  items*, so §8's checks must stay a numbered list. So the fix is a pin — the set of cited
  numbers that resolve to a real heading is asserted explicitly, and a second test pins the
  two known ambiguous numbers so a third cannot appear unnoticed. All three renumberings
  (8.1, 10.1, 7.4) are now caught. **Left open:** the underlying ambiguity is documentation
  debt, and a deliberate renumbering pass is the real repair.

- **A `git checkout --` in a mutation loop reverted uncommitted work.** Restoring the three
  mutated docs, the loop reverted `docs/WORKFLOWS.md` — whose Stage 7 edits were written but
  not yet staged — and nothing failed, because the reverted content was documentation. Found
  by reading `git status` rather than by any test. The lesson is narrow and worth keeping:
  **mutation testing must restore from a copy taken before the mutation, never from git,
  while the working tree has uncommitted work in it.** The two ARCHITECTURE mutations in the
  same loop were fine only because a `cp` from a backup ran after the checkout.

---

## Stage 7 complete — exit criteria audited, not assumed — 2026-08-25

**Exit (WORKFLOWS §13): "entire §12.6 green; airplane-mode run of the full pipeline
succeeds."** Both met. 426 tests passing.

Auditing §12.6 line by line rather than assuming it turned up **two requirements that had
never been written at all**:

- **The money grep-invariant** (§12.6, §11.1) — a repo-wide scan for `float(` applied to any
  field named amount/fee/gst/credit/net. There is no `float(` anywhere in `closo/` today, so
  the scan passes trivially — and a scan over a codebase with nothing to find passes whether
  or not the pattern works, which is the dead-test shape found in five earlier stages. It
  has a companion test asserting the pattern matches real offenders and spares a legitimate
  `float(count) / float(total)`.

- **The resolutions-table diff.** The determinism test compared scorecards, which count
  states — two rows could swap their verdicts entirely and stay in the same states, leaving
  every headline number untouched. The table is now diffed row by row, with a second test
  proving the comparison is deep enough to see a changed payment list rather than only a
  changed status.

**Fresh clone, checked rather than claimed** (§14). Cloned into a temp directory: 426 tests
pass with no `.env` and no `GEMINI_API_KEY`, and `scripts/real_api_run.py --offline` runs the
full three-layer pipeline to 95.7% / 100% / zero false resolutions on 48 cache hits and zero
requests. `api_cache.json` arrives byte-identical (sha256 matches, no CRLF), so
`.gitattributes` covers the new file as it covers the CSVs.

**Where Stage 7 leaves the project**

| | Stage 6 | Stage 7 |
|---|---|---|
| Match rate | 83.0% | **95.7%** |
| Verified accuracy | 100% | **100%** |
| False resolutions | 0 | **0** |
| False escalations | 6 (all pending Layer 2) | **0** |
| ₹ stuck | ₹294K | **₹72K** |
| Demo offline | Layer 1 only | **all three layers** |

**Open for Stage 8 and beyond:**

- **Stage 8 — the full UI.** Everything it needs already exists on `RunOutcome`
  (`verdicts`, `verifications`, `needs_signoff`, `agent_matches`) and in the `events` table,
  which now carries `layer2` tool calls and `layer3` checks. The drill-down should show an
  E4: hypothesis → evidence → arithmetic → independent verification → the sign-off question.
- **Section-number ambiguity is documentation debt.** `8.1` and `10.1` each resolve two ways;
  both are pinned by tests now, but a deliberate renumbering pass is the real repair.
- **Three modules still over ~300 code lines**: `generator.py` (392),
  `layer2_investigator.py` (340), `pipeline.py` (311).
- **The cache is a live dependency of the demo.** Change the system prompt, the opening
  brief, the exception order or a tool's output and every key misses. The zero-misses test
  is the alarm; re-recording costs ~48 requests.
- The first suite run of the session failed both subprocess-based import tests and has been
  green in every run since (a dozen or more). Still unexplained.

---

# Stage 8 — the full UI

Started 2026-08-25. Substeps 1 and 2 only this session; screens 2, 4 and 5 remain.

## Substep 1 — a replayed run carries its investigation — 2026-08-25

**Done:** the full verdict and the full verifier result are now written to the `events` log
per exception, and `replay()` rebuilds `outcome.verdicts` and `outcome.verifications` from
them. 8 new tests; full suite **434 passing**.

**Why this is first:** `replay()` restored *zero* verdicts and zero verifier results. Every
terminal state and every scorecard figure came back correctly, so nothing looked wrong — but
Stage 8's drill-down and escalation screens read verdicts, evidence and per-check results,
which means both screens would have rendered empty **on the exact path the demo falls back
to when the network dies**. Building them on top of that would have hidden it behind a
screen that looks plausible until the moment it matters.

**Decisions:**

- **The full structures go in `events`, not in `resolutions`.** §9.1 already makes the
  events table what replay mode reads, and it is the only store that *can* hold these: a
  settlement-side exception (E9) has no bank credit, so it has no `resolutions` row to hang
  a verdict on. Two of the nine verdicts in a demo run are E9's, and they were unrecoverable
  by construction. The summary fields on the existing `verified` event stay — they are what
  makes a log dump readable — and the new `verdict_recorded` / `verification_recorded`
  events carry the complete objects.

- **Rebuilt through pydantic, not as raw dicts.** `Verdict.model_validate` puts the
  arithmetic block back as `Decimal`; a dict would put a float on the one screen that claims
  to show exact figures. A test asserts every replayed amount is still `Decimal`.

**Surprises:**

- **One of the new tests could not fail.** `test_a_settlement_side_verdict_survives_a_replay`
  compared exception ids against bank-transaction ids — two disjoint namespaces — so its
  comprehension was always empty and the assertion reduced to `9 == 9 > 0`. It passed
  against a correct implementation and would have passed against any other. Rewritten to take
  the settlement-side exceptions from Layer 1's own output. Then the rewrite left a dead
  assertion behind (`not in {… for row in ()}`, an empty set comprehension), removed too.
  **A test written to cover a subtle case is where a dead test is most likely to hide**,
  because the shape of the code is doing the convincing rather than the assertion.

**Verified:** 434 passing. Mutation-tested four ways, all caught — not restoring verdicts
(4 failures), not restoring verifications (4), dropping the evidence trail from the recorded
verdict (3), dropping the verifier's checklist (2).

**Note:** `pipeline.py` is now 333 code lines against §11.9's ~300, up from 311. The
Layer 2/3 orchestration is the part that would split out cleanly.
