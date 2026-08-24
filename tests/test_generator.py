"""Tests for the synthetic generator (CLAUDE.md 12.1).

Every accuracy number Closo reports is measured against this data, so a
generator bug does not produce a wrong answer - it produces a
confidently wrong *metric*, which is far harder to notice. These tests
exist to make that impossible rather than unlikely.

The batch is built once per session. Generation is deterministic, so
sharing it across tests costs nothing and keeps the suite fast.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from closo.config import (
    FEE_CUTOVER_DATE,
    PASS_C_TOLERANCE,
    active_schedule,
    get_schedule,
    gst_on,
    money,
    normalize_utr,
    previous_business_day,
)
from closo.dataset_io import load_batch, load_ground_truth, write_batch
from closo.generator import generate
from closo.taxonomy import (
    CUTOVER_PAYMENTS,
    E10_COUNT,
    PAYMENT_CLASS_COUNTS,
    TOTAL_PAYMENTS,
    GeneratedBatch,
    settlement_math,
    verify_unresolvable,
)

DEMO_DIR = Path(__file__).resolve().parent.parent / "data" / "generated" / "demo"


@pytest.fixture(scope="session")
def batch() -> GeneratedBatch:
    return generate(42)


@pytest.fixture(scope="session")
def by_id(batch: GeneratedBatch) -> dict:
    return batch.payments_by_id()


def credits_in_class(batch: GeneratedBatch, error_class: str) -> list:
    """Bank credits ground truth assigns to ``error_class``."""
    ids = {
        k for k, v in batch.ground_truth.items() if v["error_class"] == error_class
    }
    return [b for b in batch.bank_txns if b.bank_txn_id in ids]


def settlements_in_class(batch: GeneratedBatch, error_class: str) -> list:
    ids = {
        v["settlement_id"]
        for v in batch.ground_truth.values()
        if v["error_class"] == error_class and v["settlement_id"]
    }
    return [s for s in batch.settlements if s.settlement_id in ids]


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_batch_has_the_specified_volume(batch: GeneratedBatch) -> None:
    assert len(batch.payments) == TOTAL_PAYMENTS
    assert 40 <= len(batch.settlements) <= 50
    assert len(batch.orders) == TOTAL_PAYMENTS


def test_frozen_demo_set_is_present_and_loadable() -> None:
    """Section 11.10: a fresh clone demos without regenerating anything."""
    loaded = load_batch(DEMO_DIR)
    assert len(loaded.payments) == TOTAL_PAYMENTS
    assert len(loaded.bank_txns) > 0


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def _hash_dir(path: Path) -> dict[str, str]:
    return {
        f.name: hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(path.iterdir())
        if f.is_file()
    }


def test_same_seed_produces_identical_files(tmp_path: Path) -> None:
    """The replay demo rests entirely on this."""
    write_batch(generate(42), tmp_path / "a", 42)
    write_batch(generate(42), tmp_path / "b", 42)
    assert _hash_dir(tmp_path / "a") == _hash_dir(tmp_path / "b")


def test_different_seed_produces_different_files(tmp_path: Path) -> None:
    """Guards against a generator that ignores its seed entirely - which
    would pass the determinism test above perfectly."""
    write_batch(generate(42), tmp_path / "a", 42)
    write_batch(generate(99), tmp_path / "c", 99)
    assert _hash_dir(tmp_path / "a") != _hash_dir(tmp_path / "c")


def test_regenerating_matches_the_committed_demo_set(tmp_path: Path) -> None:
    """The frozen set must still be what the current code produces.

    If this fails, either the generator changed without the demo set being
    refreshed, or line endings are being mangled on checkout - both of
    which break a fresh clone rather than this machine.
    """
    write_batch(generate(42), tmp_path / "regen", 42)
    regen = _hash_dir(tmp_path / "regen")
    frozen = {k: v for k, v in _hash_dir(DEMO_DIR).items() if k in regen}
    assert regen == frozen


# --------------------------------------------------------------------------
# Taxonomy exactness
# --------------------------------------------------------------------------


#: The counts written in CLAUDE.md 5.2, duplicated here on purpose.
#: Asserting against PAYMENT_CLASS_COUNTS alone proves only that the
#: generator agrees with itself - the table could drift away from the spec
#: and every test would still pass. These literals are the spec.
SPEC_COUNTS = {"E2": 8, "E3": 5, "E4": 4, "E5": 3, "E6": 2, "E7": 4, "E8": 2, "E9": 2}
SPEC_E10 = 2
SPEC_TOTAL = 150


def test_taxonomy_table_matches_the_specification() -> None:
    """Pins the generator's table to the numbers in CLAUDE.md 5.2."""
    assert TOTAL_PAYMENTS == SPEC_TOTAL
    assert E10_COUNT == SPEC_E10
    for cls, expected in SPEC_COUNTS.items():
        assert PAYMENT_CLASS_COUNTS[cls] == expected, f"{cls} drifted from the spec"


