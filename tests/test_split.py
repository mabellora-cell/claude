"""Regression tests over the sample exports in tests/fixtures.

Run: python3 tests/test_split.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from split.amazon import load_orders
from split.classify import load_config
from split.ledger import build
from split.parse import load_inbox

FIXTURES = ROOT / "tests" / "fixtures"
failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


def staged_inbox(tmp: Path) -> Path:
    inbox = tmp / "inbox"
    for source in ["bofa", "capitalone", "amex", "paypal", "amazon"]:
        (inbox / source).mkdir(parents=True)
        shutil.copy(FIXTURES / f"{source}.csv", inbox / source / f"{source}.csv")
    return inbox


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inbox = staged_inbox(tmp)
        config = load_config(ROOT / "rules.toml")
        rows, s = build(
            load_inbox(inbox), load_orders(inbox / "amazon"), config,
            date(2026, 8, 1), date(2026, 8, 31),
        )
        by_desc = {t.description: t for t in rows}

        # Sign conventions survive every export format.
        check("bofa debit is positive outflow", by_desc["WHOLEFDS MKT AUSTIN"].amount, Decimal("84.12"))
        check("capitalone credit is a refund", by_desc["SEPHORA RETURN"].amount, Decimal("-64.20"))

        # A card payment out of checking is not a new expense.
        check("amex payment excluded",
              by_desc["AMEX EPAYMENT ACH PMT 240805"].split, "excluded")

        # Specificity beats file order: "LITTLE SPROUTS PRESCHOOL" contains the
        # grocery keyword "sprouts" but is a baby expense.
        preschool = by_desc["LITTLE SPROUTS PRESCHOOL"]
        check("preschool is baby, not groceries", preschool.category, "Baby")
        check("preschool is shared", preschool.split, "shared")

        # Personal spend is never split.
        check("sephora is personal", by_desc["SEPHORA 0821 AUSTIN TX"].split, "personal")

        # The same Klarna charge appears on Amex and in the PayPal export.
        klarna_paypal = [t for t in rows if t.source == "paypal" and "Klarna" in t.description]
        check("paypal klarna row is deduped", bool(klarna_paypal[0].duplicate_of), True)
        check("deduped row is not counted", klarna_paypal[0].counted, False)

        # A PayPal spend with no card behind it is real money out.
        etsy = by_desc["Etsy Seller"]
        check("unmatched paypal spend is kept", etsy.duplicate_of, "")

        # Mixed Amazon order: diapers + paper towels shared, serum personal.
        mixed = by_desc["AMAZON MKTPL*RT4YU1"]
        check("mixed amazon order is shared", mixed.split, "shared")
        check("mixed amazon order owed is proportional", mixed.owed, Decimal("34.21"))
        check("mixed amazon order lists items", len(mixed.items), 3)

        # An all-baby order splits straight down the middle.
        bottles = by_desc["AMAZON.COM*AB12CD34"]
        check("all-shared amazon order", bottles.owed, Decimal("19.08"))

        # Cancelled orders never enter the ledger.
        orders = load_orders(inbox / "amazon")
        check("cancelled order dropped", "112-1111111-2222222" in orders, False)

        # Settlement math.
        check("shared total", s.shared_total, Decimal("1536.01"))
        check("partner already paid", s.partner_paid, Decimal("600.00"))
        check("net owed", s.net, Decimal("138.51"))
        check("two rows flagged for review", s.review_count, 2)

    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
