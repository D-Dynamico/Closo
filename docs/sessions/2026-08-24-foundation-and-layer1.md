# 2026-08-24 — Foundation and Layer 1 (Stages 0–3)

Scaffold, schemas/config, the synthetic generator with its frozen seed-42 dataset, and the
deterministic Layer 1 cascade. Ends with 193 tests passing offline and 83% auto-match at
zero false matches.

**Read the Surprises sections.** Three of the more serious bugs in this session were found
while writing a test or a commit message, not by anything failing.

---

## Stage 0 — Scaffold — 2026-08-24

**Done:** Repo skeleton per §4 (`closo/`, `app/`, `tests/`, `data/generated/demo/`), `requirements.txt`, `.env.example`, `.gitignore`, `pyproject.toml` with pytest config, `Makefile`, a navigable five-screen Streamlit placeholder, and three thin scaffold tests. `.venv` created and all dependencies installed. `pytest` green (3 passed); `streamlit run` returns HTTP 200. CLAUDE.md rewritten for the provider switch and given a new §15.

**Decisions:**

- **Gemini instead of Anthropic, on `gemini-3.7-flash`.** No Anthropic key available — it is paid. Rejected `gemini-2.5-flash` (the first pick) because it is two generations behind and weaker at multi-step tool use, which is precisely Layer 2's job; the only thing it had going for it was publicly documented free limits. Rejected `gemini-2.5-pro` because at least one source says Google pulled Pro's free tier in April 2026. Rejected the flash-lite variants because lite models fumble multi-hypothesis reasoning, and E4/E5 are multi-hypothesis by construction. `gemini-3.7-flash` is current, stable, free-tier, and Google positions it explicitly for agentic tool-calling workflows.

- **A provider seam at `closo/llm_client.py` rather than calling the SDK from Layer 2.** Keeps the §12.5 mocked suite free and offline, and confines any future provider swap to one file. This is also what the no-LLM-import invariants in §11.3 now assert against.

- **Response caching in SQLite, decided before Layer 2 exists.** The free tier is rate-limited and a full run is up to ~180 requests, so an uncached design would burn a day's quota in one or two runs and could strand the demo. Caching makes re-runs, tests and the demo cost zero.

- **CLAUDE.md edited in place rather than given an addendum.** It declares itself the single source of truth; an addendum would have left §3 and §15 contradicting each other, and a future session reading §3 top-down would have wired Anthropic.

- **SESSION.md gitignored.** It is a working log for whoever is building, not a deliverable for judges. Keeping it out of history means it can be written honestly.

- **Streamlit telemetry disabled in `.streamlit/config.toml`.** Not housekeeping — see Surprises.

**Surprises:**

- Booting the placeholder printed *"Collecting usage statistics"*. Streamlit pings home on startup, which directly violates §10.1's "no network calls at all". Harmless-looking in development, and exactly the kind of thing that hangs a boot in an airplane-mode room on stage. Fixed now while it costs nothing.

- The Gemini model lineup has moved much further than CLAUDE.md assumed — `gemini-3.7-flash`, `3.6-flash`, `3.5-flash` and two flash-lite generations are all stable, and `gemini-2.0-flash` is already shut down. Anything written against the 2.5 family is starting on a clock.

- Google has **removed the per-model free-tier rate limit table** from the public docs and now points at AI Studio for your own live limits. The 10 RPM / 250 RPD figures circulating are 2.5-era third-party numbers and should not be trusted for 3.x.

**Open:**

- **Check your actual free-tier limits at https://aistudio.google.com/rate-limit before the first live Layer 2 run (Stage 6).** This is the one number that decides whether a live run fits in a day. Not a blocker for Stages 1–2, which make no LLM calls.
- Next: **Stage 1** — `config.py` (the `money()` quantizer, fee schedules v1/v2 + cutover, UTR normalization regexes), `schemas.py`, the `llm_client.py` interface, and their tests. The truncated-UTR case must return `None`; §12.2 forbids prefix matching.

---

## Stage 1 — Schemas + config — 2026-08-24

**Done:** `closo/config.py` (`money()` quantizer, GST helper, fee schedules v1/v2 + cutover, UTR normalization, business-day windows), `closo/schemas.py` (all source records, match records, verdicts, verifier results, three terminal states), `closo/llm_client.py` (the `LLMClient` protocol, `MockLLMClient`, `GeminiClient` skeleton, `RequestBudget`, cache keys). 98 new tests; full suite 101 passing and order-independent.

