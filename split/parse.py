"""Read whatever CSV each institution hands you and normalize it.

Every export has a different header, a different sign convention, and sometimes
a few junk lines before the real header. Rather than hard-coding one layout per
bank, we match headers by alias and let a per-source spec say which sign means
"money left my account".
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .model import Txn, clean_description

# Header aliases, lowercased and stripped of punctuation.
ALIASES = {
    "date": [
        "date", "transaction date", "trans date", "posted date", "post date",
        "date posted", "order date", "purchase date",
    ],
    "post_date": ["posted date", "post date", "date posted"],
    "description": [
        "description", "merchant description", "payee", "name", "merchant",
        "merchant name", "details", "transaction description",
        "original description", "product name", "item",
    ],
    "amount": ["amount", "gross", "transaction amount", "amount usd", "total owed"],
    "debit": ["debit", "withdrawal", "withdrawals", "charges", "amount debit"],
    "credit": ["credit", "deposit", "deposits", "payments", "amount credit"],
    "category": ["category", "type", "transaction type"],
    "card": ["card no", "card no.", "account #", "account number", "card member", "last 4"],
    "currency": ["currency", "currency code"],
    "memo": ["memo", "notes", "extended details", "appears on your statement as"],
    # Some ledgers say outright which rows are real spend.
    "counted": ["counted in spend total", "counted", "count in total"],
}


class SourceSpec:
    """Per-institution quirks."""

    def __init__(self, source: str, account: str, outflow_sign: int,
                 skip_types: tuple[str, ...] = ()):
        self.source = source
        self.account = account
        # -1: the export writes purchases as negative numbers (bank + most cards)
        # +1: the export writes purchases as positive numbers (Amex, Klarna)
        self.outflow_sign = outflow_sign
        # Row types that record something rather than move money. A PayPal
        # "Pay in 4 - Purchase" row is the headline price of a financed
        # purchase; the money actually leaves in the installment rows, so
        # counting both would double the purchase.
        self.skip_types = skip_types


SPECS = {
    "bofa": SourceSpec("bofa", "Bank of America", -1),
    "capitalone": SourceSpec("capitalone", "Capital One", -1),
    "amex": SourceSpec("amex", "American Express", +1),
    "paypal": SourceSpec("paypal", "PayPal", -1, skip_types=("pay in 4 - purchase",)),
    "klarna": SourceSpec("klarna", "Klarna", +1),
}


def _norm_header(h: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", h.strip().lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _map_headers(headers: list[str]) -> dict[str, int]:
    """Map canonical field -> column index."""
    normed = [_norm_header(h) for h in headers]
    mapping: dict[str, int] = {}
    for field, names in ALIASES.items():
        for name in names:
            if name in normed:
                mapping[field] = normed.index(name)
                break
    return mapping


def _find_header_row(rows: list[list[str]]) -> int:
    """Some exports (BofA checking, Klarna) put a summary block above the header."""
    for i, row in enumerate(rows[:25]):
        mapping = _map_headers(row)
        if "date" in mapping and ("amount" in mapping or "debit" in mapping):
            return i
    return 0


DATE_FORMATS = [
    "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y",
    "%m-%d-%Y", "%Y/%m/%d",
]


def parse_date(value: str) -> date | None:
    v = (value or "").strip()
    if not v:
        return None
    v = v.split("T")[0].strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(value: str) -> Decimal | None:
    v = (value or "").strip()
    if not v:
        return None
    negative = v.startswith("(") and v.endswith(")")
    v = re.sub(r"[^0-9.\-]", "", v)
    if v in ("", "-", "."):
        return None
    try:
        amount = Decimal(v)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def read_csv_rows(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]


def parse_file(path: Path, source: str) -> list[Txn]:
    """Turn one exported CSV into normalized transactions."""
    spec = SPECS.get(source) or SourceSpec(source, source.title(), -1)
    rows = read_csv_rows(path)
    if not rows:
        return []

    header_idx = _find_header_row(rows)
    headers = rows[header_idx]
    mapping = _map_headers(headers)
    if "date" not in mapping:
        raise ValueError(f"{path.name}: could not find a date column in {headers!r}")

    def cell(row: list[str], field: str) -> str:
        idx = mapping.get(field)
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    account = f"{spec.account} ({path.stem})"
    txns: list[Txn] = []
    for row in rows[header_idx + 1:]:
        when = parse_date(cell(row, "date"))
        if when is None:
            continue  # footer/summary line

        # An explicit "is this real spend" column beats any inference.
        counted = cell(row, "counted").strip().lower()
        if counted in ("false", "no", "0", "n"):
            continue

        row_type = cell(row, "category").strip().lower()
        if any(skip in row_type for skip in spec.skip_types):
            continue

        amount = _row_amount(row, mapping, cell, spec)
        if amount is None:
            continue

        raw = cell(row, "description") or cell(row, "memo")
        memo = cell(row, "memo")
        txns.append(
            Txn(
                date=when,
                source=source,
                account=account,
                description=clean_description(raw),
                raw_description=raw if not memo or memo == raw else f"{raw} :: {memo}",
                amount=amount,
                category=cell(row, "category"),
            )
        )
    return txns


def _row_amount(row, mapping, cell, spec: SourceSpec) -> Decimal | None:
    """Resolve an outflow-positive amount from either Amount or Debit/Credit."""
    if "debit" in mapping or "credit" in mapping:
        debit = parse_amount(cell(row, "debit"))
        credit = parse_amount(cell(row, "credit"))
        if debit:
            return abs(debit)
        if credit:
            return -abs(credit)
        return None

    amount = parse_amount(cell(row, "amount"))
    if amount is None:
        return None
    return amount * spec.outflow_sign


def load_inbox(inbox: Path, verifications: list | None = None) -> list[Txn]:
    """Parse every export in inbox/<source>/ (Amazon is handled separately).

    Amex statement PDFs are read too, since Amex's CSV is not always available.
    Each PDF reports whether its rows reconcile to the statement's own totals;
    those reports are appended to `verifications` when one is passed in.
    """
    txns: list[Txn] = []
    for source_dir in sorted(inbox.iterdir()):
        if not source_dir.is_dir() or source_dir.name == "amazon":
            continue
        for path in sorted(source_dir.glob("*.csv")):
            txns.extend(parse_file(path, source_dir.name))
        for path in sorted(source_dir.glob("*.pdf")):
            from .amex_pdf import parse_amex_pdf

            rows, verification = parse_amex_pdf(path)
            txns.extend(rows)
            if verifications is not None:
                verifications.append(verification)
    return txns
