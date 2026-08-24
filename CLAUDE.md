# CLAUDE.md — Closo

Self-verifying three-way reconciliation agent. Built for the RazorPay AI Buildathon, Track 04 (AI Finance Controller).

This file is the single source of truth for any agent working in this repo. Read it fully before writing code. When a decision is not covered here, prefer the choice that maximizes verifiability and determinism over cleverness.

---

## 1. What we are building and why

**One sentence:** Closo ingests three financial data sources (Razorpay payments/settlements, a bank statement, an internal order ledger), reconciles them across a 150-record batch, resolves exceptions with an LLM investigator whose every verdict is independently re-verified by deterministic math, and reports honest metrics including the exceptions it could NOT resolve.

**The judging bar (from the track statement):** throughput + measured accuracy + an honest exception list, across a 50+ record batch. "One cherry-picked match proves nothing."

**Core design thesis (say this everywhere, build this everywhere):** verification capacity, not generation speed, is the bottleneck in finance ops. Therefore **the agent can never mark anything resolved unless a separate deterministic verifier confirms the math independently.** The LLM proposes; the verifier disposes.

**Hard scope boundary — do NOT build:** cash forecasting, tax-line matching beyond GST-on-fees, multi-currency, auth/user accounts, settings pages, real Razorpay production API usage. One loop, closed completely.

---

## 2. Architecture

```
                ┌─────────────────────────────────────────────┐
                │  Synthetic Data Generator (seeded, ground    │
                │  truth known)                                │
                └──────┬──────────────┬──────────────┬────────┘
                       │              │              │
                 payments.csv   bank_stmt.csv   order_ledger.csv
                 (Razorpay      (bank credits)  (internal orders)
                  + settlements)
                       │              │              │
                       ▼              ▼              ▼
┌──────────────────────────────────────────────────────────────┐
│  LAYER 1 — Deterministic Matching Engine (pandas, no LLM)    │
│  Cascade: UTR exact → amount+date-window → netting math      │
│  Target: auto-match ~80–85% with zero hallucination risk     │
└──────────────────────┬───────────────────────────────────────┘
                       │ unmatched residue (~15–20%)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  LAYER 2 — Exception Investigator (LLM + tool calling)       │
│  Per exception: form hypotheses → call tools → structured    │
│  verdict {hypothesis, proposed_match, confidence, evidence}  │
└──────────────────────┬───────────────────────────────────────┘
                       │ every proposed resolution
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  LAYER 3 — Independent Verifier (pure Python, no LLM)        │
│  Re-checks: sums to zero? cited records exist? fee schedule  │
│  valid? PASS → Resolved. FAIL → demote to Unresolvable.      │
└──────────────────────┬───────────────────────────────────────┘
                       ▼
        SQLite audit log ──► Streamlit UI ──► Metrics scorecard
```

Every record ends in exactly one of three terminal states:
- `AUTO_MATCHED` (green) — Layer 1, deterministic
- `AGENT_RESOLVED_VERIFIED` (amber) — Layer 2 proposal that passed Layer 3
- `ESCALATED` (red) — unmatched and either the agent gave up or the verifier rejected the proposal

There is no fourth state. An agent verdict that fails verification is NEVER shown as resolved.

---

## 3. Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | type hints everywhere, `dataclasses` or `pydantic` for schemas |
| Data engine | pandas | Layer 1, verifier, metrics |
| LLM | Google Gemini API, `gemini-3.5-flash-lite`, function calling | investigator only; temperature 0; modest output cap. SDK is `google-genai` (`from google import genai`) — NOT the legacy `google-generativeai`. Model chosen on quota, see §7.4 |
| Persistence | SQLite (stdlib `sqlite3`) | audit log, cached API responses, run results |
| UI | Streamlit | 5 screens, sidebar nav |
| Charts | Plotly | stacked tier bar, taxonomy chart |
| Money math | `decimal.Decimal` ONLY | never float for currency — this is non-negotiable |
| Config | `.env` via `python-dotenv` | `GEMINI_API_KEY`, `RZP_KEY_ID`, `RZP_KEY_SECRET`, `DEMO_MODE` |
| Tests | pytest | see §11 |