**Decisions:**

- **`money()` raises `TypeError` on a float rather than quantizing it.** A float that has already lost a paisa cannot be rescued by rounding it later, so absorbing it silently would defeat §11.1. Refusing outright is the only defence that does not depend on someone noticing.

- **The Decimal-as-string serializer rides on the `Money` annotated type, not on the base model config.** Pydantic's `json_encoders` was the shorter route but is deprecated and disappears in v3. Putting it on the type also means the guarantee travels into nested structures — notably the `arithmetic` block inside a verdict, which is where a stray float would do the most damage and be hardest to spot.

- **v1 and v2 differ on *every* payment method**, enforced by a test. If they agreed anywhere, E4 (settlement computed under the wrong schedule) would be invisible for those payments and the generator would be emitting unfalsifiable test data.

- **UTR is exactly 16 characters, and length is load-bearing.** A truncated UTR must fail to parse rather than prefix-match. §12.2 forbids prefix matching, and the reasoning is asymmetric: an unmatched row is an honest exception, a wrongly-matched row is a confident lie.

- **Two distinct UTR candidates → `None`.** §11.6 applied at the parsing layer. The same UTR repeated twice still resolves, because repetition is not ambiguity.

- **`add_business_days` lives in `config.py`, not the matcher.** A naive +3-day window silently rejects every Friday settlement, and that surfaces as a mysteriously low match rate rather than as an error.

- **`FinalStatus` has three members and the docstring says there is no fourth.** The temptation later will be to add `NEEDS_REVIEW` for `probable` verdicts. That case is a boolean flag on `Resolution`, not a state — keeping it that way preserves the invariant that a verdict failing verification is escalated, never resolved.

- **Budget guard and cache live in `llm_client.py`, not the investigator.** Requests are the scarce resource on this quota, so the module that spends them should be the one that counts them. RPM exhaustion sleeps; RPD exhaustion raises `QuotaExhausted` for the investigator to catch and wind the batch down.

- **A cache hit reports zero tokens** rather than replaying the original count, so cost-per-record on the scorecard stays honest about what the run actually spent.

**Surprises:**

- **Writing the tests found two real bugs, not just confirmed existing behaviour.** `GeminiClient` imported the SDK *before* validating the API key, so the most likely first-run failure still pulled `google.genai` into `sys.modules` and quietly weakened the §11.3 invariant. Key check now comes first.

- **The import-invariant test was itself order-dependent.** Reading `sys.modules` in-process meant an unrelated earlier test importing the SDK decided the result. That is exactly how an import invariant stops being enforced without anyone noticing. It now runs in a clean subprocess, and the suite was re-run in a different file order to confirm.

- Pydantic v2 still accepts `json_encoders` but emits a `PydanticDeprecatedSince20` warning; it is removed in v3. Caught only because the smoke test was run with `-W error::DeprecationWarning`. Worth keeping that habit.

**Open:**

- `GeminiClient._call_api` deliberately raises `NotImplementedError` — it is wired in Stage 6 alongside the investigator. Nothing before then makes a network call.
- Still unverified against a live API: whether `gemini-3.5-flash-lite` handles forced function calling well enough for `submit_verdict`. First real signal comes in Stage 6.
- Next: **Stage 2** — the synthetic generator. Build order matters: clean records first, then E2–E8, then E9/E10 *with* the brute-force unresolvability guard, then freeze seed 42. The guard is the piece most likely to be skipped and most costly to skip, since an accidentally-resolvable E10 breaks the honest-exception-list story with no visible symptom.

---

## Stage 2 — Synthetic generator + frozen dataset — 2026-08-24

**Done:** `closo/taxonomy.py` (class counts, settlement arithmetic, E10 guard), `closo/dataset_io.py` (CSV/JSON write + load), `closo/generator.py` (the `Generator`, CLI). All ten error classes seed. Seed 42 frozen into `data/generated/demo/` — 150 payments, 46 settlements, 47 bank credits, 150 orders. 52 new tests; full suite 153 passing.

**Decisions:**