def test_class_counts_sum_to_the_specified_total() -> None:
    assert sum(PAYMENT_CLASS_COUNTS.values()) == TOTAL_PAYMENTS


def test_generated_credits_match_the_spec_per_class(batch: GeneratedBatch) -> None:
    """Counted from the generated data against the spec literals, so a
    builder that silently emits the wrong number of credits is caught even
    if the taxonomy table is correct."""
    counts = batch.class_counts()
    assert counts["E9"] == SPEC_COUNTS["E9"]
    assert counts["E10"] == SPEC_E10
    assert counts["E8"] == SPEC_COUNTS["E8"]
    assert counts["E6"] == SPEC_COUNTS["E6"]
    assert counts["E3"] == SPEC_COUNTS["E3"]


def test_every_error_class_is_present(batch: GeneratedBatch) -> None:
    counts = batch.class_counts()
    for cls in [f"E{n}" for n in range(1, 11)]:
        assert counts.get(cls, 0) > 0, f"{cls} produced no records"


def test_designed_unresolvable_counts_are_exact(batch: GeneratedBatch) -> None:
    """E9 and E10 are the honest exception list. Their counts are the
    number the escalation screen must show, so they are not approximate."""
    counts = batch.class_counts()
    assert counts["E9"] == PAYMENT_CLASS_COUNTS["E9"]
    assert counts["E10"] == E10_COUNT


def test_every_bank_credit_appears_in_ground_truth_exactly_once(
    batch: GeneratedBatch,
) -> None:
    ids = [b.bank_txn_id for b in batch.bank_txns]
    assert len(ids) == len(set(ids)), "duplicate bank_txn_id"
    assert set(ids) == set(batch.ground_truth), "ground truth does not cover credits"


def test_ground_truth_references_only_real_records(batch: GeneratedBatch) -> None:
    """A dangling reference would make accuracy unmeasurable for that row."""
    payment_ids = {p.payment_id for p in batch.payments}
    settlement_ids = {s.settlement_id for s in batch.settlements}
    for txn_id, entry in batch.ground_truth.items():
        assert set(entry["source_payment_ids"]) <= payment_ids, txn_id
        if entry["settlement_id"] is not None:
            assert entry["settlement_id"] in settlement_ids, txn_id


def test_payment_and_order_ids_are_unique(batch: GeneratedBatch) -> None:
    assert len({p.payment_id for p in batch.payments}) == len(batch.payments)
    assert len({o.order_id for o in batch.orders}) == len(batch.orders)


def test_every_payment_has_its_order(batch: GeneratedBatch) -> None:
    order_ids = {o.order_id for o in batch.orders}
    assert all(p.order_id in order_ids for p in batch.payments)


# --------------------------------------------------------------------------
# Money invariants
# --------------------------------------------------------------------------


