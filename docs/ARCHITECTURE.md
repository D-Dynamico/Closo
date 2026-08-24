# Closo — Architecture

The static structure: the thesis, the three layers and their contracts, the module
map, the persistence schema, and the invariants that must hold no matter what changes.

Section numbers are load-bearing: docstrings across `closo/` cite them, and
`docs/SYSTEM_DESIGN.md` maps every number to the file it lives in. Do not renumber.

---

## 1. What we are building and why


**One sentence:** Closo ingests three financial data sources (Razorpay payments/settlements, a bank statement, an internal order ledger), reconciles them across a 150-record batch, resolves exceptions with an LLM investigator whose every verdict is independently re-verified by deterministic math, and reports honest metrics including the exceptions it could NOT resolve.

**The judging bar (from the track statement):** throughput + measured accuracy + an honest exception list, across a 50+ record batch. "One cherry-picked match proves nothing."

**Core design thesis (say this everywhere, build this everywhere):** verification capacity, not generation speed, is the bottleneck in finance ops. Therefore **the agent can never mark anything resolved unless a separate deterministic verifier confirms the math independently.** The LLM proposes; the verifier disposes.

**Hard scope boundary — do NOT build:** cash forecasting, tax-line matching beyond GST-on-fees, multi-currency, auth/user accounts, settings pages, real Razorpay production API usage. One loop, closed completely.

---

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

---

## 4. Repository layout


```
closo/
├── CLAUDE.md                  # this file
├── README.md                  # quickstart + demo script
├── docs/
│   ├── SYSTEM_DESIGN.md       # this file
│   └── sessions/              # dated session notes — the project's memory
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

---

## 6. Layer 1 — deterministic matcher


A cascade of passes over the unmatched pool; each pass only consumes records the previous passes left. Pure pandas + Decimal. No LLM imports allowed in this module (enforce with a test).

1. **Pass A — UTR exact:** normalize UTRs (strip narration junk via regex library in `config.py`), join bank credit ↔ settlement on UTR, assert `credit_amount == amount_settled` exactly.
2. **Pass B — amount + date window:** for UTR-less residue, match on exact amount within `value_date ∈ [settled_at, settled_at + 3 business days]`. Reject if two candidates tie (ambiguity → exception, never guess).
3. **Pass C — netting recomputation:** recompute expected settlement from constituent payments using the fee schedule active at `settled_at`; match with tolerance `abs(diff) <= ₹2.00` (covers E7). Log the tolerance used on every match.

Output per match: `MatchRecord {bank_txn_id, settlement_id, payment_ids, pass_used, tolerance_applied}`. Everything else → exception queue. Target: ≥ 80% auto-matched on the frozen demo set (assert in tests).

---

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

---

## 8. Layer 3 — independent verifier


Pure functions, no LLM imports (enforced by test). For every `resolved`/`probable` verdict:

1. **Existence:** every cited `payment_id`/`settlement_id`/`bank_txn_id` exists in the actual data. Any phantom ID → FAIL.
2. **Arithmetic:** recompute `gross − mdr − gst ± rounding` from raw records with Decimal; must equal bank `credit_amount` exactly. The model's own arithmetic block is treated as a claim, not evidence.
3. **Fee schedule validity:** the cited schedule must be the one active at `settled_at`. If it is not, the verdict is **capped at `probable`** rather than failed outright — see below.
4. **Exclusivity:** cited payments not already consumed by another match (no double-spending a payment across two resolutions).
5. **Refund consistency:** if hypothesis mentions refunds, refund records must exist and net correctly.

PASS → status `AGENT_RESOLVED_VERIFIED`. FAIL → status `ESCALATED` with `verifier_rejection_reason`, and the failed verdict is preserved in the audit log (rejections are demo gold, not embarrassments). `probable` that passes verification still lands in a "needs human sign-off" sub-list on the escalation screen — verified math, unverified intent.

### 8.1 The fee-schedule anomaly, and why check 3 caps rather than fails

Check 3 as originally written contradicted §5.2. Error class E4 **is** the case where a
payout ran on a superseded schedule, so the only correct verdict necessarily cites the
inactive schedule — and a check that fails any such verdict would make E4 unresolvable by
construction, while §5.2 lists it as a Layer 2 class.

The resolution uses machinery §8 already describes. When a verdict cites a schedule that
was not active at `settled_at`:

- The arithmetic check still runs **in full** and must reproduce the bank credit exactly
  under the cited schedule. Nothing is relaxed about the math.
- The verdict is capped at `probable` and flagged `needs_human_signoff`.
- `schedule_anomaly` is recorded on the result so the escalation screen can say *which*
  schedule was applied and which should have been.

That is precisely the "verified math, unverified intent" case: Closo can prove ₹X moved
and prove which schedule produces ₹X, but it cannot know whether applying that schedule
was authorised or a billing error. A machine should not silently decide that. It should
show its work and hand a human the specific question.

A verdict citing a schedule that reproduces **nothing** still fails outright — the cap
applies to intent, never to arithmetic.

---

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