Razorpay test-mode API is optional garnish: if keys are present, pull real test payments and merge with synthetic data; if not, run fully on synthetic data. **Everything must work with `DEMO_MODE=1` and zero network access** (see §10).

**Provider note.** Layer 2 runs on the Gemini free tier. Measured quotas on this account (2026-08-24) make request count — not tokens, not money — the binding constraint on the whole design; see §7.4. Two consequences, both non-negotiable:

- All Layer 2 access goes through `closo/llm_client.py`, a thin provider seam (`LLMClient` protocol + `GeminiClient` + `MockLLMClient`). No module outside that file imports `google.genai`. This is what keeps the mocked test suite free and a provider swap contained to one file.
- Every LLM response is cached in SQLite keyed by exception content, so re-runs, tests, and the demo cost zero requests. Only a first live run spends quota.

---

## 4. Repository layout

```
closo/
├── CLAUDE.md                  # this file
├── README.md                  # quickstart + demo script
├── SESSION.md                 # decision log, gitignored (§15)
├── requirements.txt
├── .env.example
├── data/
│   └── generated/             # output of the generator (gitignored except one frozen demo set)
├── closo/
│   ├── __init__.py
│   ├── config.py              # env, constants, fee schedules
│   ├── schemas.py             # pydantic models for all records + verdicts
│   ├── generator.py           # synthetic data generator (§5)
│   ├── layer1_matcher.py      # deterministic cascade (§6)
│   ├── llm_client.py          # provider seam: LLMClient protocol, GeminiClient, MockLLMClient
│   ├── layer2_investigator.py # LLM agent + tool definitions (§7)
│   ├── layer3_verifier.py     # independent checker (§8)
│   ├── tools.py               # tool implementations the LLM calls (query local SQLite/CSV, cached RZP API)
│   ├── audit.py               # SQLite audit log writer/reader (§9)
│   ├── metrics.py             # scorecard computation (§9)
│   ├── pipeline.py            # orchestrator: run(batch) → RunResult
│   └── rzp_client.py          # thin Razorpay test-mode client with SQLite response cache
├── app/
│   └── streamlit_app.py       # UI (§10) — may split into pages/ if it grows
└── tests/
    ├── test_generator.py
    ├── test_layer1.py
    ├── test_verifier.py
    ├── test_pipeline_e2e.py
    └── fixtures/
```

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

## 6. Layer 1 — deterministic matcher

A cascade of passes over the unmatched pool; each pass only consumes records the previous passes left. Pure pandas + Decimal. No LLM imports allowed in this module (enforce with a test).

1. **Pass A — UTR exact:** normalize UTRs (strip narration junk via regex library in `config.py`), join bank credit ↔ settlement on UTR, assert `credit_amount == amount_settled` exactly.
2. **Pass B — amount + date window:** for UTR-less residue, match on exact amount within `value_date ∈ [settled_at, settled_at + 3 business days]`. Reject if two candidates tie (ambiguity → exception, never guess).
3. **Pass C — netting recomputation:** recompute expected settlement from constituent payments using the fee schedule active at `settled_at`; match with tolerance `abs(diff) <= ₹2.00` (covers E7). Log the tolerance used on every match.

Output per match: `MatchRecord {bank_txn_id, settlement_id, payment_ids, pass_used, tolerance_applied}`. Everything else → exception queue. Target: ≥ 80% auto-matched on the frozen demo set (assert in tests).

---

## 7. Layer 2 — exception investigator

One LLM conversation per exception (not one giant conversation — isolation keeps context clean and cost bounded). System prompt lives in `layer2_investigator.py` as a module constant.