def test_every_amount_is_decimal_quantized_to_two_places(
    batch: GeneratedBatch,
) -> None:
    for payment in batch.payments:
        for value in (payment.amount_gross, payment.refund_amount):
            assert isinstance(value, Decimal)
            assert value == value.quantize(Decimal("0.01"))
    for txn in batch.bank_txns:
        assert txn.credit_amount == txn.credit_amount.quantize(Decimal("0.01"))


def test_settlement_math_reproduces_from_raw_records(
    batch: GeneratedBatch, by_id: dict
) -> None:
    """The check the verifier will perform in Stage 5. If it fails here the
    data is internally inconsistent and no verdict could ever verify."""
    for settlement in batch.settlements:
        members = [by_id[pid] for pid in settlement.payment_ids]
        gross, mdr, gst, net = settlement_math(
            members, get_schedule(settlement.fee_schedule), settlement.rounding
        )
        assert gross == settlement.amount_gross, settlement.settlement_id
        assert mdr == settlement.fee_mdr, settlement.settlement_id
        assert gst == settlement.fee_gst, settlement.settlement_id
        assert net == settlement.amount_settled, settlement.settlement_id


def test_gst_is_exactly_18_percent_of_mdr_on_every_fee_row(
    batch: GeneratedBatch,
) -> None:
    for payment in batch.payments:
        if payment.fee_mdr is not None:
            assert payment.fee_gst == gst_on(payment.fee_mdr), payment.payment_id


def test_rounding_never_exceeds_two_rupees(batch: GeneratedBatch) -> None:
    """Beyond 2.00 a settlement falls outside Pass C's tolerance and would
    become an exception for a reason the taxonomy never declared."""
    for settlement in batch.settlements:
        assert abs(settlement.rounding) <= PASS_C_TOLERANCE, settlement.settlement_id


def test_no_negative_or_zero_credits(batch: GeneratedBatch) -> None:
    assert all(t.credit_amount > 0 for t in batch.bank_txns)


def test_fees_never_exceed_gross(batch: GeneratedBatch) -> None:
    for settlement in batch.settlements:
        assert settlement.fee_mdr + settlement.fee_gst < settlement.amount_gross


# --------------------------------------------------------------------------
# Per-class structure
# --------------------------------------------------------------------------


def test_e5_legs_sum_exactly_to_the_settlement_net(batch: GeneratedBatch) -> None:
    legs = credits_in_class(batch, "E5")
    assert len(legs) >= 2
    by_settlement: dict[str, list] = {}
    for leg in legs:
        sid = batch.ground_truth[leg.bank_txn_id]["settlement_id"]
        by_settlement.setdefault(sid, []).append(leg)

    settlements = {s.settlement_id: s for s in batch.settlements}
    for sid, group in by_settlement.items():
        assert len(group) == 2, f"{sid} should split into exactly two legs"
        total = money(sum(leg.credit_amount for leg in group))
        assert total == settlements[sid].amount_settled


def test_e5_legs_are_uneven(batch: GeneratedBatch) -> None:
    """A half-and-half split would let a matcher guess the pairing from the
    amounts alone, so the class would prove nothing."""
    legs = credits_in_class(batch, "E5")
    by_settlement: dict[str, list] = {}
    for leg in legs:
        sid = batch.ground_truth[leg.bank_txn_id]["settlement_id"]
        by_settlement.setdefault(sid, []).append(leg)
    for group in by_settlement.values():
        assert group[0].credit_amount != group[1].credit_amount


def test_e5_legs_do_not_collide_with_another_settlement_net(
    batch: GeneratedBatch,
) -> None:
    """An accidental collision would create ambiguity the taxonomy never
    declared and make Pass B's tie-break tests flaky."""
    legs = credits_in_class(batch, "E5")
    e5_settlements = {
        batch.ground_truth[leg.bank_txn_id]["settlement_id"] for leg in legs
    }
    others = [s for s in batch.settlements if s.settlement_id not in e5_settlements]
    for leg in legs:
        for settlement in others:
            assert leg.credit_amount != settlement.amount_settled


