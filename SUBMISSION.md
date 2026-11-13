# Submission

## Candidate
- **Name:** _(fill in)_
- **Assignment chosen:** A (Data): Interaction Ledger & Reporting Pipeline
- **Time spent (honest estimate):** _(fill in)_

## How to run
```bash
# from a clean clone, Python 3.11+
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e solution                       # provides the `ledger` command

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
ledger ingest data/drop_1
ledger ingest data/drop_2                     # or: ledger ingest data   (all drops at once)
ledger report --out reports/

pytest solution                               # run the tests
```
Options: `--db PATH` (default `./ledger.db` or `$LEDGER_DB`), `--schemas DIR`. The PII key comes
from `$LEDGER_PII_KEY` or is generated at `<db>.key` on first run.

## Design

**Shape.** `ingest` turns files into durable rows. `report` derives every metric from those
rows. Nothing derived is stored, so reports always reflect "everything ingested so far".

```
lead CSVs ──validate/normalise/merge──► leads (current state) + lead_rows (lineage, no PII)
event JSONL ──parse/dedupe──────────────► events (append-only ledger) + event_rejects
                                                │
report: link events→leads by (workflow, phone HMAC); overrides via call_id → calls → buckets
```

**Schema (SQLite).**

| Table | Purpose | Key constraints |
|---|---|---|
| `ingested_files` | One row per input file: kind, workflow/batch, path, `sha256`, order, line count | `UNIQUE(sha256)` makes re-ingest a no-op. `UNIQUE(kind, workflow_id, batch_id)` stops a *different* file from claiming an existing batch. |
| `leads` | Merged current state of each lead | `UNIQUE(workflow_id, phone_hash)` is the lead identity. `batch_id` is set once and never updated. |
| `lead_rows` | What happened to every non-blank row (`created`/`merged`/`rejected` + reason + warnings) | PK `(workflow_id, batch_id, row_number)`. CHECKs: a rejected row has a reason and no lead. |
| `events` | **The interaction ledger**: every distinct event, normalised timestamp (UTC µs), phone HMAC, disposition code, full JSON body minus the raw phone | PK `event_id` gives dedupe. `BEFORE UPDATE/DELETE` triggers `RAISE(ABORT)`, so the table is append-only in the database itself. |
| `event_rejects` | Malformed and duplicate lines (file, line, kind) | Lets `data_quality.json` be recomputed exactly. |
| `meta` | Key fingerprint, schema version | Refuses to open the DB with a different PII key. |

**Linking happens at report time, not ingest time.** An event's link to a lead is a pure
function of `(workflow_id, phone_hash)`, plus `call_id` for overrides. Computing it during the
report means:

- an event that was an orphan after drop 1 links on its own once drop 2 brings the lead;
- orphan counts are always the final state;
- `ingest drop_1; ingest drop_2` and `ingest data` produce byte-identical reports. A test checks
  this on a generator seed different from 42.

**PII.** The schema marks `customer_name` and `phone` as PII.

- Phones are indexed with a **keyed HMAC-SHA256** blind index. That still allows the equality
  join between events and leads. A plain SHA-256 would not be safe here: India's mobile space is
  only about 4×10⁹ numbers, so an unkeyed hash can be brute-forced in minutes.
- All PII fields of a lead are stored as one **Fernet-encrypted** JSON blob (AES-128-CBC +
  HMAC), so they can be recovered with the key.
- Only the last 4 digits are kept in clear, because the masked report column shows exactly that.
- Event phones are hashed, and the raw phone is stripped from the stored event body.

A test scans the raw SQLite and WAL bytes for names and phone strings. The HMAC and encryption
keys are derived from one master key (`$LEDGER_PII_KEY`). In production that key would live in a
KMS or secret manager, not in the `<db>.key` file.

**Idempotency and atomicity.** One `ingest` command is one `BEGIN IMMEDIATE … COMMIT`. If
anything fails (bad batch, crash, Ctrl-C), nothing from that command is kept. Files are keyed by
content hash, so re-running a drop skips every file.

