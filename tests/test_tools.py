"""Tests for the investigator's tools (TEST_PLAN 12.3).

The tools are the model's only window onto the data, so their failure
modes are the model's failure modes. A tool that raises kills an
investigation over a typo; a tool that returns a float hands the model a
number the verifier will later refuse to reproduce.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from closo.config import DEMO_DIR, FEE_CUTOVER_DATE, money
from closo.dataset_io import load_batch
from closo.tools import MAX_RESULTS, ToolBox

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def batch():
    return load_batch(DEMO_DIR)


@pytest.fixture(scope="module")
def tools(batch):
    return ToolBox(batch)


@pytest.fixture(scope="module")
def a_settlement(batch):
    """A settlement with more than one payment, for the interesting cases."""
    return next(s for s in batch.settlements if len(s.payment_ids) > 1)


# --------------------------------------------------------------------------
# Errors are returned, never raised
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda t: t.get_payment("pay_nope"),
        lambda t: t.get_settlement("setl_nope"),
        lambda t: t.get_bank_txn("bt_nope"),
        lambda t: t.get_refunds("pay_nope"),
    ],
)
def test_unknown_id_returns_not_found(tools, call) -> None:
    """The model must be able to recover from a wrong guess. An exception
    would end the investigation over a typo."""
    result = call(tools)
    assert result["error"] == "not_found"
    assert result["detail"]


def test_no_tool_raises_on_garbage_input(tools) -> None:
    """Every argument here arrives from a language model, so none of it can
    be assumed well-formed."""
    assert tools.list_payments(amount_min="not-a-number")["error"] == "bad_argument"
    assert tools.list_payments(date_from="last tuesday")["error"] == "bad_argument"
    assert tools.list_settlements(date_to="")["error"] == "bad_argument"
    assert tools.compute_expected_settlement(["x"], "v1")["error"] == "not_found"


def test_float_arguments_are_refused(tools) -> None:
    """A float would silently reintroduce the rounding error the whole
    codebase avoids, in the one place the model then reasons from."""
    result = tools.list_payments(amount_min=1000.5)
    assert result["error"] == "bad_argument"
    assert "float" in result["detail"]


# --------------------------------------------------------------------------
# Amounts are strings, and round-trip losslessly
# --------------------------------------------------------------------------


def test_every_amount_is_a_string(tools, batch) -> None:
    payment = tools.get_payment(batch.payments[0].payment_id)
    for field in ("amount_gross", "refund_amount", "net_of_refund"):
        assert isinstance(payment[field], str)


def test_amounts_round_trip_through_decimal(tools, batch) -> None:
    original = batch.payments[0]
    view = tools.get_payment(original.payment_id)
    assert Decimal(view["amount_gross"]) == original.amount_gross


def test_tool_output_is_json_serializable(tools, a_settlement) -> None:
    """It goes over the wire as a function-call result. A Decimal that
    escaped would fail at the moment of the call, mid-investigation."""
    for payload in (
        tools.get_settlement(a_settlement.settlement_id),
        tools.compute_expected_settlement(a_settlement.payment_ids, "v1"),
        tools.get_fee_schedules(),
        tools.list_payments(amount_min="1000", amount_max="2000"),
    ):
        assert json.loads(json.dumps(payload)) == payload


def test_no_float_appears_anywhere_in_tool_output(tools, a_settlement) -> None:
    def walk(node) -> None:
        assert not isinstance(node, float), f"float in tool output: {node}"
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(tools.compute_expected_settlement(a_settlement.payment_ids, "v2"))
    walk(tools.get_fee_schedules())


# --------------------------------------------------------------------------
# Filter boundaries
# --------------------------------------------------------------------------


def test_exact_amount_lookup_finds_the_record(tools, batch) -> None:
    """min == max is the investigator's most common query. An exclusive
    bound would return nothing for it while looking perfectly sensible."""
    target = batch.payments[0]
    amount = str(target.amount_gross)
    result = tools.list_payments(amount_min=amount, amount_max=amount)
    assert target.payment_id in {p["payment_id"] for p in result["payments"]}


def test_amount_bounds_are_inclusive_on_both_sides(tools, batch) -> None:
    target = batch.payments[0]
    low = str(money(target.amount_gross - Decimal("0.01")))
    high = str(money(target.amount_gross + Decimal("0.01")))
    inside = tools.list_payments(amount_min=low, amount_max=high)
    assert target.payment_id in {p["payment_id"] for p in inside["payments"]}

    just_below = str(money(target.amount_gross - Decimal("0.01")))
    outside = tools.list_payments(amount_min=low, amount_max=just_below)
    assert target.payment_id not in {p["payment_id"] for p in outside["payments"]}


def test_date_bounds_are_inclusive(tools, batch) -> None:
    target = batch.payments[0]
    day = target.captured_at.isoformat()
    result = tools.list_payments(date_from=day, date_to=day)
    assert target.payment_id in {p["payment_id"] for p in result["payments"]}


def test_a_filter_matching_nothing_returns_an_empty_list_not_an_error(tools) -> None:
    """'Nothing matches' is a finding that rules out a hypothesis, not a
    failure, and the model needs to be able to tell them apart."""
    result = tools.list_payments(amount_min="99999999", amount_max="99999999")
    assert result["count"] == 0
    assert result["payments"] == []
    assert "error" not in result


def test_large_result_sets_are_truncated_and_say_so(tools) -> None:
    """Silently truncating would let the model conclude a record does not
    exist because it fell off the end of a list."""
    result = tools.list_payments()
    assert result["count"] == 150
    assert result["truncated"] is True
    assert len(result["payments"]) == MAX_RESULTS


def test_method_filter_narrows_correctly(tools) -> None:
    result = tools.list_payments(method="upi")
    assert result["count"] > 0
    assert all(p["method"] == "upi" for p in result["payments"])


# --------------------------------------------------------------------------
# compute_expected_settlement — where all the arithmetic happens
# --------------------------------------------------------------------------


def test_empty_payment_list_is_a_structured_error(tools) -> None:
    result = tools.compute_expected_settlement([], "v1")
    assert result["error"] == "bad_argument"


def test_unknown_fee_schedule_is_a_structured_error(tools, a_settlement) -> None:
    result = tools.compute_expected_settlement(a_settlement.payment_ids, "v3")
    assert result["error"] == "bad_argument"
    assert "v1" in result["detail"] and "v2" in result["detail"]


def test_one_schedule_per_call(tools, a_settlement) -> None:
    """Mixing schedules inside one computation would hide which rate applied
    to which payment, and that distinction is the whole of error class E4."""
    import inspect

    signature = inspect.signature(tools.compute_expected_settlement)
    assert signature.parameters["fee_schedule"].annotation == "str"


def test_duplicate_payment_ids_are_refused(tools, a_settlement) -> None:
    """Citing a payment twice would double its gross and produce a total
    that reconciles against nothing real."""
    pid = a_settlement.payment_ids[0]
    result = tools.compute_expected_settlement([pid, pid], "v1")
    assert result["error"] == "bad_argument"
    assert "more than once" in result["detail"]


def test_the_breakdown_sums_to_the_totals(tools, a_settlement) -> None:
    """Every figure must be traceable to a row, because the verifier
    recomputes the same way and rejects what it cannot reproduce."""
    result = tools.compute_expected_settlement(a_settlement.payment_ids, "v2")
    mdr = sum(Decimal(row["mdr"]) for row in result["breakdown"])
    gst = sum(Decimal(row["gst"]) for row in result["breakdown"])
    assert money(mdr) == Decimal(result["mdr"])
    assert money(gst) == Decimal(result["gst"])
    assert money(
        Decimal(result["gross"]) - Decimal(result["mdr"]) - Decimal(result["gst"])
    ) == Decimal(result["net"])


def test_the_two_schedules_give_different_answers(tools, a_settlement) -> None:
    """If they agreed, E4 would be undetectable for these payments."""
    first = tools.compute_expected_settlement(a_settlement.payment_ids, "v1")
    second = tools.compute_expected_settlement(a_settlement.payment_ids, "v2")
    assert first["net"] != second["net"]


def test_gross_is_net_of_refunds(tools, batch) -> None:
    """A refunded payment settles for less. Using the original gross would
    make every E3 investigation come out short by the refund."""
    refunded = next(p for p in batch.payments if p.refund_amount > 0)
    result = tools.compute_expected_settlement([refunded.payment_id], "v2")
    assert Decimal(result["gross"]) == refunded.net_of_refund


# --------------------------------------------------------------------------
# Refunds and UTR lookups
# --------------------------------------------------------------------------


def test_no_refunds_is_data_not_an_error(tools, batch) -> None:
    """'No refunds exist' is what rules out a whole hypothesis, so it must
    come back as an answer rather than as a failure."""
    clean = next(p for p in batch.payments if p.refund_amount == 0)
    result = tools.get_refunds(clean.payment_id)
    assert result["count"] == 0
    assert "error" not in result


def test_a_real_refund_is_reported_with_the_net_it_leaves(tools, batch) -> None:
    refunded = next(p for p in batch.payments if p.refund_amount > 0)
    result = tools.get_refunds(refunded.payment_id)
    assert result["count"] == 1
    assert Decimal(result["refunds"][0]["amount"]) == refunded.refund_amount
    assert Decimal(result["refunds"][0]["leaves_net"]) == refunded.net_of_refund


def test_duplicate_utr_is_detected(tools, batch) -> None:
    """E6. Two settlements sharing a UTR is what Pass A refused to guess at."""
    shared = next(
        s.utr for s in batch.settlements
        if sum(1 for other in batch.settlements if other.utr == s.utr) > 1
    )
    result = tools.check_duplicate_utr(shared)
    assert result["is_duplicate"] is True
    assert result["settlement_count"] == 2


def test_a_unique_utr_is_not_flagged(tools, batch) -> None:
    unique = next(
        s.utr for s in batch.settlements
        if sum(1 for other in batch.settlements if other.utr == s.utr) == 1
    )
    assert tools.check_duplicate_utr(unique)["is_duplicate"] is False


def test_utr_lookup_normalizes_its_argument(tools, batch) -> None:
    """The model will paste a UTR out of a narration, junk and all."""
    settlement = batch.settlements[0]
    messy = f"NEFT-RAZORPAYSOFTWARE-{settlement.utr.lower()}-SETTLEMENT"
    assert tools.check_duplicate_utr(messy)["utr"] == settlement.utr


def test_bank_txn_exposes_the_parsed_utr_alongside_the_narration(tools, batch) -> None:
    """For an E8 exception the parsed value is None, and seeing the raw
    narration next to that absence is the entire story."""
    credit = batch.bank_txns[0]
    view = tools.get_bank_txn(credit.bank_txn_id)
    assert view["narration"] == credit.narration
    assert "utr_parsed_from_narration" in view


def test_fee_schedules_include_the_cutover_and_both_versions(tools) -> None:
    result = tools.get_fee_schedules()
    assert result["cutover_date"] == FEE_CUTOVER_DATE.isoformat()
    assert set(result["schedules"]) == {"v1", "v2"}


def test_settlement_view_names_both_recorded_and_active_schedule(
    tools, a_settlement
) -> None:
    """The comparison that makes E4 discoverable at all."""
    view = tools.get_settlement(a_settlement.settlement_id)
    assert "fee_schedule_recorded" in view
    assert "fee_schedule_active_on_that_date" in view


# --------------------------------------------------------------------------
# Read-only guarantee
# --------------------------------------------------------------------------


def test_tools_module_contains_no_write_statements() -> None:
    """Audit writes go through audit.py alone (12.3)."""
    source = (REPO_ROOT / "closo" / "tools.py").read_text(encoding="utf-8")
    for statement in ("INSERT", "UPDATE ", "DELETE", "DROP", "sqlite3", ".write("):
        assert statement not in source, f"tools.py contains {statement!r}"


def test_tools_cannot_mutate_the_batch(tools, batch) -> None:
    """A tool that mutated shared records would corrupt every later
    investigation in the run, and the corruption would look like data."""
    before = [p.amount_gross for p in batch.payments]
    tools.list_payments()
    tools.compute_expected_settlement([batch.payments[0].payment_id], "v1")
    assert [p.amount_gross for p in batch.payments] == before


def test_tools_do_not_read_ground_truth(monkeypatch, tools, a_settlement) -> None:
    """The model must never be handed the answers, directly or otherwise."""
    from closo.dataset_io import GROUND_TRUTH_FILENAME

    real_open = Path.open

    def guarded(self: Path, *args: object, **kwargs: object):
        if self.name == GROUND_TRUTH_FILENAME:
            raise AssertionError("a tool opened ground truth")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    assert tools.compute_expected_settlement(a_settlement.payment_ids, "v1")["net"]
