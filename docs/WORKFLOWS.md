# Closo — Workflows

How things actually get done: producing the dataset, the screens and demo mode, the
stage sequence with its exit criteria, and what finished looks like.

Section numbers are load-bearing: docstrings across `closo/` cite them, and
`docs/SYSTEM_DESIGN.md` maps every number to the file it lives in. Do not renumber.

---

## 5. Synthetic data generator (build this FIRST)


The entire metrics story depends on seeded ground truth. The generator produces three CSVs plus a hidden `ground_truth.json` that maps every bank credit to its true source records and true error class.

### 5.1 Record schemas

**payments.csv** (Razorpay side; one row per captured payment, plus settlement rows)
`payment_id, order_id, amount_gross, method (upi|card|netbanking), captured_at, settlement_id, settlement_utr, settled_at, fee_mdr, fee_gst, amount_settled, status (captured|refunded|partial_refund|disputed), refund_amount`

**bank_stmt.csv** (bank side)
`txn_date, value_date, narration, utr, credit_amount, balance`
Narrations must be realistically ugly: `"NEFT-RAZORPAYSOFTWARE-<utr>-SETTLEMENT"`, truncated UTRs, bank-specific junk prefixes.

**order_ledger.csv** (merchant internal)
`order_id, sku, order_amount, order_date, channel, expected_settlement`

### 5.2 Volume and mess taxonomy

Generate **150 payment records** rolling into ~40 settlements. Money math per settlement: `amount_settled = Σ gross − Σ MDR − Σ GST(18% of MDR) ± rounding`. MDR by method: UPI 0% (but include a flat-fee variant), cards 2%, netbanking flat ₹10 — define two fee schedules `v1`/`v2` in `config.py` and apply `v2` to settlements after a cutover date.

Seed EXACTLY these error classes, with counts, and record each in ground truth:

| # | Error class | Count | Resolvable by |
|---|---|---|---|
| E1 | Clean straight-through match | ~118 | Layer 1 |
| E2 | T+2/T+3 settlement lag (date window miss) | 8 | Layer 1 (window) or Layer 2 |
| E3 | Partial refund netted into settlement | 5 | Layer 2 |
| E4 | Fee-schedule mismatch (v1 assumed, v2 applied) | 4 | Layer 2 |
| E5 | Split settlement (one payment across two bank credits) | 3 | Layer 2 |
| E6 | Duplicate UTR in bank statement | 2 | Layer 2 |
| E7 | Rounding drift (±₹1–2) | 4 | Layer 1 tolerance or Layer 2 |
| E8 | Missing UTR in bank narration | 2 | Layer 2 (amount+date inference) |
| E9 | Genuinely missing settlement (money truly absent) | 2 | NOBODY — must be escalated |
| E10 | Bank credit with no Razorpay counterpart (foreign credit) | 2 | NOBODY — must be escalated |

E9/E10 exist so the honest exception list is non-empty BY DESIGN. A correct run escalates exactly these (plus any verifier rejections). If the pipeline "resolves" an E9/E10, that is a critical bug.

### 5.3 Generator rules
- `--seed N` CLI flag; same seed → byte-identical CSVs. Freeze `seed=42` output in `data/generated/demo/` and commit it.
- All amounts via `Decimal`, quantized to 2 places, `ROUND_HALF_UP`.
- Write `ground_truth.json`: `{bank_txn_id: {source_payment_ids: [...], error_class: "E4", true_resolution: "..."}}`. The pipeline must NEVER read this file; only `metrics.py` may, and only after the run completes.

---

---

## 10. Streamlit UI


Five sidebar screens; color code is global and sacred: green `#C0DD97` = auto, amber `#FAC775` = agent+verified, red `#F09595` = escalated.

1. **Ingest:** three source cards (name, record count, date range) + one primary button "Run reconciliation". Nothing else.
2. **Live run:** Layer 1 progress with a fast counter and records/min; then exception queue where each exception is an `st.status(expanded=True)` block streaming the investigator's steps ("testing settlement-lag hypothesis… querying refunds…"), ending with a distinct, slightly delayed verifier line (✓ or ✗). The delay (300ms `time.sleep` in demo mode) is deliberate — verification must READ as a separate step.
3. **Scorecard:** `st.columns(4)` metrics (match rate, verified accuracy, ₹ reconciled, ₹ stuck), Plotly horizontal stacked tier bar, exception taxonomy table, throughput + cost line.
4. **Exception drill-down:** selectbox per exception → hypothesis, rejected hypotheses (strikethrough), every tool call with result, arithmetic in a monospace `st.code` block, verifier checklist with per-check ✓/✗, confidence tier.
5. **Escalation queue:** each item shows hypotheses tried-and-rejected and "what would unblock this" (e.g., "need bank narration field", "raise with Razorpay support — settlement genuinely absent"). Verifier-rejected verdicts appear here labeled "agent proposed, verifier rejected".

Optional (only if everything else is done): settlement Q&A input at the bottom of Scorecard — one LLM call grounded ONLY on the resolutions table ("why did I get ₹4,720 on the 14th?").

