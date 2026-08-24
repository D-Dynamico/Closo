# Closo — Test Plan

Per-module test requirements. The governing rule: **every test file must contain more
edge-case tests than happy-path tests.** In reconciliation the happy path proves almost
nothing — any implementation multiplies two numbers correctly.

Section numbers are load-bearing: docstrings across `closo/` cite them, and
`docs/SYSTEM_DESIGN.md` maps every number to the file it lives in. Do not renumber.

---

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
