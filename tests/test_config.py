"""Tests for money, fee schedules, UTR normalization and date windows.

Per CLAUDE.md 12, edge cases outnumber happy paths. The happy path here
proves almost nothing: any implementation multiplies two numbers. What
matters is what happens at the boundaries, because every one of them is a
place where a wrong answer would look like a plausible one.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal

import pytest

from closo import config as c


# --------------------------------------------------------------------------
# money()
# --------------------------------------------------------------------------


def test_money_quantizes_to_two_places() -> None:
    assert c.money("100") == Decimal("100.00")
    assert c.money(250) == Decimal("250.00")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100.004", "100.00"),  # rounds down
        ("100.005", "100.01"),  # HALF_UP, not banker's rounding
        ("100.015", "100.02"),  # the case where banker's rounding would give .01
        ("-100.005", "-100.01"),  # HALF_UP away from zero on negatives too
        ("0.005", "0.01"),
        ("0.004", "0.00"),
    ],
)
def test_money_rounding_boundaries(raw: str, expected: str) -> None:
    """ROUND_HALF_UP, including the .005 cases that separate it from
    Python's default banker's rounding."""
    assert c.money(raw) == Decimal(expected)


def test_money_rejects_float() -> None:
    """A float has already lost precision; quantizing it later cannot
    recover the paisa. Section 11.1 makes this a build-breaking rule."""
    with pytest.raises(TypeError, match="float is not allowed"):
        c.money(100.5)


def test_money_rejects_float_even_when_it_looks_exact() -> None:
    """0.1 + 0.2 is the classic case. Refusing floats outright is the only
    defence that does not depend on noticing."""
    with pytest.raises(TypeError):
        c.money(0.1 + 0.2)


def test_gst_is_exactly_18_percent_of_mdr() -> None:
    assert c.gst_on(Decimal("100.00")) == Decimal("18.00")
    assert c.gst_on(Decimal("112.50")) == Decimal("20.25")


def test_gst_on_zero_mdr_is_zero() -> None:
    """UPI under v1 is free, so a UPI-only settlement has zero MDR and must
    not somehow accrue GST."""
    assert c.gst_on(c.ZERO) == Decimal("0.00")


def test_gst_rounds_rather_than_truncates() -> None:
    """1.005 -> 1.01. A truncating implementation would drift a paisa per
    fee row and the drift compounds across a 40-settlement batch."""
    assert c.gst_on(Decimal("5.5833")) == Decimal("1.00")
    assert c.gst_on(Decimal("5.5834")) == Decimal("1.01")


# --------------------------------------------------------------------------
# Fee schedules
# --------------------------------------------------------------------------


def test_v1_and_v2_differ_on_every_method() -> None:
    """E4 is a settlement computed under the wrong schedule. If the two
    schedules agreed on any method, that error class would be invisible for
    those payments and the generator would emit unfalsifiable test data."""
    gross = Decimal("5000.00")
    for method in c.PAYMENT_METHODS:
        v1 = c.FEE_SCHEDULE_V1.mdr_for(method, gross)
        v2 = c.FEE_SCHEDULE_V2.mdr_for(method, gross)
        assert v1 != v2, f"{method} costs the same under v1 and v2"


def test_percentage_and_flat_fees_compute() -> None:
    assert c.FEE_SCHEDULE_V1.mdr_for("card", Decimal("5000.00")) == Decimal("100.00")
    assert c.FEE_SCHEDULE_V1.mdr_for("netbanking", Decimal("5000.00")) == Decimal("10.00")
    assert c.FEE_SCHEDULE_V1.mdr_for("upi", Decimal("5000.00")) == Decimal("0.00")


def test_flat_fee_ignores_gross() -> None:
    """A flat fee that quietly scaled with gross would still look sane on a
    single record and only diverge in aggregate."""
    small = c.FEE_SCHEDULE_V1.mdr_for("netbanking", Decimal("50.00"))
    large = c.FEE_SCHEDULE_V1.mdr_for("netbanking", Decimal("500000.00"))
    assert small == large == Decimal("10.00")


def test_mdr_on_zero_gross_is_flat_component_only() -> None:
    assert c.FEE_SCHEDULE_V2.mdr_for("card", c.ZERO) == Decimal("0.00")
    assert c.FEE_SCHEDULE_V2.mdr_for("upi", c.ZERO) == Decimal("2.00")


def test_unknown_method_raises_rather_than_charging_nothing() -> None:
    """Silently returning zero would make a bad record settle for its full
    gross, which reconciles perfectly and is completely wrong."""
    with pytest.raises(KeyError, match="unknown payment method"):
        c.FEE_SCHEDULE_V1.mdr_for("crypto", Decimal("100.00"))


def test_unknown_schedule_name_raises() -> None:
    with pytest.raises(KeyError, match="unknown fee schedule"):
        c.get_schedule("v3")


def test_cutover_date_itself_belongs_to_v2() -> None:
    """The boundary the verifier checks in 8.3. Off by one day here means
    every settlement on the cutover is judged against the wrong schedule."""
    assert c.active_schedule(c.FEE_CUTOVER_DATE).name == "v2"


def test_day_before_cutover_is_v1() -> None:
    day_before = c.FEE_CUTOVER_DATE - timedelta(days=1)
    assert c.active_schedule(day_before).name == "v1"


def test_well_after_cutover_is_v2() -> None:
    assert c.active_schedule(date(2027, 1, 1)).name == "v2"


def test_schedules_are_immutable() -> None:
    """Frozen so no caller can reprice a schedule mid-run and make two
    settlements in the same batch disagree about what a card costs."""
    with pytest.raises(Exception):
        c.FEE_SCHEDULE_V1.name = "tampered"  # type: ignore[misc]


# --------------------------------------------------------------------------
# UTR normalization
# --------------------------------------------------------------------------

GOOD_UTR = "SBIN0123456789AB"


@pytest.mark.parametrize(
    "narration",
    [
        f"NEFT-RAZORPAYSOFTWARE-{GOOD_UTR}-SETTLEMENT",
        f"neft/razorpaysoftware/{GOOD_UTR}/settlement",
        f"  NEFT  RAZORPAY   {GOOD_UTR}   CR  ",
        f"RTGS|{GOOD_UTR}|COLLECTION",
        f"IMPS REF {GOOD_UTR}",
        GOOD_UTR,
    ],
)
def test_normalizes_through_bank_junk(narration: str) -> None:
    """Junk prefixes, casing, separators and padding all strip away."""
    assert c.normalize_utr(narration) == GOOD_UTR


@pytest.mark.parametrize(
    "truncated",
    [
        "SBIN0123456789A",  # one short
        "SBIN012345",
        "SBIN0123456789ABC",  # one long
    ],
)
def test_wrong_length_utr_does_not_match(truncated: str) -> None:
    """Section 12.2 forbids prefix matching outright. A truncated UTR must
    fail to parse and fall through to a later pass, because prefix-matching
    it onto a real UTR produces a confident wrong match - by far the most
    expensive failure mode in reconciliation. An unmatched row is merely an
    honest exception."""
    assert c.normalize_utr(f"NEFT-RAZORPAY-{truncated}-SETTLEMENT") is None


def test_two_candidate_utrs_returns_none() -> None:
    """Ambiguity is an exception, never a coin flip (11.6)."""
    other = "HDFC9876543210ZZ"
    assert c.normalize_utr(f"NEFT {GOOD_UTR} REF {other}") is None


def test_same_utr_twice_still_resolves() -> None:
    """Repetition is not ambiguity - banks echo the reference. Only two
    *distinct* candidates are unresolvable."""
    assert c.normalize_utr(f"NEFT {GOOD_UTR} REF {GOOD_UTR}") == GOOD_UTR


@pytest.mark.parametrize("narration", ["", None, "   ", "NEFT-RAZORPAY-SETTLEMENT"])
def test_no_utr_present_returns_none(narration: str | None) -> None:
    """Error class E8: the narration simply has no UTR in it."""
    assert c.normalize_utr(narration) is None


def test_utr_embedded_in_longer_token_does_not_match() -> None:
    """Boundary guards mean a 16-char run inside a 20-char blob is not a
    UTR. Without them, junk reference numbers become false matches."""
    assert c.normalize_utr(f"NEFT XXXX{GOOD_UTR}XXXX SETTLEMENT") is None


def test_normalization_is_idempotent() -> None:
    """Feeding an already-normalized UTR back in returns it unchanged, so
    the matcher can normalize defensively without corrupting good data."""
    once = c.normalize_utr(f"NEFT-RAZORPAY-{GOOD_UTR}-CR")
    assert once is not None
    assert c.normalize_utr(once) == once


# --------------------------------------------------------------------------
# Business-day windows
# --------------------------------------------------------------------------


def test_zero_business_days_is_a_no_op() -> None:
    assert c.add_business_days(date(2026, 3, 4), 0) == date(2026, 3, 4)


def test_midweek_window_is_plain_addition() -> None:
    """Monday + 3 business days is Thursday."""
    assert c.add_business_days(date(2026, 3, 2), 3) == date(2026, 3, 5)


def test_friday_window_skips_the_weekend() -> None:
    """The case that makes this function necessary. A naive +3 days gives
    Monday, wrongly rejecting a legitimate T+3 settlement and showing up as
    an unexplained dip in match rate rather than as an error."""
    friday = date(2026, 3, 6)
    assert friday.weekday() == 4
    assert c.add_business_days(friday, 3) == date(2026, 3, 11)


def test_thursday_window_crosses_one_weekend() -> None:
    thursday = date(2026, 3, 5)
    assert c.add_business_days(thursday, 3) == date(2026, 3, 10)


def test_saturday_start_rolls_into_the_next_week() -> None:
    """Settlements should not fall on a Saturday, but the window arithmetic
    must not silently produce a weekend date if one ever does."""
    saturday = date(2026, 3, 7)
    result = c.add_business_days(saturday, 3)
    assert result == date(2026, 3, 11)
    assert result.weekday() < 5


def test_negative_days_rejected() -> None:
    with pytest.raises(ValueError):
        c.add_business_days(date(2026, 3, 4), -1)


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


def test_config_imports_no_llm_sdk() -> None:
    """Section 11.3: Layer 1, the verifier and metrics all import config and
    must stay LLM-free. This assertion only means anything because the SDK
    import lives solely in closo.llm_client."""
    for module in list(sys.modules):
        assert not module.startswith("google.genai"), (
            "config pulled in the Gemini SDK"
        )


def test_tolerance_boundary_constant_is_exact() -> None:
    """Pass C matches at exactly 2.00 and rejects 2.01 (12.2). A float here
    would make the boundary itself fuzzy."""
    assert c.PASS_C_TOLERANCE == Decimal("2.00")
    assert isinstance(c.PASS_C_TOLERANCE, Decimal)