### 10.1 Demo mode (non-negotiable)
`DEMO_MODE=1` in `.env`:
- No network calls at all; Layer 2 serves from the committed response cache
  (`data/generated/demo/api_cache.json`) through a client that holds no API key and no SDK
  handle, and the RZP client serves from `api_cache` / local CSVs. With an empty cache the
  run is Layer 1 only and says so, rather than reporting an investigation that never
  happened.
- Fixed seed 42; every number identical run-to-run (assert in e2e test).
- A completed run can be **replayed** from the `events` table with realistic pacing — if wifi or the LLM API dies on stage, replay of the last good run is indistinguishable from live. Wire a small "Replay last run" button on Ingest.

---

---

## 13. Stages — completion order with exit criteria


Work strictly in order; a stage is DONE only when its exit criteria pass. Never start a stage with the previous one red. Rough hackathon budget in parentheses (total ≈ 24 focused hours; cut from the bottom, never the middle).

**Stage 0 — Scaffold (1h).**
Steps: repo layout from §4, `requirements.txt`, `.env.example`, pytest wiring, CI-ish `make test`.
Exit: `pytest` runs (0 tests, green), `streamlit run` shows a placeholder page.

**Stage 1 — Schemas + config (1.5h).**
Steps: pydantic models for all records/verdicts, fee schedules v1/v2 with cutover, UTR normalization regex library, Decimal helpers (`money()` quantizer).
Exit: schema round-trip tests green; regex library passes narration fixtures.

**Stage 2 — Synthetic generator + frozen dataset (3h).**
Steps: implement §5, CLI with `--seed`, write ground truth, freeze seed-42 output into `data/generated/demo/`, commit it.
Exit: full §12.1 green, including the E10 brute-force unresolvability check.

**Stage 3 — Layer 1 matcher (3h).**
Steps: Pass A → B → C on the demo set; exception queue emission; audit events for every decision.
Exit: §12.2 green; ≥80% auto-match, zero false matches vs ground truth.

**Stage 4 — Audit log + pipeline skeleton + minimal UI (2h).**
Steps: SQLite tables + append-only trigger, `pipeline.run()` executing Layer 1 only, Streamlit Ingest + Scorecard rendering real Layer-1-only numbers.
Exit: e2e determinism test green for the Layer-1-only pipeline; Scorecard shows live numbers, remainder shown as red/pending.
*Checkpoint: from here you always have a demoable product. Everything after this improves it.*

**Stage 5 — Tools + Verifier (3h). Verifier BEFORE investigator — the checker must exist before the thing it checks.**
Steps: implement §7.1 tools against local data + cache; implement §8 checks; hand-craft the §12.4 verdict fixtures.
Exit: §12.3 + §12.4 fully green, including off-by-a-paisa and internally-consistent-but-wrong.

**Stage 6 — Exception investigator (4h).**
Steps: system prompt, tool-use loop with budget/timeout, `submit_verdict` forcing, retry-once on malformed output, mocked-client test suite; then one manual real-API run on the demo set.
Exit: §12.5 green (mocked); real-API manual run resolves E3/E4/E5/E6/E8 and escalates E9/E10; §12.6 green with mock.

**Stage 7 — Full pipeline hardening (2h). DONE 2026-08-25.**
Steps: wire Layer 2+3 into `pipeline.run()`, ground-truth quarantine, scorecard cross-checks, cost metrics, cache the real-API run's responses into `api_cache` for offline replay.
Exit: entire §12.6 green; airplane-mode run of the full pipeline succeeds.
*Met: §12.6 green, and the airplane-mode run is itself a test rather than a procedure. On seed 42 the full pipeline reaches 95.7% at 100% verified accuracy with zero false resolutions, escalating exactly E9 and E10.*

**Stage 8 — Full UI (3h).**
Steps: Live-run streaming with `st.status` + delayed verifier stamp, drill-down, escalation queue with rejected-hypotheses strikethrough, Replay-last-run button, global color code.
Exit: §12.7 checklist passes end-to-end in airplane mode.

**Stage 9 — Demo polish (1.5h).**
Steps: README quickstart + 4-minute demo script, pick the ONE drill-down exception to show on stage (an E4 fee-schedule case tells the best story), rehearse twice with a timer, verify fresh-clone-to-demo < 2 minutes.
Exit: two consecutive rehearsals inside 4 minutes with zero live typing beyond clicks.

**Stage 10 — Optional flourish (only if 0–9 done).** Settlement Q&A box grounded on the resolutions table; token/₹ cost line on Scorecard if not already done.

Cut policy under time pressure: drop Stage 10, then compress Stage 8's escalation screen to a table, then drop E5/E6 from the demo narrative (keep them in data). NEVER cut the verifier, the mocked test suite, or replay mode — they are the pitch.

---

## 14. Definition of done


- Fresh clone, `DEMO_MODE=1`, no API keys → full demo works offline.
- Scorecard on seed-42: match rate ≥ 93%, verified accuracy reported against ground truth, escalation list contains exactly the designed-unresolvable classes (+ any verifier rejections, shown as such).
- One exception drill-down tells a complete story: hypothesis → evidence → arithmetic → independent verification.
- All tests green; invariant tests (§11) present and passing.

---