**Incremental (stretch).** Ingesting drop 2 only reads drop 2's files. Leads merge into existing
rows using an in-memory index of the workflow's leads. The report is a full recompute over the
ledger, about 3 s for 630k events. That trade-off is deliberate: storing derived link and bucket
state would mean invalidating it whenever a late lead or a late override arrives. At much larger
volume I'd materialise per-call facts and recompute only the affected leads; see below.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Linking and orphan status are derived at report time | Late leads and late overrides are handled for free. Results don't depend on how drops are split. | Store `lead_id` on events and backfill when leads arrive. More state, more ways to be wrong. |
| `connectivity_rate` and decimals use ROUND_HALF_UP (`Decimal`) | Exact, and what a business reader expects. Avoids float artefacts (`1.005` → `1.01`). | Python's float `:.4f` (banker's-ish on binary floats). They differ only on exact .00005 ties such as 1/32. |
| "Latest" override/set tie on identical `occurred_at` → larger `event_id` wins | Deterministic. Event ids are monotonic in the generator. | Arrival order, which isn't stable across drops. |
| `current_disposition` ties on identical initiated time → larger `call_id` | Deterministic | — |
| Event with an `event_id` but missing `event_type` or an unparseable `occurred_at` counts as **malformed** | It can't be placed in time, so it can't count towards any date. | Keep it and skip it in reports. The generator never produces this case. |
| Event `phone` that is present but invalid → stored with no phone hash → orphan | The spec links only through a normalised phone. | — |
| A required non-phone field that is present but invalid (e.g. a required `date`) → `invalid_value:<field>` | The spec only defines two reasons. This case can't happen with the given schemas, but the code is schema-driven. | Treat it as a warning and keep the row. Wrong for a required field. |
| Two columns mapping to the same field → the first (leftmost) wins | Predictable | Error out on the file. |
| A batch that was already ingested but arrives with **different content** → the whole ingest fails and is rolled back | Silent overwrite or a double merge would corrupt lead state. An operator should decide. | Treat it as a new batch. That breaks "batch_id is permanent". |
| Defaults are applied on every write after merging | Equivalent to applying them once at the end: a default only fills a null, and any later non-null value overwrites it either way. | — |
| `phone_last4` is stored in clear | It's exactly what the masked output shows, and it can't identify a person on its own. | Decrypt at report time, which is slower and needs no extra protection. |
| `rejections_by_reason` lists only observed reasons, sorted | The example shows reason keys, but no fixed key set. | Pre-seed every possible reason with 0. |
| `DD-Mon-YYYY` accepts 3-letter abbreviations and also full month names | It's harmless and more forgiving. | Abbreviations only. |
| Numbers with commas: commas are removed rather than checked for grouping | Indian grouping (`1,23,456`) is common in this data domain. | Strict 3-digit grouping would reject valid lakh formatting. |
| Header normalisation also trims after collapsing | `"mobile_no_"` → `"mobile no"`, not `"mobile no "` | — |
| SQLite rather than Postgres | Zero setup, and the reviewer runs one command. The schema is portable. | Postgres via the provided compose file. |

## Failure modes considered

- **Re-runs and partial re-runs:** files are content-hashed; re-ingest is a no-op (tested).
- **Crash mid-ingest:** a single transaction means all or nothing (tested with a failing batch
  after a good one). WAL mode is on.
- **Conflicting re-upload of a batch:** refused with a clear error, nothing written (tested).
- **Wrong PII key:** the DB stores a key fingerprint and refuses to open with another key.
  Otherwise new events would silently stop linking (tested).
- **Dirty input:** malformed JSON, non-object JSON, missing ids, blank lines, BOMs, short CSV
  rows, extra columns, blank rows, renamed and re-ordered headers, U+2028 inside JSON strings
  (lines are split only on `\n`).
