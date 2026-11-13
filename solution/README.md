# Solution: Assignment A, Interaction Ledger & Reporting Pipeline

Python 3.11+, SQLite, `cryptography` (Fernet) for PII. See [`../SUBMISSION.md`](../SUBMISSION.md)
for the design, decisions and results.

## Run (from the repo root, clean clone)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e solution                      # installs the `ledger` command

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

ledger ingest data/drop_1                    # --db defaults to ./ledger.db (or $LEDGER_DB)
ledger ingest data/drop_2
ledger report --out reports/

pytest solution                              # 94 tests, < 2 s
```

Without installing: `PYTHONPATH=solution python -m ledger ...`.

* `ledger ingest PATH [PATH ...]` accepts drop folders, or a folder of drops (`ledger ingest data`
  ingests `drop_1` then `drop_2` in one transaction). Re-running it is a no-op.
* `--schemas DIR` overrides the schema folder (default `assignment-a-data/schemas`).
* PII key: `$LEDGER_PII_KEY` (url-safe base64, 32 bytes). If unset, a key is generated on first
  run at `<db>.key` (mode 0600). Keep the key with the database; the DB refuses to open with a
  different key.

## Layout

```
ledger/
  normalize.py   pure value rules: headers, phone, dates, decimals, enums, timestamps/IST
  catalog.py     loads workflow schemas + disposition catalogue
  pii.py         HMAC phone blind index + Fernet encryption of PII fields
  db.py          SQLite DDL (append-only ledger triggers, constraints), key check
  ingest.py      lead CSV + event JSONL ingestion (one transaction per command)
  report.py      derives calls/leads from the ledger, writes the 5 report files
  cli.py         argparse entry point
tests/
  test_normalize.py  phone/date/decimal/enum/timestamp rules
  test_rules.py      row validation order, effective disposition, buckets, rate formatting
  test_pipeline.py   end-to-end scenarios, idempotency, incremental == all-at-once,
                     no plaintext PII in the DB file, append-only ledger, atomic failure
```
