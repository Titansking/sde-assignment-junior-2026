"""Ingestion of lead CSVs and event JSONL files into the database.

One ``ingest`` call is one SQLite transaction: either every file in it lands or
none does. Each file is keyed by its SHA-256, so re-ingesting a file (or a whole
drop) is a no-op.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Field, WorkflowSchema
from .normalize import (
    normalize_header,
    normalize_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_timestamp,
    to_epoch_us,
)
from .pii import PiiVault


class IngestError(RuntimeError):
    pass


# --------------------------------------------------------------------------- row validation

@dataclass
class RowResult:
    reason: str | None                 # rejection reason, or None if the row is valid
    values: dict[str, str | None]      # normalised values (None = missing / invalid optional)
    warnings: int = 0


def _parse_value(f: Field, raw: str) -> str | None:
    if f.type == "string":
        return raw
    if f.type == "phone":
        return normalize_phone(raw)
    if f.type == "date":
        d = parse_date(raw)
        return d.isoformat() if d else None
    if f.type == "decimal":
        dec = parse_decimal(raw)
        return format(dec, "f") if dec is not None else None
    if f.type == "enum":
        return parse_enum(raw, list(f.values))
    raise AssertionError(f.type)


def validate_row(schema: WorkflowSchema, raw: dict[str, str | None]) -> RowResult:
    """Apply the spec's per-field rules in schema order.

    ``raw`` maps field name -> trimmed cell value, with ``None`` for missing/empty.
    """
    values: dict[str, str | None] = {}
    warnings = 0
    for f in schema.fields:
        cell = raw.get(f.name)
        if cell is None:
            if f.required:
                return RowResult(f"missing_required:{f.name}", {}, 0)
            values[f.name] = None
            continue
        parsed = _parse_value(f, cell)
        if parsed is None:
            if f.type == "phone":
                return RowResult("invalid_phone", {}, 0)
            if f.required:
                # Not reachable with the provided schemas; see SUBMISSION.md.
                return RowResult(f"invalid_value:{f.name}", {}, 0)
            warnings += 1
        values[f.name] = parsed
    return RowResult(None, values, warnings)


def map_headers(schema: WorkflowSchema, header: list[str]) -> dict[int, str]:
    """Column index -> field name. Unknown columns are ignored; the first column wins
    if two columns map to the same field."""
    mapping: dict[int, str] = {}
    taken: set[str] = set()
    for idx, h in enumerate(header):
        name = schema.header_lookup.get(normalize_header(h))
        if name and name not in taken:
            mapping[idx] = name
            taken.add(name)
    return mapping


def iter_lead_rows(text: str, schema: WorkflowSchema):
    """Yield ``(row_number, raw_values)`` for every non-blank data row."""
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None:
        return
    mapping = map_headers(schema, header)
    row_number = 0
    for cells in reader:
        if all(not c.strip() for c in cells):
            continue  # blank rows are skipped and not counted
        row_number += 1
        raw: dict[str, str | None] = {}
        for idx, name in mapping.items():
            v = cells[idx].strip() if idx < len(cells) else ""
            raw[name] = v or None
        yield row_number, raw


# --------------------------------------------------------------------------- summary

@dataclass
class IngestSummary:
    files_ingested: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    lead_rows: int = 0
    events_lines: int = 0


# --------------------------------------------------------------------------- ingest

class Ingestor:
    def __init__(self, conn: sqlite3.Connection, vault: PiiVault, schemas: dict[str, WorkflowSchema]):
        self.conn = conn
        self.vault = vault
        self.schemas = schemas
        # (workflow_id, phone_hash) -> [lead_id, attributes, pii_enc]; loaded lazily per workflow
        self._lead_cache: dict[str, dict[str, list]] = {}
        self.summary = IngestSummary()

    # ---- helpers
    def _next_seq(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(ingest_seq), 0) + 1 FROM ingested_files").fetchone()[0]

    def _already_ingested(self, sha: str) -> bool:
        return self.conn.execute("SELECT 1 FROM ingested_files WHERE sha256 = ?", (sha,)).fetchone() is not None

    def _leads_for(self, workflow_id: str) -> dict[str, list]:
        if workflow_id not in self._lead_cache:
            rows = self.conn.execute(
                "SELECT phone_hash, lead_id, attributes, pii_enc FROM leads WHERE workflow_id = ?",
                (workflow_id,),
            )
            self._lead_cache[workflow_id] = {
                h: [lead_id, json.loads(attrs), pii] for h, lead_id, attrs, pii in rows
            }
        return self._lead_cache[workflow_id]

    # ---- drops
    def ingest_drop(self, drop: Path) -> None:
        leads_dir = drop / "leads"
        if leads_dir.is_dir():
            for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
                for path in sorted(wf_dir.glob("*.csv")):
                    self.ingest_lead_file(wf_dir.name, path)
        events_dir = drop / "events"
        if events_dir.is_dir():
            for path in sorted(events_dir.glob("*.jsonl")):
                self.ingest_event_file(path)

    # ---- leads
    def ingest_lead_file(self, workflow_id: str, path: Path) -> None:
        schema = self.schemas.get(workflow_id)
        if schema is None:
            raise IngestError(f"{path}: no schema for workflow {workflow_id!r}")
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if self._already_ingested(sha):
            self.summary.files_skipped.append(str(path))
            return
        batch_id = path.stem
        clash = self.conn.execute(
            "SELECT source_path FROM ingested_files WHERE kind='leads' AND workflow_id=? AND batch_id=?",
            (workflow_id, batch_id),
        ).fetchone()
        if clash:
            raise IngestError(
                f"{path}: batch {workflow_id}/{batch_id} was already ingested from {clash[0]} "
                "with different content; refusing to guess which one is right"
            )
        cur = self.conn.execute(
            "INSERT INTO ingested_files (kind, workflow_id, batch_id, source_path, sha256, ingest_seq) "
            "VALUES ('leads', ?, ?, ?, ?, ?)",
            (workflow_id, batch_id, str(path), sha, self._next_seq()),
        )
        file_id = cur.lastrowid
        leads = self._leads_for(workflow_id)
        pii_fields = [f.name for f in schema.fields if f.pii]
        phone_field = schema.phone_field.name
        ref_field = schema.reference_field.name

        for row_number, raw in iter_lead_rows(data.decode("utf-8-sig"), schema):
            self.summary.lead_rows += 1
            result = validate_row(schema, raw)
            if result.reason:
                self.conn.execute(
                    "INSERT INTO lead_rows (file_id, workflow_id, batch_id, row_number, outcome, reason) "
                    "VALUES (?, ?, ?, ?, 'rejected', ?)",
                    (file_id, workflow_id, batch_id, row_number, result.reason),
                )
                continue

            phone = result.values[phone_field]
            phone_hash = self.vault.phone_index(phone)
            existing = leads.get(phone_hash)
            if existing is None:
                merged = dict(result.values)
                outcome = "created"
            else:
                lead_id, attrs, pii_enc = existing
                merged = {**attrs, **self.vault.decrypt(pii_enc)}
                for k, v in result.values.items():
                    if v is not None:  # last non-null write wins
                        merged[k] = v
                outcome = "merged"
            # Defaults after merging. Applying them on every write is equivalent to
            # applying them once at the end, because a default only fills a null and
            # any later non-null value overwrites it either way.
            for f in schema.fields:
                if merged.get(f.name) is None and f.default is not None:
                    merged[f.name] = f.default

            pii = {k: merged.get(k) for k in pii_fields}
            attrs = {f.name: merged.get(f.name) for f in schema.fields if not f.pii}
            pii_enc = self.vault.encrypt(pii)
            if existing is None:
                lead_id = self.conn.execute(
                    "INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id, external_ref, "
                    "attributes, pii_enc, created_file_id, updated_file_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (workflow_id, phone_hash, phone[-4:], batch_id, merged[ref_field],
                     json.dumps(attrs, sort_keys=True), pii_enc, file_id, file_id),
                ).lastrowid
            else:
                self.conn.execute(
                    "UPDATE leads SET external_ref=?, attributes=?, pii_enc=?, updated_file_id=? "
                    "WHERE lead_id=?",
                    (merged[ref_field], json.dumps(attrs, sort_keys=True), pii_enc, file_id, lead_id),
                )
            leads[phone_hash] = [lead_id, attrs, pii_enc]
            self.conn.execute(
                "INSERT INTO lead_rows (file_id, workflow_id, batch_id, row_number, outcome, lead_id, "
                "field_warnings) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_id, workflow_id, batch_id, row_number, outcome, lead_id, result.warnings),
            )
        self.summary.files_ingested.append(str(path))

    # ---- events
    def ingest_event_file(self, path: Path) -> None:
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if self._already_ingested(sha):
            self.summary.files_skipped.append(str(path))
            return
        file_id = self.conn.execute(
            "INSERT INTO ingested_files (kind, source_path, sha256, ingest_seq) VALUES ('events', ?, ?, ?)",
            (str(path), sha, self._next_seq()),
        ).lastrowid

        lines_total = 0
        rejects: list[tuple] = []
        # split on "\n" only: str.splitlines() would also split on U+2028 etc. inside JSON strings
        for line_number, line in enumerate(data.decode("utf-8", errors="replace").split("\n"), start=1):
            if not line.strip():
                continue
            lines_total += 1
            row = parse_event_line(line, self.vault)
            if row is None:
                rejects.append((file_id, line_number, "malformed", None))
                continue
            cur = self.conn.execute(
                "INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_raw, "
                "occurred_at_us, phone_hash, disposition_code, body, file_id, line_number) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (event_id) DO NOTHING",
                (*row, file_id, line_number),
            )
            if cur.rowcount == 0:
                rejects.append((file_id, line_number, "duplicate", row[0]))
        self.conn.executemany(
            "INSERT INTO event_rejects (file_id, line_number, kind, event_id) VALUES (?, ?, ?, ?)", rejects
        )
        self.conn.execute("UPDATE ingested_files SET lines_total = ? WHERE file_id = ?", (lines_total, file_id))
        self.summary.events_lines += lines_total
        self.summary.files_ingested.append(str(path))


def _opt_str(v: object) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else str(v)


def parse_event_line(line: str, vault: PiiVault) -> tuple | None:
    """Return the ledger row (without file/line columns), or ``None`` if malformed.

    Malformed = not JSON, not an object, no ``event_id``, or (our call) no
    ``event_type`` / unparseable ``occurred_at``: such an event can't be placed.
    """
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    if not isinstance(ev, dict):
        return None
    event_id = _opt_str(ev.get("event_id"))
    event_type = ev.get("event_type")
    occurred = parse_timestamp(ev.get("occurred_at"))
    if not event_id or not isinstance(event_type, str) or occurred is None:
        return None
    phone = normalize_phone(_opt_str(ev.get("phone")))
    payload = ev.get("payload")
    code = _opt_str(payload.get("disposition_code")) if isinstance(payload, dict) else None
    body = {k: v for k, v in ev.items() if k != "phone"}
    return (
        event_id,
        event_type,
        _opt_str(ev.get("workflow_id")),
        _opt_str(ev.get("call_id")),
        str(ev["occurred_at"]),
        to_epoch_us(occurred),
        vault.phone_index(phone) if phone else None,
        code,
        json.dumps(body, ensure_ascii=False, sort_keys=True),
    )


def resolve_drops(paths: list[Path]) -> list[Path]:
    """A path is a drop if it has ``leads/`` or ``events/``; otherwise its sub-folders
    are treated as drops in name order (so ``ingest data`` = all drops at once)."""
    drops: list[Path] = []
    for p in paths:
        if not p.is_dir():
            raise IngestError(f"{p}: not a directory")
        if (p / "leads").is_dir() or (p / "events").is_dir():
            drops.append(p)
            continue
        children = sorted(c for c in p.iterdir() if c.is_dir() and ((c / "leads").is_dir() or (c / "events").is_dir()))
        if not children:
            raise IngestError(f"{p}: contains no drop (expected leads/ or events/ folders)")
        drops.extend(children)
    return drops


def ingest(conn: sqlite3.Connection, vault: PiiVault, schemas: dict[str, WorkflowSchema],
           paths: list[Path]) -> IngestSummary:
    drops = resolve_drops(paths)
    ingestor = Ingestor(conn, vault, schemas)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for drop in drops:
            ingestor.ingest_drop(drop)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return ingestor.summary
