from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ledger.normalize import (
    ist_date,
    ist_iso,
    normalize_header,
    normalize_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_timestamp,
    to_epoch_us,
)


@pytest.mark.parametrize("raw", [
    "Mobile No", " mobile_no ", "MOBILE__  NO", "mobile\tno",
])
def test_normalize_header(raw):
    assert normalize_header(raw) == "mobile no"


@pytest.mark.parametrize("raw", [
    "9876543210",            # bare
    "+91 98765 43210",       # spaced
    "09876543210",           # trunk zero
    "91-98765-43210",        # dashed
    "(+91) 9876543210",      # brackets
    "+919876543210",         # E.164
    "919876543210",          # 12 digits starting 91
])
def test_phone_valid_formats(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",             # 9 digits
    "5876543210",            # bad leading digit
    "022-2345-6789",         # landline
    "N/A",
    "+1 415 555 0134",       # 11 digits not starting with 0
    "",
    "9198765432101",         # 13 digits
    "00919876543210",        # double prefix is not stripped twice
    None,
])
def test_phone_invalid(raw):
    assert normalize_phone(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", date(2026, 12, 1)),
    ("01/12/2026", date(2026, 12, 1)),     # day-first
    ("01-12-2026", date(2026, 12, 1)),
    ("01-Dec-2026", date(2026, 12, 1)),
    ("01-DEC-2026", date(2026, 12, 1)),
    ("1-dec-2026", date(2026, 12, 1)),
    ("29/02/2028", date(2028, 2, 29)),     # leap year
    (" 2026-10-05 ", date(2026, 10, 5)),
])
def test_dates_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "29/02/2026", "2026-13-01", "soon", "TBD", "01-Foo-2026",
    "2026/12/01", "01.12.2026", "01/12-2026", "",
])
def test_dates_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("27554", "27554.00"),
    ("₹27,554", "27554.00"),
    ("₹ 1,23,456.5", "123456.50"),
    ("Rs. 6850", "6850.00"),
    ("rs6850", "6850.00"),
    ("RS 6850", "6850.00"),
    ("INR 25301.4", "25301.40"),
    ("inr25301.456", "25301.46"),
    ("1.005", "1.01"),        # half-up, no float error
    ("0", "0.00"),
])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-100", "Rs. -5", "₹", "1.2.3", "USD 5"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum():
    values = ["en", "hi", "ta"]
    assert parse_enum("HI", values) == "hi"
    assert parse_enum(" ta ", values) == "ta"
    assert parse_enum("Hindi", values) is None


@pytest.mark.parametrize("raw", [
    "2026-10-03T04:31:00Z",
    "2026-10-03T10:01:00+05:30",
    "2026-10-03 10:01:00",       # naive => IST
    "2026-10-03T10:01:00",
])
def test_timestamps_all_mean_the_same_instant(raw):
    assert parse_timestamp(raw) == datetime(2026, 10, 3, 4, 31, tzinfo=timezone.utc)


def test_timestamp_invalid():
    assert parse_timestamp("yesterday") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp(12345) is None


def test_ist_date_crosses_midnight():
    # 20:00 UTC is 01:30 the next day in IST
    us = to_epoch_us(parse_timestamp("2026-10-03T20:00:00Z"))
    assert ist_date(us) == "2026-10-04"
    assert ist_iso(us) == "2026-10-04T01:30:00+05:30"
