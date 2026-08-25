# Closo

A self-verifying three-way reconciliation agent. RazorPay AI Buildathon, Track 04 —
AI Finance Controller.

Closo reconciles three financial sources — Razorpay payments and settlements, a bank
statement, and an internal order ledger — across a 150-record batch. What it does not
match deterministically, an LLM investigator takes up; and **every verdict that
investigator reaches is independently re-checked by deterministic math before anything is
reported as resolved.**

The point is the last clause. Verification capacity, not generation speed, is the
bottleneck in finance ops, so Closo is built around a checker that can overrule the model.
The LLM proposes; the verifier disposes. Verdicts that fail verification are not hidden —
they appear in the escalation queue labelled *agent proposed, verifier rejected*.

It also reports the exceptions it **could not** resolve. Two error classes in the dataset
are unresolvable by construction, and a run that "solves" one has a bug.

## Quickstart

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt   # Linux/macOS: .venv/bin/python

./.venv/Scripts/python.exe -m pytest                            # 488 tests, fully offline
./.venv/Scripts/python.exe -m streamlit run app/streamlit_app.py
```

No API key is needed, and none is used. The frozen seed-42 dataset ships in
`data/generated/demo/` alongside `api_cache.json` — the model's own responses from a
recorded live run — so all three layers run with `DEMO_MODE=1` (the default) and no network
call of any kind. The client the app builds has no API key and no SDK handle, which is what
makes airplane mode a property rather than a promise.

## How it works

| Layer | Module | Does | Determinism |
|---|---|---|---|
| 1 | `layer1_matcher.py` | UTR join → amount + T+3 window → netting recompute (±₹2) | pandas + Decimal, no LLM |
| 2 | `layer2_investigator.py` | One conversation per exception; hypotheses, read-only tools, structured verdict | Gemini, temperature 0 |
| 3 | `layer3_verifier.py` | Recomputes every verdict from raw records | pure Python, no LLM |

Every bank transaction ends in exactly one of three states — `AUTO_MATCHED`,
`AGENT_RESOLVED_VERIFIED`, or `ESCALATED`. There is no fourth, and a verdict that fails
verification is never shown as resolved.

Five screens: **Ingest**, **Live run** (each exception's tool calls, its verdict, and then
the verifier as a deliberately separate step), **Scorecard**, **Exception drill-down**
(hypothesis → what was ruled out → evidence → arithmetic → five independent checks), and the
**Escalation queue** (what was tried, and what would unblock it). The last three are
rebuilt from the append-only audit log, so a replay shows exactly what happened rather than
a re-enactment of it.

On seed 42, all three layers: **95.7%** of credits reconciled — 39 auto-matched by Layer 1
and 6 resolved by the agent and verified — at **100% verified accuracy** against ground
truth, with **zero false resolutions**. The two escalations left are the two designed
unresolvable credits, which is the correct answer rather than a shortfall. Two of the agent
resolutions carry proven math and unproven intent: the arithmetic reproduces the credit
exactly, but under a fee schedule that was not the active one, so they are flagged for a
human to approve instead of being quietly settled.

## Design choices worth knowing

- **Money is `Decimal` everywhere.** `config.money()` raises on a float rather than
  quantizing it — a float that has already lost a paisa cannot be rescued later.
- **Ambiguity becomes an exception, never a guess.** Every tie-break opportunity in Layer 1
  is declined. An unmatched row is an honest exception; a wrong match is a confident lie
  that reconciles and nobody re-checks.
- **The model never does arithmetic.** Tools compute; the verdict's `arithmetic` block is
  treated as a claim and recomputed from raw records.
- **Ground truth is quarantined.** The data loader has no parameter to request it; only the
  metrics module reads it, and only after a run completes.
- **Escalation is a result, not a failure.** Two error classes are unresolvable by
  construction, and the scorecard reports what is stuck as prominently as what is
  reconciled — including the money.
- **Every model response is cached and committed.** Requests, not tokens, are the scarce
  resource on a free tier. A recorded run replays exactly, which also means the demo shows
  a specific good run rather than hoping for one: temperature 0 does *not* make live
  Layer 2 output reproducible.

## Docs

- [`CLAUDE.md`](CLAUDE.md) — onboarding, commands, conventions
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — thesis, layer contracts, module map, invariants
- [`docs/WORKFLOWS.md`](docs/WORKFLOWS.md) — data generation, UI and demo mode, stages, definition of done
- [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md) — per-module test requirements
- [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) — index mapping each section number to its file
- [`docs/sessions/`](docs/sessions/) — dated notes: what changed, why, and what nearly broke
