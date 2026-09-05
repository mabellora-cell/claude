"""Normalized transaction record shared by every parser."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import date
from decimal import Decimal


# How a transaction is treated when totalling up the settlement.
SHARED = "shared"      # split between the two of you
PERSONAL = "personal"  # yours alone, ignored in the settlement
EXCLUDED = "excluded"  # not a real expense (card payments, transfers, refunds of excluded)
REVIEW = "review"      # no rule matched -- you decide before the ledger is final


@dataclass
class Txn:
    """One line of money movement, normalized across every source."""

    date: date
    source: str                 # bofa | capitalone | amex | paypal | klarna | amazon
    account: str                # human label, e.g. "Amex Blue Cash 1006"
    description: str            # cleaned merchant name
    amount: Decimal             # positive = money out, negative = refund/credit
    raw_description: str = ""
    category: str = ""
    split: str = REVIEW
    share: Decimal = Decimal("0.5")   # fraction the *other* person owes
    note: str = ""
    order_id: str = ""          # Amazon order this line belongs to, if any
    items: list[str] = field(default_factory=list)
    duplicate_of: str = ""      # txn_id of the row this one double-counts
    rule: str = ""              # which rule decided the split

    @property
    def txn_id(self) -> str:
        key = f"{self.source}|{self.account}|{self.date}|{self.amount}|{self.raw_description}"
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    @property
    def counted(self) -> bool:
        """True when this row contributes to the settlement total."""
        return self.split == SHARED and not self.duplicate_of

    @property
    def owed(self) -> Decimal:
        """What the other person owes on this row."""
        if not self.counted:
            return Decimal("0")
        return (self.amount * self.share).quantize(Decimal("0.01"))

    def as_row(self) -> dict:
        d = asdict(self)
        d["txn_id"] = self.txn_id
        d["date"] = self.date.isoformat()
        d["amount"] = f"{self.amount:.2f}"
        d["share"] = f"{self.share}"
        d["owed"] = f"{self.owed:.2f}"
        d["items"] = " | ".join(self.items)
        return d


COLUMNS = [
    "date", "source", "account", "description", "amount", "split", "share",
    "owed", "category", "rule", "items", "order_id", "note", "duplicate_of",
    "raw_description", "txn_id",
]


_CLEAN_PATTERNS = [
    r"\s+\d{2}/\d{2}\b",              # trailing transaction dates
    r"\b[A-Z]{2}\s*\d{5}(-\d{4})?\b",  # state + zip
    r"\bX{2,}\d{3,}\b",                # masked card numbers
    r"\b\d{10,}\b",                    # long reference numbers
    r"#\s?\d{4,}",
]


def clean_description(raw: str) -> str:
    """Strip the noise banks append so the same merchant matches month to month."""
    s = raw.strip()
    for pat in _CLEAN_PATTERNS:
        s = re.sub(pat, " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -*").strip()