### 7.1 Tools the model may call (implement in `tools.py`)
- `get_payment(payment_id)` / `list_payments(amount_min, amount_max, date_from, date_to)` — reads local data or cached RZP API
- `get_refunds(payment_id)`
- `get_settlement(settlement_id)` / `list_settlements(...)`
- `compute_expected_settlement(payment_ids, fee_schedule)` — returns the arithmetic breakdown; **the tool does the math, never the model**
- `check_duplicate_utr(utr)`
- `get_fee_schedules()` — returns v1/v2 with cutover date

All tools are read-only. Tools return JSON with `Decimal` serialized as strings.

### 7.2 Required verdict schema (force via the `submit_verdict` function; reject free text)

Forced with Gemini `tool_choice`: `{"allowed_tools": {"mode": "any", "tools": ["submit_verdict"]}}`. A final turn that returns prose instead of a `submit_verdict` call is a malformed verdict — retry once with a corrective message, then `unresolvable` (§12.5).
```json
{
  "exception_id": "EX-017",
  "hypothesis": "Settlement of order #1042 net of fee schedule v2",
  "hypotheses_rejected": [{"hypothesis": "partial refund", "reason": "no refunds on payment"}],
  "proposed_match": {"bank_txn_id": "...", "payment_ids": ["..."], "fee_schedule": "v2",
                      "arithmetic": {"gross": "5000.00", "mdr": "-236.00", "gst": "-42.48", "rounding": "-1.52", "net": "4720.00"}},
  "confidence": "resolved | probable | unresolvable",
  "evidence": [{"tool": "get_refunds", "args": {...}, "result_summary": "..."}]
}
```
Rules baked into the system prompt: max 8 tool calls per exception; `unresolvable` is an acceptable and expected answer; never invent record IDs; every number in `arithmetic` must come from a tool result; if two hypotheses fit, return `probable`, not `resolved`.

### 7.3 Cost/latency controls
- `temperature=0`, cap tool loops at 8, hard timeout 30s per exception, then auto-`unresolvable`.
- Record tokens used per exception in the audit log → surfaces as cost-per-record in the scorecard.
- Cache every LLM response in SQLite keyed by exception content. Free-tier quota is finite; re-runs, tests and the demo must cost zero requests.
- Retry once on 429/503 with backoff, then `unresolvable`. Rate-limit exhaustion must degrade one exception, never kill the batch.
- Process exceptions sequentially in demo mode (streaming UI narration), `ThreadPoolExecutor(4)` otherwise.

### 7.4 Model choice and the quota constraint

Free-tier quotas measured in AI Studio on 2026-08-24:

| Model | RPM | TPM | RPD | Full runs/day |
|---|---|---|---|---|
| `gemini-3.7-flash` | 5 | 250K | 20 | 0.2 — cannot finish one run |
| `gemini-3.6-flash` | 5 | 250K | 20 | 0.2 — cannot finish one run |
| `gemini-3.5-flash` | 5 | 250K | 20 | 0.2 — cannot finish one run |
| **`gemini-3.5-flash-lite`** | **15** | **250K** | **500** | **~5** |

A run is ~20 exceptions × ~5 requests each ≈ 100 requests. Every full-Flash model is therefore disqualified: 20 RPD cannot complete four exceptions, and 5 RPM would add ~20 minutes of pure throttling to a throughput number that is part of the judging bar. **`gemini-3.5-flash-lite` is the primary model.** TPM is never the constraint — optimize for fewer requests, never for fewer tokens.

Flash-lite reasons less well than full Flash, and that is acceptable *by design*: the model never does arithmetic (§7.1), and every verdict is recomputed from raw records (§8). A weak proposal does not become a wrong answer — it becomes an `ESCALATED` row labelled "agent proposed, verifier rejected", which §10 already treats as the strongest thing on the screen. A cheap model behind a strict verifier demonstrates the thesis better than an expensive one.

**Budget guard (required).** The investigator tracks requests against a configured RPD ceiling. On exhaustion it stops cleanly and marks every remaining exception `unresolvable — quota exhausted`, recording an audit event. A quota wall must degrade the batch honestly, never crash it or silently truncate the scorecard.

