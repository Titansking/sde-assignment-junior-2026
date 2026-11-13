"""End-to-end tests: ingest drops into a fresh SQLite DB and inspect the reports."""
import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.catalog import load_catalog, load_schemas
from ledger.cli import main
from ledger.db import KeyMismatchError, connect
from ledger.ingest import IngestError, ingest
from ledger.report import build_reports, write_reports

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "assignment-a-data" / "schemas"
GENERATOR = REPO / "assignment-a-data" / "tools" / "generate_data.py"

MOTOR_HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"


# ---------------------------------------------------------------- helpers

def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def ev(event_id, etype, call_id, at, phone="9876543210", wf="motor_renewal", **payload):
    e = {"event_id": event_id, "event_type": etype, "workflow_id": wf, "call_id": call_id, "occurred_at": at}
    if phone is not None:
        e["phone"] = phone
    e["payload"] = payload
    return json.dumps(e)


def run(db: Path, *drops: Path):
    conn, vault = connect(db)
    try:
        ingest(conn, vault, load_schemas(SCHEMAS), list(drops))
        return build_reports(conn, load_catalog(SCHEMAS))
    finally:
        conn.close()


def report_files(db: Path, out: Path) -> dict[str, str]:
    assert main(["report", "--db", str(db), "--out", str(out)]) == 0
    return {p.name: p.read_text() for p in sorted(out.iterdir())}


@pytest.fixture(autouse=True)
def isolated_key(monkeypatch):
    # Each test DB gets its own key file next to it.
    monkeypatch.delenv("LEDGER_PII_KEY", raising=False)


# ---------------------------------------------------------------- hand-built scenario

@pytest.fixture
def drops(tmp_path):
    d1, d2 = tmp_path / "drop_1", tmp_path / "drop_2"
    write(d1 / "leads/motor_renewal/batch_001.csv",
          MOTOR_HEADER
          + "Asha Rao,+91 98765 43210,P-1,01/12/2026,\"₹1,000\",HI\n"
          + ",,,,,\n"
          + ",9876500001,P-2,,,\n"                       # missing name
          + "Ravi K,12345,P-3,,,\n"                      # invalid phone
          + "Asha R,09876543210,P-1,,Rs. 2000,Hindi\n")  # duplicate of row 1; bad language -> warning
    write(d2 / "leads/motor_renewal/batch_002.csv",
          "Mobile No,Insured Name,Policy No,Language,agent_notes\n"
          + "9876511111,Late Lead,P-9,ta,VIP\n"
          + "+919876543210,Asha Final,P-1,,\n")          # duplicate across batches: keeps batch_001
    write(d1 / "events/events_0001.jsonl", "\n".join([
        # call c1 for P-1 on 2026-10-01 IST, connected, AI says renewed, supervisor overrides
        ev("e1", "call.initiated", "c1", "2026-10-01T04:00:00Z", phone="+91 98765 43210"),
        ev("e2", "call.connected", "c1", "2026-10-01 09:30:10"),
        ev("e3", "disposition.set", "c1", "2026-10-01T09:35:00+05:30", disposition_code="renewed"),
        ev("e4", "disposition.override", "c1", "2026-10-02T10:00:00+05:30", phone=None,
           disposition_code="callback_requested"),
        # call c2 for P-1: telephony failed, late evening UTC = next day IST
        ev("e5", "call.initiated", "c2", "2026-10-01T20:00:00Z"),
        ev("e6", "call.telephony_failed", "c2", "2026-10-01T20:00:05Z", reason="sip_503"),
        # event for the lead that only arrives in drop_2 (orphan until then)
        ev("e7", "call.initiated", "c3", "2026-10-03T12:00:00+05:30", phone="9876511111"),
        ev("e8", "call.telephony_failed", "c3", "2026-10-03T12:00:02+05:30", phone="9876511111"),
        # never-uploaded number and an override for an unknown call
        ev("e9", "call.initiated", "c4", "2026-10-03T12:00:00+05:30", phone="9000000000"),
        ev("e10", "disposition.override", "c99", "2026-10-03T12:00:00+05:30", phone=None,
           disposition_code="renewed"),
        # unknown disposition on a linked call
        ev("e11", "disposition.set", "c1", "2026-10-01T09:36:00+05:30", disposition_code="RENEWD"),
        # malformed lines
        '{"event_id": "e12", "event_ty',
        '{"event_type": "call.initiated"}',
        "",
        ev("e1", "call.initiated", "c1", "2026-10-01T04:00:00Z"),  # duplicate within file
    ]) + "\n")
    write(d2 / "events/events_1001.jsonl", "\n".join([
        ev("e5", "call.initiated", "c2", "2026-10-01T20:00:00Z"),  # duplicate across drops
        # c5 for P-9, without call.initiated: must not count as a call
        ev("e13", "call.connected", "c5", "2026-10-04T12:00:00+05:30", phone="9876511111"),
    ]) + "\n")
    return d1, d2


