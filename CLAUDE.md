# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Protocol (READ FIRST — keep the project's memory rich)

This repo keeps a durable, written memory so every session starts with full context. **You
must maintain it** — it is not optional:

1. **At the start of a session**, skim the newest file in [`docs/sessions/`](docs/sessions/)
   (and this file). That is where the last session recorded what changed, why, and what's
   still open. [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) is the deep architecture +
   test-plan reference; its section numbers are cited from docstrings throughout `closo/`.
2. **After each successful, behavior-changing task**, update the running session note
   `docs/sessions/<YYYY-MM-DD>-<topic>.md` (create it on the first such task of the session):
   what changed, why (the decision, not just the diff), files touched, and how you verified it.
   Trivial/no-op turns don't need an entry.
3. **At session end**, make sure that note is complete — scope, decisions *with reasoning*,
   surprises, open items — and update any doc whose *behavior* changed (`README.md`,
   `docs/SYSTEM_DESIGN.md`).
4. **Record surprises, not just successes.** Several of the worst bugs so far were found
   while writing a test or a commit message, not by the code failing. A note that lists only
   what shipped is worth much less than one that says what nearly went wrong.

## Working practice

- **One commit per substep.** A substep is one bounded change, not a whole stage. Stage the
  specific files — `git add -A` has already bundled unrelated work into the wrong commit once.
- **Push once per stage**, after that stage's exit criteria in `docs/SYSTEM_DESIGN.md` §13
  pass. Never push a stage whose tests are red.
- **Commit title: one line.** Imperative, no trailing period, no `type(scope):` prefix.
- **Commit description: humanized.** Write it the way you'd explain the change to a teammate —
  what changed, why it mattered, what you decided against. Full sentences, not a list of
  filenames. The diff already says which files moved; the message says what was in your head.

## What this is

Closo is a **self-verifying three-way reconciliation agent** built for the RazorPay AI
Buildathon, Track 04 (AI Finance Controller). It ingests three sources (Razorpay
payments/settlements, a bank statement, an internal order ledger), reconciles a 150-record
batch, resolves the leftovers with an LLM investigator whose every verdict is independently
re-checked by deterministic math, and reports honest metrics **including the exceptions it
could not resolve**.

**The thesis, and the reason behind every design choice here:** verification capacity, not
generation speed, is the bottleneck in finance ops. So **nothing is ever marked resolved
unless a separate deterministic verifier confirms the math independently.** The LLM
proposes; the verifier disposes.

Out of scope on purpose: cash forecasting, multi-currency, auth, settings screens, real
Razorpay production API usage. One loop, closed completely.

## Commands

```bash
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -r requirements.txt

./.venv/Scripts/python.exe -m pytest                        # full suite, offline, no API key
./.venv/Scripts/python.exe -m pytest tests/test_layer1.py   # one file
./.venv/Scripts/python.exe -m streamlit run app/streamlit_app.py

# Regenerate the frozen demo dataset (must stay byte-identical — see Conventions)
./.venv/Scripts/python.exe -m closo.generator --seed 42 --out data/generated/demo
```

`make test` / `make run` / `make generate` wrap the same commands. On Windows the venv
interpreter is `./.venv/Scripts/python.exe`; a bare `python` is the system 3.12 with no deps.

## Architecture

Three layers, each consuming only what the previous one could not resolve:

- **Layer 1 — `layer1_matcher.py`** (pandas + Decimal, no LLM). A cascade over a shrinking
  pool: Pass A exact UTR join → Pass B exact amount inside a T+3 business-day window →
  Pass C netting recomputation within ±₹2. Currently 83% auto-matched, zero false matches.
- **Layer 2 — `layer2_investigator.py`** (Gemini, tool calling). One isolated conversation
  per exception. Forms hypotheses, calls read-only tools, emits a structured verdict.
- **Layer 3 — `layer3_verifier.py`** (pure Python, no LLM). Re-checks every verdict from raw
  records. PASS → resolved. FAIL → escalated, with the rejected verdict preserved.

Every bank transaction ends in **exactly one of three terminal states** — `AUTO_MATCHED`,
`AGENT_RESOLVED_VERIFIED`, `ESCALATED`. There is no fourth. A verdict that fails
verification is never shown as resolved.

Supporting modules: `config.py` (money quantizer, fee schedules, UTR regexes), `schemas.py`
(pydantic models), `taxonomy.py` (error classes, settlement arithmetic), `generator.py` +
`dataset_io.py` (synthetic data), `llm_client.py` (provider seam).

## Conventions to preserve when editing

- **`Decimal` for money, always — never `float`.** `config.money()` is the only sanctioned
  way to make a rupee value, and it raises `TypeError` on a float rather than quantizing it.
  A float that has already lost a paisa cannot be rescued by rounding it later.
- **The LLM never does arithmetic.** All math flows through `compute_expected_settlement` and
  the verifier. A model-emitted number not reproducible from tool output fails verification.
  A verdict's `arithmetic` block is a *claim*, not evidence.
- **Ambiguity is an exception, never a guess.** Tie-breaks are forbidden in Layer 1 — two
  credits sharing a UTR, two settlements at one amount in overlapping windows, one settlement
  claimed by two credits. An unmatched row is an honest exception; a wrong match is a
  confident lie that reconciles and nobody re-checks.
- **Only `closo/llm_client.py` may import the LLM SDK**, and the import is deferred into
  `GeminiClient.__init__` so importing the module stays side-effect free. Tests assert in a
  *clean subprocess* that `layer1_matcher`, `layer3_verifier` and `metrics` pull in neither
  `google.genai` nor `closo.llm_client` — an in-process `sys.modules` check is
  order-dependent and silently stops enforcing anything.
- **Ground truth is quarantined.** `dataset_io.load_batch()` has no parameter to request it;
  only `metrics.py` may call `load_ground_truth()`, and only after a run completes.
- **Requests, not tokens, are the scarce resource.** Free tier is 15 RPM / 500 RPD on
  `gemini-3.5-flash-lite`; every full-Flash model is capped at 20 RPD and cannot finish a
  single run (§7.4). Cache every response. The budget guard marks the remainder
  `unresolvable — quota exhausted` and stops cleanly rather than dying mid-batch.
- **Determinism is load-bearing.** Same seed → byte-identical files. The generator uses one
  seeded `random.Random` and a fixed build order; reordering the class builders changes every
  generated value. `.gitattributes` pins LF, without which a fresh clone fails the
  determinism test on Windows.
- **Escalation is success, not failure.** Error classes E9 and E10 are *designed* to be
  unresolvable. A run that "resolves" one has a critical bug, not a good day.
- **Every module ≤ ~300 lines.** `generator.py` is currently ~364 and flagged.

## Status

Stages 0–3 complete (scaffold, schemas/config, generator + frozen dataset, Layer 1).
193 tests passing, offline. Next: **Stage 4** — SQLite audit log with append-only trigger,
`pipeline.run()` on Layer 1 only, Streamlit Ingest + Scorecard on real numbers.

Two open items carried forward, detailed in the newest `docs/sessions/` note: the **E4 spec
conflict** between §5.2 and §8.3 — the verifier as specified would reject the only correct E4
verdict, and the plan is to cap it at `probable` — and E5/E6 both surfacing as
`duplicate_utr`, distinguishable only by the detail string.
