"""Pure value-normalisation rules from the spec.

Every function here is side-effect free and returns ``None`` for "invalid" so
callers can decide whether that means a rejection or a field warning.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

_HEADER_SEP = re.compile(r"[\s_]+")
_NON_DIGIT = re.compile(r"\D")


def normalize_header(header: str) -> str:
    """Lower-case, trim, and collapse every run of ``_``/whitespace to one space."""
    return _HEADER_SEP.sub(" ", header.strip().lower()).strip()


# --------------------------------------------------------------------------- phone

def normalize_phone(raw: str | None) -> str | None:
    """Return ``+91XXXXXXXXXX`` for a valid Indian mobile number, else ``None``."""
    if raw is None:
        return None
    digits = _NON_DIGIT.sub("", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


# --------------------------------------------------------------------------- date

_MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}

_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_NUMERIC = re.compile(r"^(\d{1,2})([/-])(\d{1,2})\2(\d{4})$")
_DMY_MONTH = re.compile(r"^(\d{1,2})-([A-Za-z]+)-(\d{4})$")


def parse_date(raw: str | None) -> date | None:
    """Parse the four accepted day-first formats. Impossible dates return ``None``."""
    if raw is None:
        return None
    s = raw.strip()
    try:
        if m := _ISO_DATE.match(s):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _DMY_NUMERIC.match(s):
            return date(int(m[4]), int(m[3]), int(m[1]))
        if m := _DMY_MONTH.match(s):
            month = _MONTHS.get(m[2].lower())
            if month is None:
                return None
            return date(int(m[3]), month, int(m[1]))
    except ValueError:  # e.g. 31/02/2026, month 13
        return None
    return None


# --------------------------------------------------------------------------- decimal

_CURRENCY_PREFIX = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)$")


def parse_decimal(raw: str | None) -> Decimal | None:
    """Parse a non-negative amount with optional ₹/Rs/Rs./INR prefix; round to 2 dp."""
    if raw is None:
        return None
    s = _CURRENCY_PREFIX.sub("", raw.strip(), count=1).replace(",", "").strip()
    if not _PLAIN_NUMBER.match(s):  # rejects "-", "N/A", negatives, words
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- enum

def parse_enum(raw: str | None, allowed: list[str]) -> str | None:
    if raw is None:
        return None
    candidate = raw.strip().lower()
    allowed_lower = {v.lower() for v in allowed}
    return candidate if candidate in allowed_lower else None


# --------------------------------------------------------------------------- timestamps

def parse_timestamp(raw: object) -> datetime | None:
    """Parse an event timestamp to an aware UTC datetime.

    ``...Z`` is UTC, ``...+05:30`` carries its own offset, and a naive value is IST.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)  # accepts both 'T' and ' ' separators
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def to_epoch_us(dt: datetime) -> int:
    """Exact integer microseconds since the epoch (no float rounding)."""
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_epoch_us(us: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=us)


def ist_iso(us: int) -> str:
    return from_epoch_us(us).astimezone(IST).isoformat()


def ist_date(us: int) -> str:
    return from_epoch_us(us).astimezone(IST).date().isoformat()
