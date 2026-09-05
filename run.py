#!/usr/bin/env python3
"""Build the monthly expense split.

    python3 run.py --month 2026-08

Reads every CSV under inbox/, applies rules.toml, and writes
out/ledger-<month>.csv and out/summary-<month>.md.

After reviewing the flagged rows, edit the `split` column in the ledger CSV and
re-run with --ledger out/ledger-2026-08.csv to recompute the totals from your
decisions.
"""

from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from split.amazon import load_orders
from split.classify import load_config
from split.ledger import build
from split.model import COLUMNS, Txn
from split.parse import load_inbox, parse_date
from split.report import write_csv, write_summary

ROOT = Path(__file__).parent


def month_bounds(month: str) -> tuple[date, date]:
    start = datetime.strptime(month, "%Y-%m").date()
    last = calendar.monthrange(start.year, start.month)[1]
    return start, start.replace(day=last)


def load_reviewed(path: Path) -> list[Txn]:
    """Re-read a ledger CSV after you've filled in the `split` column."""
    import csv

    rows: list[Txn] = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            rows.append(
                Txn(
                    date=parse_date(row["date"]),
                    source=row["source"],
                    account=row["account"],
                    description=row["description"],
                    amount=Decimal(row["amount"]),
                    raw_description=row["raw_description"],
                    category=row["category"],
                    split=row["split"].strip().lower(),
                    share=Decimal(row["share"]),
                    note=row["note"],
                    order_id=row["order_id"],
                    items=[i for i in row["items"].split(" | ") if i],
                    duplicate_of=row["duplicate_of"],
                    rule=row["rule"],
                )
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month", help="YYYY-MM to settle, e.g. 2026-08")
    ap.add_argument("--from", dest="start", help="start date YYYY-MM-DD")
    ap.add_argument("--to", dest="end", help="end date YYYY-MM-DD")
    ap.add_argument("--inbox", default="inbox", help="folder holding the exports")
    ap.add_argument("--out", default="out", help="output folder")
    ap.add_argument("--rules", default="rules.toml")
    ap.add_argument("--ledger", help="recompute from an already-reviewed ledger CSV")
    args = ap.parse_args(argv)

    if args.month:
        start, end = month_bounds(args.month)
        label = args.month
    else:
        start = parse_date(args.start) if args.start else None
        end = parse_date(args.end) if args.end else None
        label = f"{start or 'all'}_{end or 'all'}"

    config = load_config(ROOT / args.rules)
    inbox = ROOT / args.inbox

    if args.ledger:
        txns = load_reviewed(Path(args.ledger))
        orders = {}
    else:
        if not inbox.exists():
            print(f"No inbox at {inbox}", file=sys.stderr)
            return 1
        txns = load_inbox(inbox)
        orders = load_orders(inbox / "amazon")

    if not txns:
        print("No transactions found. Drop your CSV exports into inbox/<source>/.")
        return 1

    rows, settlement = build(txns, orders, config, start, end)

    out = ROOT / args.out
    ledger_path = out / f"ledger-{label}.csv"
    summary_path = out / f"summary-{label}.md"
    write_csv(rows, ledger_path)
    text = write_summary(rows, settlement, config, summary_path)

    print(text)
    print(f"\nWrote {ledger_path}\nWrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