def test_hand_built_scenario(tmp_path, drops):
    r = run(tmp_path / "db.sqlite", *drops)
    dq = r.data_quality
    assert dq == {
        "lead_rows_total": 6,
        "lead_rows_rejected": 2,
        "rejections_by_reason": {"invalid_phone": 1, "missing_required:customer_name": 1},
        "duplicate_lead_rows_merged": 2,
        "field_warnings": 1,
        "events_total": 16,
        "malformed_events": 2,
        "duplicate_events_dropped": 2,
        "orphan_events": 2,          # e9 (unknown number) and e10 (unknown call)
        "unknown_disposition_events": 1,
    }
    assert r.rejections == [["motor_renewal", "batch_001", 2, "missing_required:customer_name"],
                            ["motor_renewal", "batch_001", 3, "invalid_phone"]]
    leads = {row[1]: row for row in r.lead_report}
    # P-1: first seen in batch_001, two calls; effective disposition of c1 is the override
    assert leads["P-1"] == ["motor_renewal", "P-1", "batch_001", "+91******3210", "in_progress_dialed",
                            2, 1, "2026-10-02T01:30:00+05:30", "callback_requested", "false"]
    # P-9: its drop_1 event linked once the lead arrived; c5 has no initiated -> not a call
    assert leads["P-9"] == ["motor_renewal", "P-9", "batch_002", "+91******1111",
                            "in_progress_telephony_failed", 1, 0, "2026-10-03T12:00:00+05:30", "", "false"]
    assert r.batch_summary == [["motor_renewal", "batch_001", 1, 0, 0, 1, 0],
                               ["motor_renewal", "batch_002", 1, 0, 1, 0, 0]]
    assert r.daily_overview == [
        ["motor_renewal", "2026-10-01", 1, 1, "1.0000", 0],
        ["motor_renewal", "2026-10-02", 1, 0, "0.0000", 0],  # c2 is 01:30 IST on the 2nd
        ["motor_renewal", "2026-10-03", 1, 0, "0.0000", 0],
    ]


def test_override_to_success_counts_on_call_date(tmp_path):
    d = tmp_path / "drop"
    write(d / "leads/motor_renewal/batch_001.csv", MOTOR_HEADER + "A,9876543210,P-1,,,\n")
    write(d / "events/events_0001.jsonl", "\n".join([
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00+05:30"),
        ev("e2", "disposition.set", "c1", "2026-10-01T10:05:00+05:30", disposition_code="callback_requested"),
        ev("e3", "disposition.override", "c1", "2026-10-03T10:00:00+05:30", phone=None,
           disposition_code="renewed"),
    ]))
    r = run(tmp_path / "db.sqlite", d)
    assert r.daily_overview == [["motor_renewal", "2026-10-01", 1, 0, "0.0000", 1]]
    assert r.lead_report[0][4] == "completed" and r.lead_report[0][9] == "true"


def test_merge_last_non_null_wins_and_defaults_after_merge(tmp_path):
    d = tmp_path / "drop"
    write(d / "leads/motor_renewal/batch_001.csv",
          MOTOR_HEADER + "A,9876543210,P-1,01/12/2026,100,\n" + "B,9876543210,P-1b,,200,mr\n"
          + "C,9876543210,P-1c,bad,,\n")
    db = tmp_path / "db.sqlite"
    run(db, d)
    conn, vault = connect(db)
    lead = conn.execute("SELECT external_ref, attributes, pii_enc, batch_id FROM leads").fetchone()
    attrs = json.loads(lead[1])
    assert lead[0] == "P-1c" and lead[3] == "batch_001"
    assert attrs == {"policy_number": "P-1c", "policy_expiry": "2026-12-01", "premium_amount": "200.00",
                     "preferred_language": "mr"}
    assert vault.decrypt(lead[2]) == {"customer_name": "C", "phone": "+919876543210"}
    conn.close()

    d2 = tmp_path / "drop_b"
    write(d2 / "leads/motor_renewal/batch_001.csv", MOTOR_HEADER + "A,9876543210,P-1,,,\n")
    db2 = tmp_path / "db2.sqlite"
    run(db2, d2)
    conn, _ = connect(db2)
    assert json.loads(conn.execute("SELECT attributes FROM leads").fetchone()[0])["preferred_language"] == "en"
    conn.close()


# ---------------------------------------------------------------- idempotency / incrementality

def test_reingest_changes_nothing(tmp_path, drops):
    db = tmp_path / "db.sqlite"
    run(db, *drops)
    before = report_files(db, tmp_path / "r1")
    counts_before = _table_counts(db)
    run(db, *drops)
    run(db, drops[0])
    assert report_files(db, tmp_path / "r2") == before
    assert _table_counts(db) == counts_before