- **Split the generator three ways at 785 lines.** Not only about length. Stage 3's matcher must load a batch without importing the thing that produced it, and `load_batch` deliberately has *no parameter* for ground truth — the cheapest way to pass the §11.4 quarantine test is to give the load path no way to reach the file.

- **Settlements are class-homogeneous.** Mixing an E4 payment into an otherwise clean settlement would leave that settlement's ground-truth error class genuinely ambiguous, and ambiguous ground truth cannot be used to measure accuracy.

- **E1 is computed as the remainder**, so the batch is exactly 150 by construction rather than by luck. CLAUDE.md says "~118"; the real number is 120.

- **E9 lives in a separate `missing_settlements` key**, not in `ground_truth`. Ground truth is indexed by bank transaction and an E9 has none — which is exactly what makes it unresolvable.

- **`bank_stmt.csv` ships the `utr` column blank.** See Surprises — this was the most consequential catch of the stage.

- **E5 legs are 40/60, not 50/50.** A half-and-half split would let a matcher guess the pairing from the amounts alone and the class would prove nothing.

- **E4 is generated as v1-applied to a post-cutover settlement**, with a discrepancy tested to exceed the ₹2 Pass C tolerance. If the two schedules differed by less than that for these payments, Pass C would absorb E4 into a tolerance match and it would never reach the investigator.

- **Spec counts duplicated as literals in the test file.** Asserting only against `PAYMENT_CLASS_COUNTS` proved the generator agreed with itself; the table could drift from §5.2 with everything still green.

**Surprises:**

- **The generator was shipping a pre-parsed `utr` column in `bank_stmt.csv`.** A real bank statement never does that — it gives you a narration with the reference buried in junk, and recovering it is Pass A's whole job (§6). With the column populated, Layer 1 could skip `normalize_utr` entirely and **E8 would have silently stopped testing anything** while still appearing in the taxonomy. Column is now blank; exactly 2 of 47 narrations fail to parse and both are the E8 rows.

- **`FEE_CUTOVER_DATE` was 2026-03-01, a Sunday**, and the day before is a Saturday. Banks don't settle at weekends, so no settlement could ever land on the boundary §12.1 requires, and the pre-cutover settlement was dated to a weekend. Moved the cutover to Monday 2026-03-02 and added `previous_business_day()`.

- **Git was converting the frozen CSVs to CRLF on checkout.** The generator writes LF and the determinism test hashes files, so **a fresh clone would have failed determinism on someone else's machine** — the worst place to find it, and directly against the §14 fresh-clone requirement. Fixed with `.gitattributes` pinning LF, verified by actually cloning into a temp dir and hashing all five files against a fresh generate.

- **The cutover-boundary settlements were adding 4 payments on top of 150** rather than drawing from E1's allocation, so the batch was quietly 154.

- **Mutation-tested the suite twice** (wrong class count, neutered E10 guard). Both caught. Worth repeating on the verifier in Stage 5 — a test suite for a verifier that doesn't bite is worse than none.

**Open:**

- **Spec conflict to settle in Stage 5.** §5.2 says E4 is resolvable by Layer 2, but §8.3 says the verifier rejects any verdict citing a schedule that isn't active at `settled_at` — and E4 *is* the wrong-schedule case, so the correct verdict necessarily cites the inactive one and gets killed. Plan: verifier confirms the arithmetic reproduces the bank credit exactly but caps a schedule anomaly at `probable`, landing it in the "verified math, unverified intent" sign-off list §8 already describes. Needs a CLAUDE.md §8.3 wording change.

- **`generator.py` is 364 code lines against §11.9's ~300.** Deliberate: splitting a cohesive `Generator` class further across files would cost more readability than it buys. Flagged rather than hidden.

- Layer-1-vs-Layer-2 resolvability per class is not yet validated — that gets measured in Stage 3. Current expectation is that E2, E7 and E8 fall to Layer 1, leaving E3/E4/E5/E6 for the investigator plus E9/E10 escalating. Fewer LLM exceptions than CLAUDE.md assumed, which given a 500 RPD budget is a benefit, not a loss.

- Next: **Stage 3** — the Layer 1 cascade (Pass A UTR exact → Pass B amount+date window → Pass C netting recompute), exception queue, audit events. Exit: ≥80% auto-match, zero false matches vs ground truth.

