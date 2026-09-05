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
import json
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
from split.review import apply_decisions, build_page

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
    ap.add_argument("--review", action="store_true",
                    help="also write the interactive review page to out/review-<month>.html")
    ap.add_argument("--also-month", action="append",
                    help="include another month in the review page (repeatable)")
    ap.add_argument("--sample-note", help="banner text marking the page as example data")
    ap.add_argument("--decisions",
                    help="JSON of decisions exported from the review page, folded in "
                         "before totalling")
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
        checks: list[dict] = []
        txns = load_inbox(inbox, checks)
        orders = load_orders(inbox / "amazon")
        for c in checks:
            status = "reconciles" if c["charges_ok"] and c["credits_ok"] else "DOES NOT RECONCILE"
            print(f"{c['file']}: {c['rows']} rows, {status} against the statement totals")
        if checks:
            print()

    if not txns:
        print("No transactions found. Drop your CSV exports into inbox/<source>/.")
        return 1

    rows, settlement = build(txns, orders, config, start, end)

    if args.decisions:
        decisions = json.loads(Path(args.decisions).read_text())
        applied = apply_decisions(rows, decisions, config)
        rows, settlement = _retotal(rows, config, start, end)
        print(f"Applied {applied} decisions from {args.decisions}\n")

    out = ROOT / args.out
    ledger_path = out / f"ledger-{label}.csv"
    summary_path = out / f"summary-{label}.md"
    write_csv(rows, ledger_path)
    text = write_summary(rows, settlement, config, summary_path)

    print(text)
    print(f"\nWrote {ledger_path}\nWrote {summary_path}")

    if args.review:
        # Every month named with --also-month ships in the same page, so the
        # artifact is the settlement record rather than a view onto one run.
        periods = [(label, rows)]
        for extra in args.also_month or []:
            e_start, e_end = month_bounds(extra)
            e_rows, _ = build(txns, orders, config, e_start, e_end)
            periods.append((extra, e_rows))
        periods.sort(key=lambda p: p[0])

        page = build_page(periods, config, args.sample_note or "")
        (out / f"review-{label}.html").write_text(page)
        # Stable path so re-publishing updates the same artifact URL.
        (out / "review.html").write_text(page)
        months = ", ".join(p[0] for p in periods)
        print(f"Wrote {out / 'review.html'} covering {months}")
    return 0


def _retotal(rows, config, start, end):
    """Re-run the totalling pass over rows whose splits you just changed."""
    from split.ledger import _total
    return rows, _total(rows, config, start, end)


if __name__ == "__main__":
    raise SystemExit(main())
