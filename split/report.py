"""Write the ledger out: a CSV you can edit and a Markdown summary you can send."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from .classify import Config
from .ledger import Settlement
from .model import COLUMNS, HIS, PERSONAL, REVIEW, SHARED, Txn


def write_csv(rows: list[Txn], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for txn in rows:
            writer.writerow(txn.as_row())


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def write_summary(rows: list[Txn], s: Settlement, config: Config, path: Path) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"# Expense split — {s.period}\n")
    add(f"**{config.partner} owes {config.me}: {_money(s.net)}**\n")
    add("| | |")
    add("|---|---:|")
    add(f"| Shared expenses | {_money(s.shared_total)} |")
    add(f"| Half of shared | {_money(s.shared_total * config.default_share)} |")
    add(f"| Paid on his behalf (he owes all of it) | {_money(s.his_total)} |")
    add(f"| **{config.partner}'s share** | **{_money(s.owed)}** |")
    add(f"| Already paid back this period | −{_money(s.partner_paid)} |")
    add(f"| **Net owed** | **{_money(s.net)}** |")
    add("")
    add(f"Your personal spend (not split): {_money(s.personal_total)}  ")
    add(f"Income received (not an expense): {_money(s.income_total)}  ")
    add(f"Excluded — card payments, transfers, round-ups: {_money(s.excluded_total)}")
    add("")

    if s.review_count:
        add(f"## ⚠️ {s.review_count} transactions need your decision "
            f"({_money(s.review_total)})\n")
        add("These are **not** in the total above. Mark each `shared` or `personal` in "
            "the `split` column of the CSV, then re-run with `--ledger`.\n")
        add("| Date | Account | Description | Amount | Why |")
        add("|---|---|---|---:|---|")
        for txn in rows:
            if txn.split != REVIEW or txn.duplicate_of:
                continue
            why = txn.rule or "no rule matched"
            add(f"| {txn.date} | {txn.source} | {txn.description[:44]} | "
                f"{_money(txn.amount)} | {why} |")
            for item in txn.items[:8]:
                add(f"| | | ↳ {item[:70]} | | |")
        add("")

    if s.by_category:
        add("## Shared spending by category\n")
        add("| Category | Total | Split in half |")
        add("|---|---:|---:|")
        for name, total in sorted(s.by_category.items(), key=lambda kv: -kv[1]):
            add(f"| {name} | {_money(total)} | {_money(total * config.default_share)} |")
        add("")

    add("## Shared line items\n")
    add("| Date | Account | Description | Amount | His share |")
    add("|---|---|---|---:|---:|")
    for txn in rows:
        if txn.split not in (SHARED, HIS) or txn.duplicate_of:
            continue
        tag = " *(his)*" if txn.split == HIS else ""
        add(f"| {txn.date} | {txn.source} | {txn.description[:44]}{tag} | "
            f"{_money(txn.amount)} | {_money(txn.owed)} |")
        if txn.note:
            add(f"| | | ↳ _{txn.note[:80]}_ | | |")

    text = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return text
