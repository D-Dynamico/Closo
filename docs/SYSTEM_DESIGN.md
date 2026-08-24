# Closo — System Design Index

The specification is split across three documents by concern. This page exists so a
section number resolves to a file.

**Section numbers are load-bearing.** Docstrings throughout `closo/` cite them — a comment
reading `(12.2)` means section 12.2, which lives in `TEST_PLAN.md`. Numbers were kept
stable through the split precisely so those references survive. Do not renumber; a test in
`tests/test_scaffold.py` checks that every number cited from `closo/` still resolves.

## Where each section lives

| § | Topic | Document |
|---|---|---|
| 1 | What we are building and why — the thesis | [ARCHITECTURE](ARCHITECTURE.md) |
| 2 | Layer diagram and the three terminal states | [ARCHITECTURE](ARCHITECTURE.md) |
| 3 | Tech stack | [ARCHITECTURE](ARCHITECTURE.md) |
| 4 | Repository layout | [ARCHITECTURE](ARCHITECTURE.md) |
| 5 | Synthetic data generator and the error taxonomy | [WORKFLOWS](WORKFLOWS.md) |
| 6 | Layer 1 — deterministic matcher | [ARCHITECTURE](ARCHITECTURE.md) |
| 7 | Layer 2 — exception investigator (incl. §7.4 quotas) | [ARCHITECTURE](ARCHITECTURE.md) |
| 8 | Layer 3 — independent verifier | [ARCHITECTURE](ARCHITECTURE.md) |
| 9 | Audit log and metrics | [ARCHITECTURE](ARCHITECTURE.md) |
| 10 | Streamlit UI and demo mode | [WORKFLOWS](WORKFLOWS.md) |
| 11 | Invariants — enforce, don't just intend | [ARCHITECTURE](ARCHITECTURE.md) |
| 12 | Test plan, per module | [TEST_PLAN](TEST_PLAN.md) |
| 13 | Stages with exit criteria | [WORKFLOWS](WORKFLOWS.md) |
| 14 | Definition of done | [WORKFLOWS](WORKFLOWS.md) |

## Reading order

New to the project: [`../CLAUDE.md`](../CLAUDE.md) → [ARCHITECTURE](ARCHITECTURE.md) §1–§2
→ the newest note in [`sessions/`](sessions/).

Picking up mid-build: the newest [`sessions/`](sessions/) note first — it carries what is
still open and what nearly broke — then [WORKFLOWS](WORKFLOWS.md) §13 for the current stage.

Changing a layer: its section in [ARCHITECTURE](ARCHITECTURE.md), then §11 invariants, then
the matching part of [TEST_PLAN](TEST_PLAN.md) before writing code.