def test_e6_utr_appears_exactly_twice_with_different_amounts(
    batch: GeneratedBatch,
) -> None:
    credits = credits_in_class(batch, "E6")
    assert len(credits) == 2
    utrs = {normalize_utr(c.narration) for c in credits}
    assert len(utrs) == 1, "E6 credits must share one UTR"
    assert len({c.credit_amount for c in credits}) == 2, "amounts must differ"


def test_e6_utr_is_not_reused_by_any_other_credit(batch: GeneratedBatch) -> None:
    """Exactly twice - a third occurrence would change what Pass A sees."""
    credits = credits_in_class(batch, "E6")
    shared = normalize_utr(credits[0].narration)
    occurrences = [
        c for c in batch.bank_txns if normalize_utr(c.narration) == shared
    ]
    assert len(occurrences) == 2


def test_e8_narrations_yield_no_utr(batch: GeneratedBatch) -> None:
    """Tested against the real regex library, not a reimplementation."""
    credits = credits_in_class(batch, "E8")
    assert len(credits) == PAYMENT_CLASS_COUNTS["E8"]
    for credit in credits:
        assert normalize_utr(credit.narration) is None, credit.narration


def test_only_e8_narrations_fail_to_parse(batch: GeneratedBatch) -> None:
    """If a clean class also failed to parse, Layer 1's match rate would
    drop for a reason the taxonomy never accounted for."""
    unparseable = {
        b.bank_txn_id for b in batch.bank_txns if normalize_utr(b.narration) is None
    }
    classes = {batch.ground_truth[i]["error_class"] for i in unparseable}
    assert classes == {"E8"}


def test_e3_payments_carry_a_real_refund(batch: GeneratedBatch, by_id: dict) -> None:
    for settlement in settlements_in_class(batch, "E3"):
        for pid in settlement.payment_ids:
            payment = by_id[pid]
            assert payment.refund_amount > 0
            assert payment.status == "partial_refund"


def test_e4_applied_schedule_is_not_the_active_one(batch: GeneratedBatch) -> None:
    """That mismatch is the entire error class."""
    settlements = settlements_in_class(batch, "E4")
    assert settlements
    for settlement in settlements:
        active = active_schedule(settlement.settled_at).name
        assert settlement.fee_schedule != active, settlement.settlement_id


def test_e4_discrepancy_exceeds_pass_c_tolerance(
    batch: GeneratedBatch, by_id: dict
) -> None:
    """If the two schedules differed by under 2.00 for these payments, Pass
    C would absorb E4 into a tolerance match and the class would never
    reach the investigator at all."""
    for settlement in settlements_in_class(batch, "E4"):
        members = [by_id[pid] for pid in settlement.payment_ids]
        _, _, _, active_net = settlement_math(
            members, active_schedule(settlement.settled_at)
        )
        drift = abs(active_net - settlement.amount_settled)
        assert drift > PASS_C_TOLERANCE, settlement.settlement_id


def test_e2_credits_land_later_than_the_settlement_date(
    batch: GeneratedBatch,
) -> None:
    settlements = {s.settlement_id: s for s in batch.settlements}
    for credit in credits_in_class(batch, "E2"):
        sid = batch.ground_truth[credit.bank_txn_id]["settlement_id"]
        assert credit.value_date > settlements[sid].settled_at


def test_e7_settlements_carry_actual_drift(batch: GeneratedBatch) -> None:
    for settlement in settlements_in_class(batch, "E7"):
        assert settlement.rounding != 0
        assert abs(settlement.rounding) <= PASS_C_TOLERANCE


# --------------------------------------------------------------------------
# Designed unresolvables
# --------------------------------------------------------------------------


def test_e9_settlements_have_no_bank_credit(batch: GeneratedBatch) -> None:
    """The money genuinely never arrived. Nobody can resolve this."""
    missing = set(batch.missing_settlements)
    assert len(missing) == PAYMENT_CLASS_COUNTS["E9"]
    referenced = {v["settlement_id"] for v in batch.ground_truth.values()}
    assert not (missing & referenced)