**Optional Stage 6 escalation.** The unused 20 RPD on `gemini-3.7-flash` is roughly the right size to retry only those exceptions flash-lite returns `unresolvable` on, under a hard 20/day budget. Build only if Stage 6 shows flash-lite failing E4/E5; it is not required for the definition of done.

---

## 8. Layer 3 — independent verifier

Pure functions, no LLM imports (enforced by test). For every `resolved`/`probable` verdict:

1. **Existence:** every cited `payment_id`/`settlement_id`/`bank_txn_id` exists in the actual data. Any phantom ID → FAIL.
2. **Arithmetic:** recompute `gross − mdr − gst ± rounding` from raw records with Decimal; must equal bank `credit_amount` exactly. The model's own arithmetic block is treated as a claim, not evidence.
3. **Fee schedule validity:** the cited schedule must be the one active at `settled_at`.
4. **Exclusivity:** cited payments not already consumed by another match (no double-spending a payment across two resolutions).
5. **Refund consistency:** if hypothesis mentions refunds, refund records must exist and net correctly.

PASS → status `AGENT_RESOLVED_VERIFIED`. FAIL → status `ESCALATED` with `verifier_rejection_reason`, and the failed verdict is preserved in the audit log (rejections are demo gold, not embarrassments). `probable` that passes verification still lands in a "needs human sign-off" sub-list on the escalation screen — verified math, unverified intent.

---

## 9. Audit log and metrics

### 9.1 SQLite tables (`audit.py`)
- `runs(run_id, seed, started_at, finished_at, records_total, config_json)`
- `events(event_id, run_id, ts, layer, record_ref, event_type, payload_json)` — every pass decision, every tool call, every verdict, every verifier check. Append-only. The UI's replay mode reads this table.
- `resolutions(run_id, bank_txn_id, final_status, pass_or_verdict_json, verifier_result_json)`
- `api_cache(cache_key, response_json, fetched_at)` — RZP responses.

### 9.2 Scorecard (`metrics.py`) — the only module allowed to open `ground_truth.json`
- **Match rate:** (auto + verified) / total bank credits
- **Verified accuracy:** of all non-escalated resolutions, % whose matched source IDs equal ground truth. This is the headline number — report it even if < 100%.
- **Escalation correctness:** did we escalate exactly E9+E10 (+ legit verifier rejections)? Report false-escalations and false-resolutions separately.
- **Money view:** ₹ reconciled vs ₹ stuck.
- **Throughput:** records/min overall; Layer 1 records/min separately (it will be huge — show it).
- **Cost:** total tokens → ₹ per record.
- **Exception taxonomy:** count by error class, resolved vs escalated per class.

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
- No network calls at all; RZP client serves from `api_cache` / local CSVs.
- Fixed seed 42; every number identical run-to-run (assert in e2e test).
- A completed run can be **replayed** from the `events` table with realistic pacing — if wifi or the LLM API dies on stage, replay of the last good run is indistinguishable from live. Wire a small "Replay last run" button on Ingest.

---

## 11. Best practices and invariants (enforce, don't just intend)

1. **Decimal everywhere for money.** A pytest grep-test fails the build if `float(` touches any amount field in `closo/`.
2. **The LLM never does arithmetic.** All math flows through `compute_expected_settlement` and the verifier. If a model-emitted number isn't reproducible from tool outputs, verification fails.
3. **No LLM imports in `layer1_matcher.py`, `layer3_verifier.py`, `metrics.py`.** Test asserts `google.genai` and `closo.llm_client` are absent from `sys.modules` after importing each. Only `closo/llm_client.py` may import the SDK.
4. **Ground truth is quarantined.** Only `metrics.py` may read `ground_truth.json`; a test monkeypatches `open` during a pipeline run to prove the pipeline never touches it.
5. **Determinism:** same seed + DEMO_MODE → byte-identical scorecard. e2e test runs the pipeline twice and diffs.
6. **Ambiguity → exception, never a guess.** Tie-breaks are forbidden in Layer 1.
7. **Append-only audit log.** No UPDATE/DELETE on `events`.
8. **Escalation is success, not failure.** E9/E10 must land in the escalation queue; test asserts it.
9. **Every module ≤ ~300 lines;** split before it grows. Type hints + docstrings on all public functions.
10. **Commit the frozen demo dataset** so a fresh clone demos in under 2 minutes: `pip install -r requirements.txt && streamlit run app/streamlit_app.py`.