---

## Stage 3 — Layer 1 matcher — 2026-08-24

**Done:** `closo/layer1_matcher.py` — Pass A (UTR exact) → Pass B (amount + T+3 business-day window) → Pass C (netting recompute, ±₹2), exception queue, audit events. **83.0% auto-matched (39/47), zero false matches.** 38 new tests; full suite 193 passing.

**Results on seed 42:**

| | count | classes |
|---|---|---|
| Pass A | 35 | E1, E2, E3 |
| Pass B | 2 | E8 |
| Pass C | 2 | E7 (incl. one at exactly ₹2.00) |
| Exceptions | 10 | E4 ×2, E5 ×2, E6 ×2, E9 ×2, E10 ×2 |

E9/E10 escalate exactly as designed. Layer 2 gets 8 bank-side exceptions — fewer than CLAUDE.md assumed, which on a 500 RPD budget is a benefit.

**Decisions:**

- **Uniqueness is checked in both directions.** A credit with two candidate settlements is ambiguous, *and* a settlement claimed by two credits is too. Only the first is obvious; missing the second would let one settlement be matched twice and double-count money on the scorecard.

- **Pass C recomputes with the schedule active at `settled_at`, not the one recorded on the settlement.** Trusting the recorded schedule would make Pass C agree with a payout that ran on a stale one — which is exactly E4, and it must not resolve here.

- **Pass A falls through on an amount disagreement rather than raising an exception** (§12.2). A matching UTR with differing amounts is precisely what Pass C exists to explain.

- **Settlement-side sweep only fires when the settlement's UTR appears nowhere in the statement.** See Surprises.

- **pandas is used for the Pass A join only.** It's genuinely a join. The amount and tolerance logic stays in plain Python + Decimal, where pandas would obscure rather than help.

**Surprises:**

- **E4 and E7 were generated in a shape no matcher could see.** Both had the settlement record *and* the bank credit computed the same way, so they agreed with each other and Pass A matched cleanly. E4 would never have reached the investigator despite being a Layer 2 class, and E7's drift would never have exercised Pass C's tolerance branch — it would have been covered only by hand-built fixtures, which is the kind of coverage that looks fine and proves nothing. Both discrepancies now live *between* the record and the credit. One E7 case lands on a drift of exactly ₹2.00, so the inclusive boundary is exercised by real data.

- **E4 was being reported as two exceptions** — an unmatched credit *and* a settlement with "no bank credit". One problem, and the second message was plainly false. Now a settlement whose UTR appears anywhere in the statement is logged rather than escalated, so only genuinely-absent settlements (E9) sweep as `no_counterpart`. Exception count dropped 12 → 10, one per real problem.

- **E10 was reported as `outside_tolerance`** when the truth is that no settlement carries its UTR at all. Different problem, different escalation note. Sweep now distinguishes `no_utr_match` from `outside_tolerance`.

- **One test fixture was wrong rather than the code** — it dated the credit before the second settlement, so there was no real window overlap to be ambiguous about.

- **Mutation-tested three ways** (guess-on-tie, short-UTR regex, exclusive tolerance). All caught. But it also revealed the layer-1 truncated-UTR test is weak — it only proves fall-through, which several wrong implementations would also do. Real protection is in the config regex tests, which did catch it. Logged rather than papered over.

**Open:**

- `git add -A` bundled the matcher into the generator-fix commit; split with a soft reset. Watch for that — stage the specific files.
- Two duplicate commits remain in history from Stage 2 (`ab00838`/`c875d97`, same message). Harmless; squash if the history matters for judging.
- **E4 spec conflict still unresolved** (§5.2 vs §8.3) — decide in Stage 5. Now more concrete: E4 exceptions carry reason `outside_tolerance`, and the correct verdict will cite v1 while v2 is active.
- Layer 2 will need to tell E5 from E6: both surface as `duplicate_utr`, distinguished only by the detail string ("2 credits and **1** settlement" vs "**2** settlements"). Consider a distinct reason in Stage 6 if the investigator struggles.
- Next: **Stage 4** — audit log (SQLite, append-only trigger), `pipeline.run()` Layer-1-only, Streamlit Ingest + Scorecard on real numbers. This is the checkpoint after which there is always a demoable product.
