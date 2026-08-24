"""Reading and writing the generated dataset.

Kept apart from the generator so Stage 3's matcher can load a batch without
importing the thing that produced it - and so nothing in the load path can
accidentally reach ``ground_truth.json``, which only ``metrics.py`` may
open, and only after a run completes (11.4).

:func:`load_batch` deliberately returns no ground truth. There is no
parameter to ask for it.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from closo.schemas import BankTxn, Order, Payment, Settlement
from closo.taxonomy import GeneratedBatch

PAYMENT_COLUMNS = [
    "payment_id", "order_id", "amount_gross", "method", "captured_at",
    "settlement_id", "settlement_utr", "settled_at", "fee_mdr", "fee_gst",
    "amount_settled", "status", "refund_amount",
]
BANK_COLUMNS = [
    "bank_txn_id", "txn_date", "value_date", "narration", "utr",
    "credit_amount", "balance",
]
ORDER_COLUMNS = [
    "order_id", "sku", "order_amount", "order_date", "channel",
    "expected_settlement",
]
SETTLEMENT_COLUMNS = [
    "settlement_id", "utr", "settled_at", "payment_ids", "amount_gross",
    "fee_mdr", "fee_gst", "amount_settled", "fee_schedule", "rounding",
]

GROUND_TRUTH_FILENAME = "ground_truth.json"

#: Payment IDs are joined with a pipe inside one CSV cell. Commas would
#: collide with the delimiter and quoting makes the diffs harder to read.
LIST_SEPARATOR = "|"


def _cell(value: object) -> str:
    """Render one CSV cell. Amounts stay strings; None becomes empty."""
    if value is None:
        return ""
    if isinstance(value, list):
        return LIST_SEPARATOR.join(str(v) for v in value)
    return str(value)


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    """Write a CSV with LF endings.

    The line terminator is pinned because the determinism test compares file
    hashes, and Python's default on Windows would emit CRLF - making the
    same seed produce different bytes on different machines.
    """
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _cell(row.get(c)) for c in columns})


def write_batch(batch: GeneratedBatch, out_dir: Path, seed: int) -> None:
    """Write the source CSVs and ``ground_truth.json`` into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)

    write_csv(out_dir / "payments.csv", PAYMENT_COLUMNS,
              [p.model_dump(mode="json") for p in batch.payments])
    write_csv(out_dir / "bank_stmt.csv", BANK_COLUMNS,
              [b.model_dump(mode="json") for b in batch.bank_txns])
    write_csv(out_dir / "order_ledger.csv", ORDER_COLUMNS,
              [o.model_dump(mode="json") for o in batch.orders])
    write_csv(out_dir / "settlements.csv", SETTLEMENT_COLUMNS,
              [s.model_dump(mode="json") for s in batch.settlements])

    payload = {
        "seed": seed,
        "bank_txns": batch.ground_truth,
        "missing_settlements": batch.missing_settlements,
        "counts": batch.class_counts(),
    }
    with (out_dir / GROUND_TRUTH_FILENAME).open(
        "w", newline="\n", encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _blank_to_none(row: dict[str, str]) -> dict[str, object]:
    """Empty CSV cells mean absent, not empty string.

    An unsettled payment must read back as None rather than "", or E9 would
    look like a settlement of zero rupees instead of no settlement at all.
    """
    return {k: (v if v != "" else None) for k, v in row.items()}


def _read_rows(path: Path) -> list[dict[str, object]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [_blank_to_none(row) for row in csv.DictReader(handle)]


def load_batch(data_dir: Path) -> GeneratedBatch:
    """Load a generated dataset. Never reads ground truth.

    There is no flag to include it. The quarantine in 11.4 is enforced by
    a test that fails if the pipeline touches ``ground_truth.json``, and the
    cheapest way to pass that test is to give the load path no way to.
    """
    batch = GeneratedBatch()
    batch.payments = [Payment(**row) for row in _read_rows(data_dir / "payments.csv")]
    batch.bank_txns = [BankTxn(**row) for row in _read_rows(data_dir / "bank_stmt.csv")]
    batch.orders = [Order(**row) for row in _read_rows(data_dir / "order_ledger.csv")]

    settlements_path = data_dir / "settlements.csv"
    if settlements_path.exists():
        rows = _read_rows(settlements_path)
        for row in rows:
            raw = row.get("payment_ids") or ""
            row["payment_ids"] = str(raw).split(LIST_SEPARATOR) if raw else []
        batch.settlements = [Settlement(**row) for row in rows]

    return batch


def load_ground_truth(data_dir: Path) -> dict:
    """Read ground truth. **Only ``metrics.py`` may call this** (11.4)."""
    with (data_dir / GROUND_TRUTH_FILENAME).open(encoding="utf-8") as handle:
        return json.load(handle)
