"""Derive call/lead facts from the ledger and write the five report files.

Reports are always recomputed from the full ledger + current leads, so they
reflect "everything ingested so far" (including events that arrived before
their lead) and are independent of how the data was split across drops.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .catalog import Catalog
from .normalize import ist_date, ist_iso

OVERRIDE = "disposition.override"


# --------------------------------------------------------------------------- pure derivation rules

@dataclass
class Call:
    call_id: str
    initiated_us: int | None = None
    connected: bool = False
    telephony_failed: bool = False
    # (occurred_at_us, event_id, code)
    sets: list[tuple[int, str, str]] = field(default_factory=list)
    overrides: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def effective_disposition(self) -> str | None:
        return effective_disposition(self.overrides, self.sets)


def effective_disposition(overrides: list[tuple[int, str, str]],
                          sets: list[tuple[int, str, str]]) -> str | None:
    """Latest override wins; else latest set; else None.

    "Latest" is by occurred_at; an exact tie is broken by the larger event_id.
    """
    for candidates in (overrides, sets):
        if candidates:
            return max(candidates, key=lambda c: (c[0], c[1]))[2]
    return None


def lead_bucket(calls: list[Call], dispositions: dict) -> str:
    if not calls:
        return "yet_to_reach_out"
    for c in calls:
        d = dispositions.get(c.effective_disposition)
        if d is not None and d.is_terminal:
            return "completed"
    if all(c.telephony_failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


def format_rate(num: int, den: int) -> str:
    return str((Decimal(num) / Decimal(den)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- building

@dataclass
class Lead:
    lead_id: int
    workflow_id: str
    batch_id: str
    external_ref: str
    phone_last4: str
    calls: list[Call] = field(default_factory=list)


@dataclass
class Reports:
    batch_summary: list[list]
    daily_overview: list[list]
    lead_report: list[list]
    rejections: list[list]
    data_quality: dict


def build_reports(conn: sqlite3.Connection, catalog: Catalog) -> Reports:
    leads: dict[int, Lead] = {}
    lead_by_phone: dict[tuple[str, str], int] = {}
    for lead_id, wf, phone_hash, last4, batch_id, ref in conn.execute(
        "SELECT lead_id, workflow_id, phone_hash, phone_last4, batch_id, external_ref FROM leads"
    ):
        leads[lead_id] = Lead(lead_id, wf, batch_id, ref, last4)
        lead_by_phone[(wf, phone_hash)] = lead_id

    events = conn.execute(
        "SELECT event_id, event_type, workflow_id, call_id, occurred_at_us, phone_hash, disposition_code "
        "FROM events ORDER BY occurred_at_us, event_id"
    ).fetchall()

    # Pass 1: link every non-override event by (workflow, phone); remember each call's lead.
    linked: list[tuple[int, tuple]] = []
    call_lead: dict[tuple[str, str], int] = {}
    orphans = 0
    for ev in events:
        _, etype, wf, call_id, _, phone_hash, _ = ev
        if etype == OVERRIDE:
            continue
        lead_id = lead_by_phone.get((wf, phone_hash)) if phone_hash else None
        if lead_id is None:
            orphans += 1
            continue
        linked.append((lead_id, ev))
        if call_id is not None:
            call_lead.setdefault((wf, call_id), lead_id)

    # Pass 2: overrides link through the call to that call's lead.
    for ev in events:
        if ev[1] != OVERRIDE:
            continue
        lead_id = call_lead.get((ev[2], ev[3])) if ev[3] is not None else None
        if lead_id is None:
            orphans += 1
            continue
        linked.append((lead_id, ev))

    # Fold linked events into per-call facts.
    unknown_dispositions = 0
    calls: dict[tuple[int, str], Call] = {}
    for lead_id, (event_id, etype, wf, call_id, at_us, _, code) in linked:
        if etype.startswith("disposition.") and code not in catalog.get(wf, {}):
            unknown_dispositions += 1
            continue
        if call_id is None:
            continue
        call = calls.setdefault((lead_id, call_id), Call(call_id))
        if etype == "call.initiated":
            call.initiated_us = at_us if call.initiated_us is None else min(call.initiated_us, at_us)
        elif etype == "call.connected":
            call.connected = True
        elif etype == "call.telephony_failed":
            call.telephony_failed = True
        elif etype == "disposition.set":
            call.sets.append((at_us, event_id, code))
        elif etype == OVERRIDE:
            call.overrides.append((at_us, event_id, code))

    # Only calls with a (linked) call.initiated count.
    for (lead_id, _), call in calls.items():
        if call.initiated_us is not None:
            leads[lead_id].calls.append(call)
    for lead in leads.values():
        lead.calls.sort(key=lambda c: (c.initiated_us, c.call_id))

    return Reports(
        batch_summary=_batch_summary(leads, catalog),
        daily_overview=_daily_overview(leads, catalog),
        lead_report=_lead_report(leads, catalog),
        rejections=_rejections(conn),
        data_quality=_data_quality(conn, orphans, unknown_dispositions),
    )


BUCKETS = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]


def _batch_summary(leads: dict[int, Lead], catalog: Catalog) -> list[list]:
    counts: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for lead in leads.values():
        counts[(lead.workflow_id, lead.batch_id)][lead_bucket(lead.calls, catalog.get(lead.workflow_id, {}))] += 1
    return [
        [wf, batch, sum(c.values()), *(c[b] for b in BUCKETS)]
        for (wf, batch), c in sorted(counts.items())
    ]


def _daily_overview(leads: dict[int, Lead], catalog: Catalog) -> list[list]:
    total: Counter = Counter()
    connected: Counter = Counter()
    success: dict[tuple[str, str], set[int]] = defaultdict(set)
    for lead in leads.values():
        disp = catalog.get(lead.workflow_id, {})
        for call in lead.calls:
            key = (lead.workflow_id, ist_date(call.initiated_us))
            total[key] += 1
            connected[key] += call.connected
            d = disp.get(call.effective_disposition)
            if d is not None and d.is_success:
                success[key].add(lead.lead_id)
    return [
        [wf, day, total[(wf, day)], connected[(wf, day)],
         format_rate(connected[(wf, day)], total[(wf, day)]), len(success[(wf, day)])]
        for wf, day in sorted(total)
    ]


def _lead_report(leads: dict[int, Lead], catalog: Catalog) -> list[list]:
    rows = []
    for lead in leads.values():
        disp = catalog.get(lead.workflow_id, {})
        effective = [c.effective_disposition for c in lead.calls]
        current = next((e for e in reversed(effective) if e is not None), None)
        rows.append([
            lead.workflow_id,
            lead.external_ref,
            lead.batch_id,
            f"+91******{lead.phone_last4}",
            lead_bucket(lead.calls, disp),
            len(lead.calls),
            sum(c.connected for c in lead.calls),
            ist_iso(lead.calls[-1].initiated_us) if lead.calls else "",
            current or "",
            "true" if any(e in disp and disp[e].is_success for e in effective) else "false",
        ])
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def _rejections(conn: sqlite3.Connection) -> list[list]:
    return [
        list(r) for r in conn.execute(
            "SELECT workflow_id, batch_id, row_number, reason FROM lead_rows "
            "WHERE outcome = 'rejected' ORDER BY workflow_id, batch_id, row_number"
        )
    ]


def _data_quality(conn: sqlite3.Connection, orphans: int, unknown: int) -> dict:
    def scalar(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0] or 0)

    by_reason = dict(conn.execute(
        "SELECT reason, COUNT(*) FROM lead_rows WHERE outcome = 'rejected' GROUP BY reason ORDER BY reason"
    ).fetchall())
    return {
        "lead_rows_total": scalar("SELECT COUNT(*) FROM lead_rows"),
        "lead_rows_rejected": scalar("SELECT COUNT(*) FROM lead_rows WHERE outcome = 'rejected'"),
        "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": scalar("SELECT COUNT(*) FROM lead_rows WHERE outcome = 'merged'"),
        "field_warnings": scalar("SELECT SUM(field_warnings) FROM lead_rows WHERE outcome <> 'rejected'"),
        "events_total": scalar("SELECT SUM(lines_total) FROM ingested_files WHERE kind = 'events'"),
        "malformed_events": scalar("SELECT COUNT(*) FROM event_rejects WHERE kind = 'malformed'"),
        "duplicate_events_dropped": scalar("SELECT COUNT(*) FROM event_rejects WHERE kind = 'duplicate'"),
        "orphan_events": orphans,
        "unknown_disposition_events": unknown,
    }


# --------------------------------------------------------------------------- writing

HEADERS = {
    "batch_summary.csv": ["workflow_id", "batch_id", "total_leads", *BUCKETS],
    "daily_overview.csv": ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate",
                           "success_leads"],
    "lead_report.csv": ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                        "connected_calls", "last_call_at", "current_disposition", "ever_success"],
    "rejections.csv": ["workflow_id", "batch_id", "row_number", "reason"],
}


def write_reports(reports: Reports, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, rows in [
        ("batch_summary.csv", reports.batch_summary),
        ("daily_overview.csv", reports.daily_overview),
        ("lead_report.csv", reports.lead_report),
        ("rejections.csv", reports.rejections),
    ]:
        path = out_dir / name
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(HEADERS[name])
            w.writerows(rows)
        tmp.replace(path)  # atomic: readers never see a half-written report
        written.append(path)
    path = out_dir / "data_quality.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reports.data_quality, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    written.append(path)
    return written