def test_e9_settlements_still_exist_on_the_razorpay_side(
    batch: GeneratedBatch,
) -> None:
    """Absent from the bank, present in Razorpay - that asymmetry is what
    makes E9 detectable at all, and what the escalation note explains."""
    ids = {s.settlement_id for s in batch.settlements}
    assert set(batch.missing_settlements) <= ids


def test_e10_credits_are_brute_force_unresolvable(
    batch: GeneratedBatch, by_id: dict
) -> None:
    """The single most important test in this file.

    Constructing an E10 to be unmatchable is not the same as it being
    unmatchable: a randomly drawn amount can collide with a real settlement
    net by chance. Such an E10 would be quietly resolvable, the honest
    exception list would be wrong, and every other test here would still
    pass. This checks every settlement under both fee schedules.
    """
    credits = credits_in_class(batch, "E10")
    assert len(credits) == E10_COUNT
    for credit in credits:
        assert verify_unresolvable(credit, batch.settlements, by_id), (
            f"{credit.bank_txn_id} is accidentally resolvable"
        )


def test_e10_credits_cite_no_payments(batch: GeneratedBatch) -> None:
    for credit in credits_in_class(batch, "E10"):
        assert batch.ground_truth[credit.bank_txn_id]["source_payment_ids"] == []


def test_unresolvability_guard_rejects_a_matchable_credit(
    batch: GeneratedBatch, by_id: dict
) -> None:
    """Guard the guard. A verify_unresolvable that always returned True
    would make the test above pass while proving nothing."""
    real = next(
        c
        for c in batch.bank_txns
        if batch.ground_truth[c.bank_txn_id]["error_class"] == "E1"
    )
    assert not verify_unresolvable(real, batch.settlements, by_id)


# --------------------------------------------------------------------------
# Fee-schedule cutover
# --------------------------------------------------------------------------


def test_a_settlement_lands_exactly_on_the_cutover(batch: GeneratedBatch) -> None:
    """The boundary is where an off-by-one-day bug lives. Without a record
    sitting on it the verifier's schedule check is never exercised."""
    on_cutover = [s for s in batch.settlements if s.settled_at == FEE_CUTOVER_DATE]
    assert on_cutover
    assert all(s.fee_schedule == "v2" for s in on_cutover)


def test_a_settlement_lands_on_the_last_business_day_before_cutover(
    batch: GeneratedBatch,
) -> None:
    day_before = previous_business_day(FEE_CUTOVER_DATE)
    before = [s for s in batch.settlements if s.settled_at == day_before]
    assert before
    assert all(s.fee_schedule == "v1" for s in before)


def test_no_settlement_or_credit_falls_on_a_weekend(batch: GeneratedBatch) -> None:
    """Banks do not settle at weekends. A weekend date would also make the
    business-day window arithmetic untestable against real records."""
    assert all(s.settled_at.weekday() < 5 for s in batch.settlements)
    assert all(t.value_date.weekday() < 5 for t in batch.bank_txns)


def test_cutover_payments_come_out_of_e1s_allocation() -> None:
    """They are clean matches that happen to sit on an edge. Adding them on
    top would push the batch past 150 payments."""
    assert CUTOVER_PAYMENTS < PAYMENT_CLASS_COUNTS["E1"]
    assert CUTOVER_PAYMENTS % 2 == 0


# --------------------------------------------------------------------------
# No accidental ambiguity
# --------------------------------------------------------------------------


def test_no_two_settlements_collide_on_amount_and_date_window(
    batch: GeneratedBatch,
) -> None:
    """Section 12.1: an undeclared collision makes Pass B's tie-break tests
    flaky, because the ambiguity would be real but unrecorded."""
    settlements = sorted(batch.settlements, key=lambda s: s.settled_at)
    for i, first in enumerate(settlements):
        for second in settlements[i + 1 :]:
            if abs((second.settled_at - first.settled_at).days) > 3:
                continue
            if first.amount_settled == second.amount_settled:
                pytest.fail(
                    f"{first.settlement_id} and {second.settlement_id} share an "
                    f"amount within a 3-day window"
                )