## 12. Test plan — per module, edge cases mandatory

Philosophy: the happy path proves nothing in reconciliation; **every test file must contain more edge-case tests than happy-path tests.** Layer 1 and Layer 3 are tested with real assertions against ground truth. Layer 2 is tested with a **mocked LLM client** (canned tool-call sequences and verdicts) so tests are free, fast, and deterministic — never call the real API in CI.

### 12.1 `test_generator.py`
Happy path: seed 42 produces 150 payments, ~40 settlements, three CSVs + `ground_truth.json`.
Edge cases — must all pass:
- **Determinism:** two runs with seed 42 → byte-identical files (hash comparison). Different seed → different files.
- **Taxonomy exactness:** count per error class E1–E10 matches §5.2 exactly; every bank txn appears in ground truth exactly once; every ground-truth entry references existing records.
- **Money invariants:** every amount is `Decimal` quantized to 2dp; per settlement, `net = Σgross − Σmdr − Σgst ± rounding` with `|rounding| ≤ ₹2.00`; GST is exactly 18% of MDR (quantized) for every fee row.
- **Split settlements (E5):** the two bank credits sum exactly to the settlement net; neither leg equals any other settlement's net (would create accidental ambiguity that breaks Layer 1 tests).
- **Duplicate UTR (E6):** the UTR appears exactly twice in bank_stmt with different credit amounts.
- **Missing UTR (E8):** narration contains no extractable UTR under the §6 regex library (test the regexes against these narrations directly).
- **Designed unresolvables:** E9 settlements exist on the Razorpay side with NO bank credit; E10 bank credits match no settlement net under any fee schedule (assert by brute-force check — this guards against the generator accidentally making E10 resolvable).
- **Fee-schedule cutover:** at least one settlement lands exactly ON the cutover date (belongs to v2); one the day before (v1).
- **No accidental collisions:** no two distinct settlements share (amount, ±3-day window) unless seeded as ambiguous — otherwise Pass B tie-break tests become flaky.

### 12.2 `test_layer1.py`
Happy path: ≥80% auto-match on frozen demo set; **zero false matches** vs ground truth.
Edge cases:
- **UTR normalization:** junk prefixes (`NEFT-RAZORPAY...-`), lowercase, embedded spaces all normalize correctly; a **truncated** UTR must NOT match in Pass A (prefix-matching is forbidden — it must fall through).
- **Pass A strictness:** matching UTR but `credit_amount ≠ amount_settled` by even ₹0.01 → no match in Pass A (falls to C or exception).
- **Date window boundaries:** value_date exactly at `settled_at + 3 business days` → match; +4 → exception. Window arithmetic skips weekends (craft a Friday settlement fixture).
- **Tolerance boundary:** diff of exactly ₹2.00 → Pass C match with tolerance logged; ₹2.01 → exception.
- **Ambiguity:** two settlements, same amount, overlapping windows → BOTH sides go to exceptions, and the audit log records `ambiguous_tie` as the reason. Guessing here is the bug this test exists to catch.
- **Exclusivity:** a settlement consumed by Pass A is invisible to Passes B/C (feed a crafted duplicate-amount fixture).
- **Duplicate UTR (E6):** both bank rows go to exceptions — Pass A must refuse when a UTR join yields 2 bank candidates.
- **Empty/degenerate input:** empty bank statement → 0 matches, 0 exceptions raised as errors, clean run. One-row inputs work.
- **No-LLM invariant:** importing `layer1_matcher` pulls in neither `google.genai` nor `closo.llm_client` (inspect `sys.modules`).

