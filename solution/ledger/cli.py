"""Command line interface: ``ledger ingest <drop>...`` and ``ledger report --out <dir>``."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .catalog import load_catalog, load_schemas
from .db import connect
from .ingest import ingest
from .report import build_reports, write_reports

DEFAULT_SCHEMAS = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=Path(os.environ.get("LEDGER_DB", "ledger.db")),
                        help="SQLite database file (default: $LEDGER_DB or ./ledger.db)")
    common.add_argument("--schemas", type=Path, default=DEFAULT_SCHEMAS,
                        help="folder with <workflow>.json schemas and dispositions.csv")

    parser = argparse.ArgumentParser(prog="ledger", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_ingest = sub.add_parser("ingest", parents=[common], help="ingest one or more drops (idempotent)")
    p_ingest.add_argument("paths", nargs="+", type=Path,
                          help="drop folder(s), or a folder of drops (processed in name order)")
    p_report = sub.add_parser("report", parents=[common], help="write the five report files")
    p_report.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.perf_counter()
    conn, vault = connect(args.db)
    try:
        if args.command == "ingest":
            summary = ingest(conn, vault, load_schemas(args.schemas), args.paths)
            print(f"ingested {len(summary.files_ingested)} file(s), skipped {len(summary.files_skipped)} "
                  f"already-ingested file(s); {summary.lead_rows} lead rows, {summary.events_lines} event lines "
                  f"in {time.perf_counter() - started:.2f}s")
        else:
            reports = build_reports(conn, load_catalog(args.schemas))
            for path in write_reports(reports, args.out):
                print(f"wrote {path}")
            print(f"report done in {time.perf_counter() - started:.2f}s")
    except Exception as exc:  # concise message for operators; full traceback with LEDGER_DEBUG=1
        if os.environ.get("LEDGER_DEBUG"):
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0
