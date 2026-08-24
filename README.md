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

./.venv/Scripts/python.exe -m pytest                            # 193 tests, fully offline
./.venv/Scripts/python.exe -m streamlit run app/streamlit_app.py
```

No API key is needed. The frozen seed-42 dataset ships in `data/generated/demo/`, and
`DEMO_MODE=1` (the default) makes no network calls at all — the demo runs in airplane mode.

## How it works

| Layer | Module | Does | Determinism |
|---|---|---|---|
| 1 | `layer1_matcher.py` | UTR join → amount + T+3 window → netting recompute (±₹2) | pandas + Decimal, no LLM |
| 2 | `layer2_investigator.py` | One conversation per exception; hypotheses, read-only tools, structured verdict | Gemini, temperature 0 |
| 3 | `layer3_verifier.py` | Recomputes every verdict from raw records | pure Python, no LLM |

Every bank transaction ends in exactly one of three states — `AUTO_MATCHED`,
`AGENT_RESOLVED_VERIFIED`, or `ESCALATED`. There is no fourth, and a verdict that fails
verification is never shown as resolved.

Current: Layer 1 auto-matches **83%** of credits with **zero false matches** against
ground truth, escalating exactly the two designed-unresolvable classes.

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

## Docs

- [`CLAUDE.md`](CLAUDE.md) — onboarding, commands, conventions
- [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) — the full specification
- [`docs/sessions/`](docs/sessions/) — dated notes: what changed, why, and what nearly broke