def test_all_settlement_utrs_are_unique_except_the_seeded_duplicate(
    batch: GeneratedBatch,
) -> None:
    utrs = [s.utr for s in batch.settlements]
    duplicates = {u for u in utrs if utrs.count(u) > 1}
    assert len(duplicates) == 1, "only E6 may duplicate a UTR"


def test_every_utr_is_sixteen_characters(batch: GeneratedBatch) -> None:
    assert all(len(s.utr) == 16 for s in batch.settlements)


# --------------------------------------------------------------------------
# Ground-truth quarantine (11.4)
# --------------------------------------------------------------------------


def test_load_batch_returns_no_ground_truth() -> None:
    """The load path has no parameter to ask for it. That is the cheapest
    way to pass the pipeline quarantine test in Stage 7."""
    loaded = load_batch(DEMO_DIR)
    assert loaded.ground_truth == {}
    assert loaded.missing_settlements == {}


def test_ground_truth_file_is_valid_and_complete() -> None:
    payload = load_ground_truth(DEMO_DIR)
    assert payload["seed"] == 42
    assert payload["bank_txns"]
    assert payload["missing_settlements"]
    loaded = load_batch(DEMO_DIR)
    assert set(payload["bank_txns"]) == {b.bank_txn_id for b in loaded.bank_txns}


def test_ground_truth_is_json_serializable_without_floats() -> None:
    """A float in ground truth would reintroduce the rounding error the
    whole Decimal discipline exists to prevent."""
    raw = (DEMO_DIR / "ground_truth.json").read_text(encoding="utf-8")
    payload = json.loads(raw)

    def walk(node: object) -> None:
        if isinstance(node, float):
            pytest.fail("ground truth contains a float")
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_written_data_reloads_identically(
    batch: GeneratedBatch, tmp_path: Path
) -> None:
    write_batch(batch, tmp_path, 42)
    reloaded = load_batch(tmp_path)
    assert len(reloaded.payments) == len(batch.payments)
    assert len(reloaded.settlements) == len(batch.settlements)
    original = {p.payment_id: p.amount_gross for p in batch.payments}
    assert {p.payment_id: p.amount_gross for p in reloaded.payments} == original


def test_unsettled_fields_reload_as_none_not_zero(tmp_path: Path) -> None:
    """An empty CSV cell means absent. Reading it back as 0.00 would make an
    unsettled payment look like a settlement of nothing."""
    write_batch(generate(7), tmp_path, 7)
    reloaded = load_batch(tmp_path)
    assert all(
        p.amount_settled is None or p.amount_settled > 0 for p in reloaded.payments
    )
    assert any(t.balance is None for t in reloaded.bank_txns)


def test_bank_csv_ships_no_pre_parsed_utr(tmp_path: Path) -> None:
    """A pre-parsed column would let Layer 1 skip normalize_utr, and E8
    would silently stop testing anything (6, Pass A)."""
    write_batch(generate(42), tmp_path, 42)
    reloaded = load_batch(tmp_path)
    assert all(t.utr is None for t in reloaded.bank_txns)
    assert any(normalize_utr(t.narration) is not None for t in reloaded.bank_txns)


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


def test_generator_uses_no_global_randomness(tmp_path: Path) -> None:
    """Seeding the global random module must not change the output. If it
    did, anything else in the process touching random would silently break
    determinism - and the replay demo with it."""
    import random

    random.seed(1)
    write_batch(generate(42), tmp_path / "a", 42)
    random.seed(999)
    write_batch(generate(42), tmp_path / "b", 42)
    assert _hash_dir(tmp_path / "a") == _hash_dir(tmp_path / "b")


def test_generator_imports_no_llm_sdk() -> None:
    import subprocess
    import sys

    code = (
        "import sys, closo.generator; "
        "print(any(m.startswith('google.genai') for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert out.stdout.strip() == "False"