- **Duplicates:** within a file, across files and across drops (first one wins; PK-enforced).
- **Ledger tampering:** UPDATE/DELETE on `events` aborts at the DB level (tested).
- **Report writes** go to a temp file and are then renamed, so readers never see a half-written
  file.
- **Not handled:**
  - concurrent `ingest` processes (SQLite's write lock serialises them, but there's no
    friendlier message);
  - a lead whose phone *changes* (identity is the phone by spec);
  - key rotation (would need re-encryption and re-indexing);
  - very large single files (read into memory; fine at 5k lines per file).

## Testing

`pytest solution` runs 94 tests in under 2 s.

- **Unit tests:** the six generator phone formats plus invalid ones; every date format and
  impossible dates; currency prefixes, commas, negatives and rounding; enums; the three timestamp
  forms; the IST midnight crossing.
- **Rule tests:** rejection order follows schema order; optional invalid → null + warning; no
  warnings on rejected rows; header aliases and blank-row numbering; effective disposition
  (override beats a later set, latest override by time not arrival, ties); every bucket and its
  precedence; rate formatting.
- **End-to-end:** a hand-built two-drop scenario with every type of dirt and exact expected
  reports. It covers an event before its lead, an override without a phone, a duplicate across
  drops, a call without `call.initiated`, and a UTC time that falls on the next IST date.
- **Properties on generated data (seed 7):** incremental equals all-at-once; reports are
  internally consistent (lead counts, call totals and sort order).
- **Safety:** no plaintext PII in the DB bytes, append-only triggers, atomic rollback, key
  mismatch.

An **independent re-implementation** written from the README alone (no DB, written without seeing
this code) was diffed against this pipeline. All five report files were **byte-identical**
on seed 1234 (small) and on seed 42 (large, 633k event lines).

**Not tested:** Postgres; concurrency; the `--scale large` run as an automated test (it was timed
manually instead).

## Results

Timings on a MacBook (Python 3.11, SQLite, WAL):

| Dataset | ingest drop_1 | ingest drop_2 | report | Total |
|---|---|---|---|---|
| small, seed 42 (2,057 lead rows, 8,745 event lines) | 0.08 s | 0.06 s | 0.03 s | ~0.2 s |
| large, seed 42 (143,990 lead rows, 632,602 event lines) | 8.1 s | 8.5 s | 2.8 s | **~19.6 s** |

Seed 42 small, `data_quality.json`: 2,057 lead rows; 108 rejected (42 `invalid_phone`, 25
missing name, 14 missing phone, 14 missing policy number, 13 missing order id); 57 merged
duplicates; 122 field warnings; 8,745 event lines; 8 malformed; 237 duplicates; 462 orphans; 18
unknown dispositions.

The output matches both examples in the README: `motor_renewal,2026-10-01,…,0.5625` and the
first lead's `last_call_at` of `2026-10-14T17:55:24+05:30`.

Cross-check: an independent stdlib-only reference implementation, written from the README alone, produced byte-identical output for all five files on seed 1234 small and seed 42 large.

Surprising in the data:

- About 25% of leads are never called.
- Some leads only appear in `batch_003`, under aliased headers with an extra `agent_notes`
  column.
- Rejected leads still get calls, which become orphans.
- Overrides can land in the next drop, up to 72 h after the call.

## If I had another day
1. Do the report aggregation in SQL (window functions for effective disposition), so the report
   doesn't load the whole ledger into Python.
2. Materialise per-call facts incrementally: on ingest, recompute only the calls and leads
   touched by new events or new leads.
3. Production for 100k leads/day:
   - an orchestrator such as Airflow or Temporal triggers `ingest` per drop as files land in
     object storage;
   - Postgres instead of SQLite, with a KMS-managed key;
   - quarantine tables surfaced to ops;
   - backfills as a re-ingest of the archived drops into a fresh DB, diffed against the live
     reports;
   - alerts on orphan and rejection-rate spikes.
4. Key rotation tooling (re-encrypt blobs, re-index phones under a new HMAC key version).