def _table_counts(db):
    conn = sqlite3.connect(db)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("ingested_files", "leads", "lead_rows", "events", "event_rejects")}
    finally:
        conn.close()


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("gen") / "data"
    subprocess.run([sys.executable, str(GENERATOR), "--seed", "7", "--scale", "small", "--out", str(out)],
                   check=True, capture_output=True)
    return out


def test_incremental_equals_all_at_once_on_generated_data(tmp_path, generated):
    inc, once = tmp_path / "inc.sqlite", tmp_path / "once.sqlite"
    assert main(["ingest", "--db", str(inc), str(generated / "drop_1")]) == 0
    assert main(["ingest", "--db", str(inc), str(generated / "drop_2")]) == 0
    assert main(["ingest", "--db", str(inc), str(generated / "drop_1")]) == 0  # re-run: no-op
    assert main(["ingest", "--db", str(once), str(generated)]) == 0
    a, b = report_files(inc, tmp_path / "a"), report_files(once, tmp_path / "b")
    assert a == b
    dq = json.loads(a["data_quality.json"])
    assert dq["lead_rows_total"] > 2000 and dq["orphan_events"] > 0 and dq["malformed_events"] > 0
    assert dq["duplicate_events_dropped"] > 0 and dq["unknown_disposition_events"] > 0


def test_generated_reports_are_internally_consistent(tmp_path, generated):
    db = tmp_path / "db.sqlite"
    main(["ingest", "--db", str(db), str(generated)])
    files = report_files(db, tmp_path / "out")
    lead_rows = list(csv.DictReader(files["lead_report.csv"].splitlines()))
    batch_rows = list(csv.DictReader(files["batch_summary.csv"].splitlines()))
    daily_rows = list(csv.DictReader(files["daily_overview.csv"].splitlines()))
    dq = json.loads(files["data_quality.json"])

    assert len(lead_rows) == sum(int(b["total_leads"]) for b in batch_rows)
    assert len(lead_rows) == dq["lead_rows_total"] - dq["lead_rows_rejected"] - dq["duplicate_lead_rows_merged"]
    assert sum(int(r["total_calls"]) for r in lead_rows) == sum(int(r["total_calls"]) for r in daily_rows)
    assert sum(int(r["connected_calls"]) for r in lead_rows) == sum(int(r["connected_calls"]) for r in daily_rows)
    keys = [(r["workflow_id"], r["external_ref"]) for r in lead_rows]
    assert keys == sorted(keys)
    for r in lead_rows:
        assert (r["bucket"] == "yet_to_reach_out") == (r["total_calls"] == "0")
        assert r["phone_masked"].startswith("+91******") and len(r["phone_masked"]) == 13


# ---------------------------------------------------------------- PII, ledger, safety

def test_no_plaintext_pii_in_database(tmp_path, drops):
    db = tmp_path / "db.sqlite"
    run(db, *drops)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    blob = b"".join(p.read_bytes() for p in tmp_path.glob("db.sqlite*") if not p.name.endswith(".key"))
    for needle in [b"Asha", b"Ravi", b"Late Lead", b"9876543210", b"98765 43210", b"9876511111"]:
        assert needle not in blob, needle


def test_ledger_is_append_only(tmp_path, drops):
    db = tmp_path / "db.sqlite"
    run(db, *drops)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET disposition_code = 'renewed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")
    conn.close()


def test_conflicting_batch_is_rejected_atomically(tmp_path, drops):
    db = tmp_path / "db.sqlite"
    run(db, drops[0])
    before = _table_counts(db)
    good, bad = tmp_path / "x_good_drop", tmp_path / "x_bad_drop"
    write(good / "events/events_9999.jsonl", ev("z1", "call.initiated", "cz", "2026-10-05T10:00:00Z") + "\n")
    write(good / "leads/motor_renewal/batch_007.csv", MOTOR_HEADER + "Y,9000000002,P-Y,,,\n")
    write(bad / "leads/motor_renewal/batch_001.csv", MOTOR_HEADER + "X,9000000001,P-X,,,\n")
    conn, vault = connect(db)
    with pytest.raises(IngestError, match="already ingested"):
        ingest(conn, vault, load_schemas(SCHEMAS), [good, bad])
    conn.close()
    assert _table_counts(db) == before  # nothing from the failed ingest was kept


def test_wrong_key_is_refused(tmp_path, drops, monkeypatch):
    db = tmp_path / "db.sqlite"
    run(db, drops[0])
    monkeypatch.setenv("LEDGER_PII_KEY", "A" * 43 + "=")
    with pytest.raises(KeyMismatchError):
        connect(db)


def test_cli_reports_errors_cleanly(tmp_path, capsys):
    assert main(["ingest", "--db", str(tmp_path / "db.sqlite"), str(tmp_path)]) == 1
    assert "no drop" in capsys.readouterr().err
