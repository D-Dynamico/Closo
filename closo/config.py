"""Configuration, money helpers, fee schedules and UTR normalization.

This module is the single source of truth for three things that the rest
of Closo is forbidden to reimplement:

1. **Money.** Every rupee amount in the repo is produced by :func:`money`.
   Floats never touch a currency value (CLAUDE.md 11.1).
2. **Fee schedules.** Two schedules, ``v1`` and ``v2``, with a cutover
   date. Error class E4 is a settlement computed under the wrong one, so
   the difference between them has to be real and detectable.
3. **UTR normalization.** Bank narrations are deliberately ugly. Exactly
   one function turns a narration into a UTR, and it refuses to guess.

No LLM imports here, ever - Layer 1, the verifier and metrics all import
this module and must stay LLM-free (11.3).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "generated"
DEMO_DIR = DATA_DIR / "demo"
DB_PATH = REPO_ROOT / "closo.db"

# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

DEMO_MODE: bool = os.getenv("DEMO_MODE", "1") == "1"
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or None
RZP_KEY_ID: str | None = os.getenv("RZP_KEY_ID") or None
RZP_KEY_SECRET: str | None = os.getenv("RZP_KEY_SECRET") or None

DEMO_SEED = 42

# --------------------------------------------------------------------------
# LLM - see CLAUDE.md 7.4. Requests are the binding constraint, not tokens.
# --------------------------------------------------------------------------

GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_RPD_LIMIT = 500  # measured 2026-08-24; the budget guard counts against this
GEMINI_RPM_LIMIT = 15
MAX_TOOL_CALLS_PER_EXCEPTION = 8  # 7.3

# Raised from the 30s in 7.3 after the first real-API run, where the two
# bounds turned out to contradict each other. Measured latency on
# gemini-3.5-flash-lite is ~4s per request, so a full eight-call
# investigation needs nine requests and about 36 seconds - the timeout fired
# before the tool budget could ever be spent, and one exception died at three
# calls with the model mid-investigation.
#
# The tool budget is the meaningful control, since requests are the scarce
# resource (7.4). The timeout exists to catch a stall, not to cap work, so it
# is set well clear of a legitimate full-length investigation.
EXCEPTION_TIMEOUT_SECONDS = 90

# Rupees per million tokens, for the cost line on the Scorecard (9.2). Zero
# by default because this runs on the free tier and a run genuinely costs
# nothing - and a made-up price on a judged scorecard is a fabricated
# number, not a conservative one. Set INR_PER_MILLION_TOKENS in .env to the
# published paid rate to see what the same batch would cost billed.
INR_PER_MILLION_TOKENS: Decimal = Decimal(os.getenv("INR_PER_MILLION_TOKENS") or "0")

# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

PAISE = Decimal("0.01")
ZERO = Decimal("0.00")
GST_RATE = Decimal("0.18")  # 18% of MDR, always


def money(value: Decimal | int | str) -> Decimal:
    """Quantize an amount to 2 decimal places, ROUND_HALF_UP.

    The only sanctioned way to produce a currency value. Rejects floats
    outright rather than silently absorbing binary rounding error.

    Raises:
        TypeError: if handed a float.
    """
    if isinstance(value, float):
        raise TypeError(
            "float is not allowed for money; pass Decimal, int or str "
            "(CLAUDE.md 11.1)"
        )
    return Decimal(value).quantize(PAISE, rounding=ROUND_HALF_UP)


def gst_on(mdr: Decimal) -> Decimal:
    """GST charged on an MDR amount. Exactly 18%, quantized."""
    return money(mdr * GST_RATE)


# --------------------------------------------------------------------------
# Fee schedules
# --------------------------------------------------------------------------

UPI = "upi"
CARD = "card"
NETBANKING = "netbanking"
PAYMENT_METHODS = (UPI, CARD, NETBANKING)


@dataclass(frozen=True)
class MethodFee:
    """MDR for one payment method: a percentage of gross plus a flat fee."""

    percent: Decimal = ZERO
    flat: Decimal = ZERO

    def mdr(self, gross: Decimal) -> Decimal:
        """MDR payable on ``gross`` under this method."""
        return money(gross * self.percent + self.flat)


@dataclass(frozen=True)
class FeeSchedule:
    """A named set of per-method MDR rules."""

    name: str
    fees: Mapping[str, MethodFee]

    def mdr_for(self, method: str, gross: Decimal) -> Decimal:
        """MDR for a single payment.

        Raises:
            KeyError: on an unknown payment method - better a loud failure
                than a silently free transaction.
        """
        if method not in self.fees:
            raise KeyError(f"unknown payment method {method!r} in schedule {self.name}")
        return self.fees[method].mdr(gross)


# v1 - the original schedule. UPI is free, cards 2%, netbanking a flat Rs 10.
FEE_SCHEDULE_V1 = FeeSchedule(
    name="v1",
    fees={
        UPI: MethodFee(percent=Decimal("0.00"), flat=ZERO),
        CARD: MethodFee(percent=Decimal("0.02"), flat=ZERO),
        NETBANKING: MethodFee(percent=ZERO, flat=Decimal("10.00")),
    },
)

# v2 - the repriced schedule. Every method moves, so a settlement computed
# under the wrong schedule is always detectable (this is error class E4).
FEE_SCHEDULE_V2 = FeeSchedule(
    name="v2",
    fees={
        UPI: MethodFee(percent=ZERO, flat=Decimal("2.00")),
        CARD: MethodFee(percent=Decimal("0.0225"), flat=ZERO),
        NETBANKING: MethodFee(percent=ZERO, flat=Decimal("12.00")),
    },
)

FEE_SCHEDULES: Mapping[str, FeeSchedule] = {
    "v1": FEE_SCHEDULE_V1,
    "v2": FEE_SCHEDULE_V2,
}

# Settlements on or after this date use v2. On the cutover itself: v2.
#
# Deliberately a Monday. Section 12.1 requires a settlement landing exactly
# on the cutover, and banks do not settle at weekends - a cutover on a
# Saturday or Sunday is a boundary no record can ever sit on, so the
# verifier's fee-schedule check would never be exercised by real data.
FEE_CUTOVER_DATE = date(2026, 3, 2)


def active_schedule(settled_at: date) -> FeeSchedule:
    """The fee schedule in force on ``settled_at``.

    The cutover date itself belongs to v2. The verifier uses this to reject
    a verdict citing the wrong schedule (8.3), so the boundary matters.
    """
    return FEE_SCHEDULE_V2 if settled_at >= FEE_CUTOVER_DATE else FEE_SCHEDULE_V1


def get_schedule(name: str) -> FeeSchedule:
    """Look up a schedule by name.

    Raises:
        KeyError: on an unknown schedule name.
    """
    if name not in FEE_SCHEDULES:
        raise KeyError(f"unknown fee schedule {name!r}")
    return FEE_SCHEDULES[name]


# --------------------------------------------------------------------------
# UTR normalization
# --------------------------------------------------------------------------

# A UTR here is exactly 16 uppercase alphanumerics. Length is load-bearing:
# a truncated UTR must fail to normalize rather than prefix-match onto a
# real one (12.2 forbids prefix matching outright).
UTR_LENGTH = 16
_UTR_TOKEN = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{16})(?![A-Z0-9])")

# Junk that banks staple onto narrations. Stripped before token extraction so
# a prefix can never be mistaken for part of the UTR.
_NARRATION_JUNK = (
    re.compile(r"\bNEFT\b", re.I),
    re.compile(r"\bRTGS\b", re.I),
    re.compile(r"\bIMPS\b", re.I),
    re.compile(r"\bUPI\b", re.I),
    re.compile(r"\bRAZORPAY\s*SOFTWARE\b", re.I),
    re.compile(r"\bRAZORPAY\b", re.I),
    re.compile(r"\bSETTLEMENT\b", re.I),
    re.compile(r"\bCR\b", re.I),
    re.compile(r"\bREF\b", re.I),
    re.compile(r"\bCOLLECTION\b", re.I),
)

_SEPARATORS = re.compile(r"[-/_.,:|]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_utr(narration: str | None) -> str | None:
    """Extract a canonical UTR from a bank narration, or ``None``.

    Returns ``None`` - never a guess - when the narration contains no
    well-formed UTR (error class E8) or when it contains more than one
    distinct candidate. Ambiguity is an exception, not a coin flip
    (CLAUDE.md 11.6).

    Args:
        narration: raw bank narration text, possibly empty or None.

    Returns:
        The 16-character uppercase UTR, or None if absent or ambiguous.
    """
    if not narration:
        return None

    text = narration.upper()
    for junk in _NARRATION_JUNK:
        text = junk.sub(" ", text)
    text = _SEPARATORS.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    candidates = {m.group(1) for m in _UTR_TOKEN.finditer(text)}
    if len(candidates) != 1:
        return None
    return candidates.pop()


# --------------------------------------------------------------------------
# Business-day arithmetic (Layer 1 Pass B windows, 6)
# --------------------------------------------------------------------------

SETTLEMENT_WINDOW_BUSINESS_DAYS = 3
PASS_C_TOLERANCE = Decimal("2.00")  # inclusive; Rs 2.01 is an exception


def previous_business_day(start: date) -> date:
    """The last business day strictly before ``start``.

    Used to place a settlement just short of the fee cutover. A plain
    ``start - 1 day`` can land on a Saturday, and a settlement dated to a
    weekend is not something a bank would ever produce.
    """
    current = start - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def add_business_days(start: date, days: int) -> date:
    """Advance ``start`` by ``days`` business days, skipping weekends.

    Banks do not settle on weekends, so a naive +3 days window wrongly
    rejects any Friday settlement. Holidays are out of scope.
    """
    if days < 0:
        raise ValueError("days must be non-negative")
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Mon-Fri
            remaining -= 1
    return current
