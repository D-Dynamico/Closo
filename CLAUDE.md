# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Protocol (READ FIRST — keep the project's memory rich)

This repo keeps a durable, written memory so every session starts with full context. **You
must maintain it** — it is not optional:

1. **At the start of a session**, skim the newest file in [`docs/sessions/`](docs/sessions/)
   (and this file). That is where the last session recorded what changed, why, and what's
   still open. [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) indexes the spec: which of
   [ARCHITECTURE](docs/ARCHITECTURE.md), [WORKFLOWS](docs/WORKFLOWS.md) or
   [TEST_PLAN](docs/TEST_PLAN.md) each section number lives in. Those numbers are cited from
   docstrings throughout `closo/`, so they must not be renumbered.
2. **After each successful, behavior-changing task**, update the running session note
   `docs/sessions/<YYYY-MM-DD>-<topic>.md` (create it on the first such task of the session):
   what changed, why (the decision, not just the diff), files touched, and how you verified it.
   Trivial/no-op turns don't need an entry.
3. **At session end**, make sure that note is complete — scope, decisions *with reasoning*,
   surprises, open items — and update any doc whose *behavior* changed: `README.md`,
   `docs/ARCHITECTURE.md` (structure and contracts) or `docs/WORKFLOWS.md` (how things run).
4. **Record surprises, not just successes.** Several of the worst bugs so far were found
   while writing a test or a commit message, not by the code failing. A note that lists only
   what shipped is worth much less than one that says what nearly went wrong.

## Working practice

- **One commit per substep.** A substep is one bounded change, not a whole stage. Stage the
  specific files — `git add -A` has already bundled unrelated work into the wrong commit once.
- **Push once per stage**, after that stage's exit criteria in `docs/WORKFLOWS.md` §13
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

## Architecture in one paragraph

Three layers, each consuming only what the previous one could not resolve. **Layer 1**
(`layer1_matcher.py`, pandas + Decimal, no LLM) runs a deterministic cascade. **Layer 2**
(`layer2_investigator.py`, Gemini) investigates the residue, one isolated conversation per
exception. **Layer 3** (`layer3_verifier.py`, pure Python, no LLM) re-checks every verdict
from raw records and can overrule the model. Every bank transaction ends in exactly one of
three terminal states — `AUTO_MATCHED`, `AGENT_RESOLVED_VERIFIED`, `ESCALATED` — and there
is no fourth.

Full detail, module map and layer contracts: **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**.

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
  single run (ARCHITECTURE §7.4). Cache every response. The budget guard marks the remainder
  `unresolvable — quota exhausted` and stops cleanly rather than dying mid-batch.
- **Determinism is load-bearing.** Same seed → byte-identical files. The generator uses one
  seeded `random.Random` and a fixed build order; reordering the class builders changes every
  generated value. `.gitattributes` pins LF, without which a fresh clone fails the
  determinism test on Windows.
- **Escalation is success, not failure.** Error classes E9 and E10 are *designed* to be
  unresolvable. A run that "resolves" one has a critical bug, not a good day.
- **Every module ≤ ~300 lines.** Three now exceed it — `generator.py` (364), `tools.py`
  (364), `layer3_verifier.py` (362), `layer2_investigator.py` (419). Flagged rather than
  hidden; worth a split pass.
- **Mutation-test anything that guards something.** Four stages running, mutating the code
  has exposed a test that could not fail — including, in Stage 5, the two tests protecting
  the project's central claim. Docs count too: renumbering a section slipped past its guard.

## Status

Stages 0–6 complete except one item. 345 tests passing, offline. **There is a demoable product from here
on** — `streamlit run app/streamlit_app.py`, press Run reconciliation, see the Scorecard;
Replay rebuilds it from the audit log with no network at all.

Seed 42: 47 credits, 83.0% match rate, 100% verified accuracy, ₹3.82M reconciled /
₹294K stuck, 2 correct escalations and 6 false ones (all awaiting Layer 2).

**Outstanding:** the Stage 6 real-API run. `GEMINI_API_KEY` is unset, so Layer 2 has only
ever run against the mock. To do it: `cp .env.example .env`, add a key from
aistudio.google.com/apikey, then run the pipeline. Expect ~100 requests against 500 RPD.

Next: **Stage 7** — wire Layers 2 and 3 into `pipeline.run()`, add cost metrics, and cache
the real-API responses into `api_cache` so the demo replays offline.

The **E4 spec conflict is resolved** — ARCHITECTURE §8.1. A verdict citing an inactive fee
schedule has its math checked in full and is capped at `probable` with the anomaly named,
rather than failed outright. Still open: E5/E6 both surface as `duplicate_utr`,
distinguishable only by the detail string.

## Docs

| File | What it is |
|---|---|
| [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) | Index — maps every § number to its file |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Thesis, layer contracts, module map, invariants |
| [`docs/WORKFLOWS.md`](docs/WORKFLOWS.md) | Data generation, UI and demo mode, stages, definition of done |
| [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md) | Per-module test requirements |
| [`docs/sessions/`](docs/sessions/) | Dated notes — what changed, why, and what nearly broke |
