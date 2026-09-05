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
        # Pinned fixture rules: these tests assert engine behaviour, not the
        # contents of the live rules.toml, which changes as merchants are added.
        config = load_config(FIXTURES / "rules.toml")
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
        # grocery keyword "sprouts" but is a baby expense. This is the regression
        # that a real statement surfaced.
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

        # A mixed Amazon order becomes one row per item, each decided on its
        # own: the charge itself never appears, because "$127.43 at Amazon" is
        # not a thing you can classify.
        check("parent amazon charge is gone", "AMAZON MKTPL*RT4YU1" in by_desc, False)
        lines = [t for t in rows if t.order_id == "112-4455667-8899001"]
        check("mixed order became three rows", len(lines), 3)
        check("item rows sum to the charge",
              sum((t.amount for t in lines), Decimal("0")), Decimal("127.43"))

        diapers = next(t for t in lines if "Diapers" in t.description)
        towels = next(t for t in lines if "Paper Towels" in t.description)
        serum = next(t for t in lines if "Serum" in t.description)
        check("diapers are shared", diapers.split, "shared")
        check("paper towels are shared", towels.split, "shared")
        check("serum is personal", serum.split, "personal")
        check("only the shared items are charged to him",
              diapers.owed + towels.owed + serum.owed,
              ((diapers.amount + towels.amount) / 2).quantize(Decimal("0.01")))
        check("the personal item is charged to nobody", serum.owed, Decimal("0"))

        # An all-baby order still splits down the middle, item by item.
        bottles = [t for t in rows if t.order_id == "112-9988776-5544332"]
        check("all-shared order", sum((t.owed for t in bottles), Decimal("0")),
              Decimal("19.08"))

        # Cancelled orders never enter the ledger.
        orders = load_orders(inbox / "amazon")
        check("cancelled order dropped", "112-1111111-2222222" in orders, False)

        # Settlement math. The shared pool excludes the $59 serum, which is a
        # row of its own now rather than a discount on a part-shared charge.
        check("shared total", s.shared_total, Decimal("1477.01"))
        check("partner already paid", s.partner_paid, Decimal("600.00"))
        # One cent higher than half-even rounding would give; half-up is the
        # convention for splitting a bill.
        check("net owed", s.net, Decimal("138.52"))
        check("two rows flagged for review", s.review_count, 2)

    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
