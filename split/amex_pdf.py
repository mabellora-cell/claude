"""Read an Amex statement PDF.

Amex's CSV export is better, but the statement PDF is what most people can
actually get hold of. The layout is stable:

    07/13/26 MACYS DADELAND 000000776 MIAMI FL          $197.95
    8002896229                                     <- descriptor line

Every statement states its own "Total New Charges" and "Total Payments and
Credits". We parse those too and check our extracted rows add up to them, so a
silent extraction failure becomes a loud error instead of a wrong ledger.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .model import Txn, clean_description

# 07/13/26 MERCHANT CITY ST $197.95   (a * marks a posting date)
LINE = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{2})\*?\s+(?P<desc>.+?)\s+(?P<sign>-?)\$(?P<amt>[\d,]+\.\d{2})\s*$"
)
TOTAL_CHARGES = re.compile(r"Total New Charges\s+\$([\d,]+\.\d{2})")
TOTAL_CREDITS = re.compile(r"Total Payments and Credits\s+-?\$([\d,]+\.\d{2})")
ACCOUNT = re.compile(r"Account Ending\s*([\w-]+)")


def _money(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def parse_amex_pdf(path: Path) -> tuple[list[Txn], dict]:
    """Return (transactions, verification) for one Amex statement PDF."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    text = "\n".join(pages)

    account_match = ACCOUNT.search(text)
    account = f"Amex {account_match.group(1)}" if account_match else "American Express"

    lines = text.split("\n")
    txns: list[Txn] = []
    for i, line in enumerate(lines):
        m = LINE.match(line.strip())
        if not m:
            continue
        try:
            when = datetime.strptime(m.group("date"), "%m/%d/%y").date()
        except ValueError:
            continue

        amount = _money(m.group("amt"))
        if m.group("sign") == "-":
            amount = -amount  # a payment or credit reduces the balance

        desc = m.group("desc").strip()
        # The line below a charge carries a phone number or merchant category.
        memo = ""
        if i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and not LINE.match(nxt) and len(nxt) < 60 and not nxt.startswith("Total"):
                memo = nxt

        txns.append(
            Txn(
                date=when,
                source="amex",
                account=account,
                description=clean_description(desc),
                raw_description=f"{desc} :: {memo}" if memo else desc,
                amount=amount,
            )
        )

    charges = sum((t.amount for t in txns if t.amount > 0), Decimal("0"))
    credits = sum((-t.amount for t in txns if t.amount < 0), Decimal("0"))
    stated_charges = _money(TOTAL_CHARGES.search(text).group(1)) if TOTAL_CHARGES.search(text) else None
    stated_credits = _money(TOTAL_CREDITS.search(text).group(1)) if TOTAL_CREDITS.search(text) else None

    verification = {
        "file": path.name,
        "account": account,
        "rows": len(txns),
        "charges": charges,
        "stated_charges": stated_charges,
        "charges_ok": stated_charges is None or charges == stated_charges,
        "credits": credits,
        "stated_credits": stated_credits,
        "credits_ok": stated_credits is None or credits == stated_credits,
    }
    return txns, verification