### 12.3 `test_tools.py`
- Unknown ID → structured `{"error": "not_found"}`, never a raised exception (the LLM must be able to recover).
- All amounts serialized as strings; round-trip through `Decimal` is lossless.
- `list_payments` boundary filters: `amount_min == amount_max == exact amount` returns the record; date range boundaries inclusive.
- `compute_expected_settlement` with an empty payment list → structured error. With mixed fee schedules → error (one schedule per call).
- Read-only guarantee: tools module has no write access to the data stores (assert no INSERT/UPDATE strings; audit writes go through `audit.py` only).

### 12.4 `test_verifier.py` (the most important test file in the repo)
Craft verdicts by hand — do not generate them with an LLM. Each rejection case is one test:
- **Phantom ID:** cited `payment_id` doesn't exist → FAIL with reason `phantom_reference`.
- **Off-by-a-paisa:** arithmetic block sums to bank credit but recomputation from raw records differs by ₹0.01 → FAIL. (This is the test that proves the model's arithmetic is a claim, not evidence.)
- **Wrong fee schedule:** verdict cites v1 for a settlement dated ON the cutover → FAIL; day-before-cutover with v1 → PASS.
- **Double-spend:** payment already consumed by an earlier verified resolution, cited again → FAIL with `exclusivity_violation`.
- **Refund fabrication:** hypothesis mentions a partial refund; no refund record exists → FAIL.
- **Internally consistent but wrong:** arithmetic block is self-consistent AND matches the credit, but the cited payments' real gross differs from the block's gross → FAIL (verifier must recompute from raw records, never trust the block).
- **Split-settlement pass:** valid E5 verdict citing both bank legs → PASS.
- **Probable path:** a `probable` verdict that passes math checks → lands in sign-off sub-list, not in `AGENT_RESOLVED_VERIFIED`.
- **No-LLM invariant** as in 12.2.

### 12.5 `test_investigator.py` (mocked LLM client)
Mocks `MockLLMClient` against the `LLMClient` protocol — never the Gemini SDK. No test in CI touches the network.
- **Tool-loop cap:** mock a model that never stops calling tools → hard stop at 8 calls, auto-`unresolvable`, audit event `tool_budget_exhausted`.
- **Timeout:** mock a 31s tool call → `unresolvable`, run continues to next exception (one bad exception must never kill the batch).
- **Malformed verdict:** mock free-text final message (no `submit_verdict` call) → one retry with corrective message; second failure → `unresolvable`.
- **Verdict schema violations:** missing `arithmetic`, unknown confidence value, empty evidence list → rejected before reaching the verifier.
- **API error resilience:** mock a 529/overloaded on one exception → retried once, then `unresolvable`; other exceptions unaffected.
- **Sequential/parallel parity:** same mocked responses in sequential vs ThreadPool mode → identical resolutions.

### 12.6 `test_pipeline_e2e.py` (frozen demo set, mocked LLM in CI, real LLM behind an opt-in flag)
- **Determinism:** run twice with seed 42 + DEMO_MODE → identical `resolutions` table and scorecard JSON (deep diff).
- **Terminal-state totality:** every bank txn ends in exactly one of the three states; states sum to total.
- **Escalation correctness:** escalated set == {E9, E10 records} ∪ {verifier rejections}; with the well-behaved mock, exactly E9+E10.
- **Resolution correctness:** every `AGENT_RESOLVED_VERIFIED` matches ground truth's `source_payment_ids` (via metrics harness).
- **Ground-truth quarantine:** monkeypatch `open`/`Path.read_text` to raise if `ground_truth.json` is touched during pipeline execution; only `metrics.py` post-run may read it.
- **Audit append-only:** attempt UPDATE/DELETE on `events` → raises (SQLite trigger).
- **Scorecard cross-checks:** ₹ reconciled + ₹ stuck == Σ bank credits; taxonomy counts sum to 150-payment coverage; cost-per-record present when the mocked client reports token counts.
- **Money grep-invariant:** repo-wide scan — no `float(` applied to any field named `amount|fee|gst|credit|net` within `closo/`.

### 12.7 UI smoke (manual checklist, scripted where possible)
`streamlit run` boots with no network; Replay-last-run works airplane-mode; all three colors appear on Scorecard; drill-down renders a full evidence trail for one E4 record; escalation screen shows a verifier-rejected verdict when one is present in the run.

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

**Stage 7 — Full pipeline hardening (2h).**
Steps: wire Layer 2+3 into `pipeline.run()`, ground-truth quarantine, scorecard cross-checks, cost metrics, cache the real-API run's responses into `api_cache` for offline replay.
Exit: entire §12.6 green; airplane-mode run of the full pipeline succeeds.

**Stage 8 — Full UI (3h).**
Steps: Live-run streaming with `st.status` + delayed verifier stamp, drill-down, escalation queue with rejected-hypotheses strikethrough, Replay-last-run button, global color code.
Exit: §12.7 checklist passes end-to-end in airplane mode.

**Stage 9 — Demo polish (1.5h).**
Steps: README quickstart + 4-minute demo script, pick the ONE drill-down exception to show on stage (an E4 fee-schedule case tells the best story), rehearse twice with a timer, verify fresh-clone-to-demo < 2 minutes.
Exit: two consecutive rehearsals inside 4 minutes with zero live typing beyond clicks.

**Stage 10 — Optional flourish (only if 0–9 done).** Settlement Q&A box grounded on the resolutions table; token/₹ cost line on Scorecard if not already done.

Cut policy under time pressure: drop Stage 10, then compress Stage 8's escalation screen to a table, then drop E5/E6 from the demo narrative (keep them in data). NEVER cut the verifier, the mocked test suite, or replay mode — they are the pitch.

## 14. Definition of done

- Fresh clone, `DEMO_MODE=1`, no API keys → full demo works offline.
- Scorecard on seed-42: match rate ≥ 93%, verified accuracy reported against ground truth, escalation list contains exactly the designed-unresolvable classes (+ any verifier rejections, shown as such).
- One exception drill-down tells a complete story: hypothesis → evidence → arithmetic → independent verification.
- All tests green; invariant tests (§11) present and passing.

---

## 15. Working practice

### 15.1 SESSION.md — the decision log
`SESSION.md` lives at the repo root and is **gitignored** — it is a working log for whoever is building, not a deliverable. It starts empty and is filled **at the end of each session**, never written ahead of time. One section per stage, appended chronologically, newest last:

```markdown
## Stage N — <name> — <YYYY-MM-DD>

**Done:** what shipped, in plain terms.
**Decisions:** each decision and *why* — including the options rejected and the reason they lost.
**Surprises:** anything that did not go the way CLAUDE.md predicted.
**Open:** what the next session picks up.
```

The point is the *why*. CLAUDE.md says what to build and git history says what changed; SESSION.md is the only place that records the reasoning, so a session picking up cold does not re-litigate a settled decision.

### 15.2 Commit cadence
- **One commit per substep.** A substep is one bounded, self-contained change — not a whole stage.
- **Push once per stage**, after that stage's exit criteria in §13 pass.
- **Never push a stage whose tests are red.** A red stage is an unfinished stage.

### 15.3 Commit style
- **Title: one line.** Imperative mood, no trailing period, no `type(scope):` prefix.
- **Description: humanized.** Write it the way you would explain the change to a teammate — what changed, why it mattered, what you decided against and why. Full sentences and paragraphs, not a bulleted list of filenames. The diff already says which files moved; the message should say what was going on in your head.

Example:

```
Add the synthetic data generator

Everything Closo claims about its own accuracy is measured against
seeded ground truth, so this had to land before the matcher rather
than alongside it.

The fiddly part was E10 — the bank credits that must match no
settlement under any fee schedule. It is easy to accidentally
generate one that Layer 1 can solve, which would quietly destroy the
honest-exception-list story with no visible symptom, so there is a
brute-force check that proves each E10 is genuinely unresolvable.
```
