"""SQLite schema and connection handling."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .pii import PiiVault, load_master_key

SCHEMA_VERSION = "1"

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per input file ever ingested. sha256 makes re-ingest a no-op.
CREATE TABLE IF NOT EXISTS ingested_files (
    file_id        INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id    TEXT,                 -- leads only
    batch_id       TEXT,                 -- leads only (file stem)
    source_path    TEXT NOT NULL,
    sha256         TEXT NOT NULL UNIQUE,
    ingest_seq     INTEGER NOT NULL,     -- order files were processed in
    ingested_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    lines_total    INTEGER NOT NULL DEFAULT 0,  -- events: non-empty lines read
    UNIQUE (kind, workflow_id, batch_id)
);

-- Current, merged state of each lead. Identity = (workflow_id, phone_hash).
CREATE TABLE IF NOT EXISTS leads (
    lead_id        INTEGER PRIMARY KEY,
    workflow_id    TEXT NOT NULL,
    phone_hash     TEXT NOT NULL,         -- HMAC-SHA256 blind index of +91XXXXXXXXXX
    phone_last4    TEXT NOT NULL,         -- only what the masked report shows
    batch_id       TEXT NOT NULL,         -- batch of the first valid row; never changes
    external_ref   TEXT NOT NULL,
    attributes     TEXT NOT NULL,         -- JSON of non-PII fields (nulls kept; defaults applied on read)
    pii_enc        BLOB NOT NULL,         -- Fernet-encrypted JSON of PII fields
    created_file_id INTEGER NOT NULL REFERENCES ingested_files(file_id),
    updated_file_id INTEGER NOT NULL REFERENCES ingested_files(file_id),
    UNIQUE (workflow_id, phone_hash)
);

-- Lineage: what happened to every non-blank lead row. No PII.
CREATE TABLE IF NOT EXISTS lead_rows (
    file_id        INTEGER NOT NULL REFERENCES ingested_files(file_id),
    workflow_id    TEXT NOT NULL,
    batch_id       TEXT NOT NULL,
    row_number     INTEGER NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('created', 'merged', 'rejected')),
    reason         TEXT,                  -- rejected rows only
    lead_id        INTEGER REFERENCES leads(lead_id),
    field_warnings INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, batch_id, row_number),
    CHECK ((outcome = 'rejected') = (reason IS NOT NULL)),
    CHECK ((outcome = 'rejected') = (lead_id IS NULL))
);

-- The interaction ledger. Append-only (enforced by triggers below).
-- Linking to leads is NOT stored here: it is derived at report time, so an event
-- that arrived before its lead links automatically once the lead exists.
CREATE TABLE IF NOT EXISTS events (
    event_id         TEXT PRIMARY KEY,
    event_type       TEXT NOT NULL,
    workflow_id      TEXT,
    call_id          TEXT,
    occurred_at_raw  TEXT NOT NULL,
    occurred_at_us   INTEGER NOT NULL,    -- UTC epoch microseconds
    phone_hash       TEXT,                -- NULL if no phone or phone invalid
    disposition_code TEXT,
    body             TEXT NOT NULL,       -- full event JSON minus the raw phone (PII)
    file_id          INTEGER NOT NULL REFERENCES ingested_files(file_id),
    line_number      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_link ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS ix_events_call ON events (workflow_id, call_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;

-- Lines that never made it into the ledger.
CREATE TABLE IF NOT EXISTS event_rejects (
    file_id     INTEGER NOT NULL REFERENCES ingested_files(file_id),
    line_number INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('malformed', 'duplicate')),
    event_id    TEXT,
    PRIMARY KEY (file_id, line_number)
);
"""


class KeyMismatchError(RuntimeError):
    pass


def connect(db_path: Path) -> tuple[sqlite3.Connection, PiiVault]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit transactions
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(DDL)

    vault = PiiVault(load_master_key(db_path))
    row = conn.execute("SELECT value FROM meta WHERE key = 'key_fingerprint'").fetchone()
    if row is None:
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [("key_fingerprint", vault.fingerprint), ("schema_version", SCHEMA_VERSION)],
        )
    elif row[0] != vault.fingerprint:
        conn.close()
        raise KeyMismatchError(
            "PII key does not match the key this database was created with; "
            "refusing to continue (event linking would silently break)."
        )
    return conn, vault
