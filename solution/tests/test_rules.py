from pathlib import Path

import pytest

from ledger.catalog import load_catalog, load_schemas
from ledger.ingest import iter_lead_rows, validate_row
from ledger.report import Call, effective_disposition, format_rate, lead_bucket

SCHEMAS = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"
MOTOR = load_schemas(SCHEMAS)["motor_renewal"]
CATALOG = load_catalog(SCHEMAS)
MOTOR_DISP = CATALOG["motor_renewal"]


def row(**kw):
    base = {"customer_name": "A B", "phone": "9876543210", "policy_number": "P1",
            "policy_expiry": None, "premium_amount": None, "preferred_language": None}
    base.update(kw)
    return base


# ---------------------------------------------------------------- lead rows

def test_valid_row_normalises():
    r = validate_row(MOTOR, row(policy_expiry="01-Dec-2026", premium_amount="₹1,000", preferred_language="HI"))
    assert r.reason is None and r.warnings == 0
    assert r.values["phone"] == "+919876543210"
    assert r.values["policy_expiry"] == "2026-12-01"
    assert r.values["premium_amount"] == "1000.00"
    assert r.values["preferred_language"] == "hi"


def test_first_failure_in_schema_order_wins():
    # customer_name comes before phone in the schema
    r = validate_row(MOTOR, row(customer_name=None, phone="123"))
    assert r.reason == "missing_required:customer_name"
    r = validate_row(MOTOR, row(phone="123", policy_number=None))
    assert r.reason == "invalid_phone"
    assert validate_row(MOTOR, row(phone=None)).reason == "missing_required:phone"
    assert validate_row(MOTOR, row(policy_number=None)).reason == "missing_required:policy_number"


def test_optional_invalid_becomes_null_with_warning():
    r = validate_row(MOTOR, row(policy_expiry="31/02/2026", premium_amount="N/A", preferred_language="Hindi"))
    assert r.reason is None
    assert r.warnings == 3
    assert r.values["policy_expiry"] is None and r.values["premium_amount"] is None


def test_rejected_row_has_no_warnings():
    r = validate_row(MOTOR, row(phone="N/A", premium_amount="N/A"))
    assert r.reason == "invalid_phone" and r.warnings == 0


def test_header_aliases_blank_rows_and_row_numbers():
    text = (
        "Language,Insured Name,Policy No,Mobile No,Expiry Date,Premium,agent_notes\r\n"
        "hi, Asha ,P1,+91 98765 43210,01/12/2026,Rs. 10,VIP\r\n"
        ",,,,,,\r\n"
        "  ,  ,,,,,\r\n"
        "en,Ravi,P2,9876500000,,,\r\n"
    )
    rows = list(iter_lead_rows(text, MOTOR))
    assert [n for n, _ in rows] == [1, 2]  # blank rows skipped and not counted
    first = rows[0][1]
    assert first["customer_name"] == "Asha"
    assert first["phone"] == "+91 98765 43210"
    assert first["preferred_language"] == "hi"
    assert rows[1][1]["policy_expiry"] is None  # empty value counts as missing


# ---------------------------------------------------------------- effective disposition

def test_effective_disposition_prefers_latest_override_even_if_older_than_set():
    sets = [(100, "e1", "renewed"), (300, "e3", "callback_requested")]
    overrides = [(200, "e2", "not_interested")]
    assert effective_disposition(overrides, sets) == "not_interested"


def test_effective_disposition_latest_override_by_time_not_arrival():
    overrides = [(500, "e9", "renewed"), (400, "e10", "dnd")]
    assert effective_disposition(overrides, []) == "renewed"


def test_effective_disposition_latest_set_and_none():
    assert effective_disposition([], [(1, "a", "x"), (5, "b", "y"), (3, "c", "z")]) == "y"
    assert effective_disposition([], []) is None


def test_effective_disposition_tie_breaks_on_event_id():
    assert effective_disposition([], [(1, "evt_2", "b"), (1, "evt_1", "a")]) == "b"


# ---------------------------------------------------------------- buckets

def call(cid, tf=False, sets=(), overrides=()):
    return Call(cid, initiated_us=1, telephony_failed=tf, sets=list(sets), overrides=list(overrides))


def test_bucket_yet_to_reach_out():
    assert lead_bucket([], MOTOR_DISP) == "yet_to_reach_out"


def test_bucket_completed_beats_telephony_failed():
    calls = [call("c1", tf=True), call("c2", tf=True, sets=[(1, "e", "renewed")])]
    assert lead_bucket(calls, MOTOR_DISP) == "completed"


def test_bucket_completed_uses_effective_disposition():
    # override turns a terminal outcome into a non-terminal one
    c = call("c1", sets=[(1, "e1", "not_interested")], overrides=[(2, "e2", "callback_requested")])
    assert lead_bucket([c], MOTOR_DISP) == "in_progress_dialed"
    c = call("c1", sets=[(1, "e1", "callback_requested")], overrides=[(2, "e2", "dnd")])
    assert lead_bucket([c], MOTOR_DISP) == "completed"


def test_bucket_telephony_failed_needs_every_call():
    assert lead_bucket([call("a", tf=True), call("b", tf=True)], MOTOR_DISP) == "in_progress_telephony_failed"
    assert lead_bucket([call("a", tf=True), call("b")], MOTOR_DISP) == "in_progress_dialed"


def test_bucket_non_terminal_disposition_is_dialed():
    assert lead_bucket([call("a", sets=[(1, "e", "payment_link_sent")])], MOTOR_DISP) == "in_progress_dialed"


@pytest.mark.parametrize("num,den,expected", [(9, 16, "0.5625"), (1, 3, "0.3333"), (2, 3, "0.6667"),
                                              (1, 32, "0.0313"), (0, 5, "0.0000"), (5, 5, "1.0000")])
def test_format_rate(num, den, expected):
    assert format_rate(num, den) == expected
