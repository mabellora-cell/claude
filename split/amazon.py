"""Amazon order history: item-level detail joined onto the card charge.

The card statement only ever says "AMAZON MKTPL*RT4YU1 $127.43" -- useless for
deciding whether that was diapers or a personal purchase. Amazon's own order
export has one row per *item*, with the product name and what that item cost.

Two things make the join awkward and both are handled here:

1. Amazon charges per *shipment*, not per order. A $127.43 charge can be three
   items out of a five-item order, so the card amount often matches no single
   order total.
2. One order can be mixed -- diapers (shared) plus a book for yourself
   (personal). So a charge is split *proportionally* by its items rather than
   being forced into one bucket.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .parse import parse_amount, parse_date, read_csv_rows, _norm_header


@dataclass
class AmazonItem:
    order_id: str
    order_date: date
    name: str
    total: Decimal
    quantity: int = 1
    card_last4: str = ""
    status: str = ""


@dataclass
class AmazonOrder:
    order_id: str
    order_date: date
    items: list[AmazonItem] = field(default_factory=list)
    grand_total: Decimal | None = None   # what the card was actually charged

    @property
    def item_total(self) -> Decimal:
        """Sum of the item prices -- the basis for splitting a mixed order."""
        return sum((i.total for i in self.items), Decimal("0"))

    @property
    def total(self) -> Decimal:
        """What to match a card charge against: the grand total when the export
        gives one (it includes tax and shipping), else the item prices."""
        return self.grand_total if self.grand_total is not None else self.item_total

    @property
    def card_last4(self) -> str:
        for item in self.items:
            if item.card_last4:
                return item.card_last4
        return ""


# Amazon's export headers have shifted over the years; accept the known variants.
_COLS = {
    "order_id": ["order id", "order number", "orderid"],
    "order_date": ["order date", "orderdate", "shipment date"],
    "name": ["product name", "title", "item name", "item"],
    "total": ["total owed", "item total", "total charged", "shipment item subtotal",
              "item price"],
    "unit_price": ["unit price", "purchase price per unit"],
    "quantity": ["quantity", "qty"],
    "payment": ["payment instrument type", "payment instrument"],
    "status": ["order status", "shipment status", "status"],
    # What actually hit the card: the subtotal plus tax and shipping.
    "grand_total": ["order grand total", "grand total", "order total"],
}


def _resolve(headers: list[str]) -> dict[str, int]:
    normed = [_norm_header(h) for h in headers]
    out: dict[str, int] = {}
    for field_name, names in _COLS.items():
        for name in names:
            if name in normed:
                out[field_name] = normed.index(name)
                break
    return out


def _last4(payment: str) -> str:
    m = re.findall(r"(\d{4})\s*$", payment.strip())
    return m[0] if m else ""


def load_orders(inbox_amazon: Path) -> dict[str, AmazonOrder]:
    """Read every Amazon order-history CSV into orders keyed by order id."""
    orders: dict[str, AmazonOrder] = {}
    for path in sorted(inbox_amazon.glob("*.csv")):
        rows = read_csv_rows(path)
        if not rows:
            continue
        cols = _resolve(rows[0])
        if "order_id" not in cols or "name" not in cols:
            continue

        def cell(row, key):
            i = cols.get(key)
            return row[i].strip() if i is not None and i < len(row) else ""

        for row in rows[1:]:
            order_id = cell(row, "order_id")
            when = parse_date(cell(row, "order_date"))
            if not order_id or when is None:
                continue

            status = cell(row, "status").lower()
            if "cancel" in status:
                continue  # never charged

            qty = int(parse_amount(cell(row, "quantity")) or 1)
            total = parse_amount(cell(row, "total"))
            if total is None:
                unit = parse_amount(cell(row, "unit_price")) or Decimal("0")
                total = unit * qty

            item = AmazonItem(
                order_id=order_id,
                order_date=when,
                name=cell(row, "name"),
                total=total,
                quantity=qty,
                card_last4=_last4(cell(row, "payment")),
                status=status,
            )
            order = orders.setdefault(order_id, AmazonOrder(order_id, when))
            order.items.append(item)
            order.order_date = min(order.order_date, when)
            grand = parse_amount(cell(row, "grand_total"))
            if grand is not None:
                order.grand_total = grand

    # An order paid entirely with rewards points never reaches a statement.
    return {k: o for k, o in orders.items() if o.total != 0}


AMAZON_MERCHANT = re.compile(r"amazon|amzn|amznmktplace|prime video|whole foods mkt", re.I)


def is_amazon_charge(description: str) -> bool:
    return bool(AMAZON_MERCHANT.search(description)) and "whole foods" not in description.lower()


def order_id_in(text: str) -> str:
    """Amex and some banks echo the order number in the extended details."""
    m = re.search(r"\b(\d{3}-\d{7}-\d{7})\b", text or "")
    return m.group(1) if m else ""


def match_charge(
    charge_date: date,
    amount: Decimal,
    orders: dict[str, AmazonOrder],
    used: set[str],
    window_days: int = 7,
    tolerance: Decimal = Decimal("0.02"),
) -> tuple[AmazonOrder | None, list[AmazonItem]]:
    """Find the order and the specific items behind one Amazon card charge.

    A match is only returned when the dollars actually add up -- to a whole
    order, to a subset of one (a partial shipment), or to several orders settled
    together. When nothing adds up we return nothing rather than the nearest
    order, because attaching the wrong items would then drive the split.
    """
    candidates = [
        o for o in orders.values()
        if abs((o.order_date - charge_date).days) <= window_days
    ]
    candidates.sort(key=lambda o: abs((o.order_date - charge_date).days))

    # 1. The charge equals a whole order. Try grand totals across every
    #    candidate before falling back to pre-tax item totals -- otherwise a
    #    charge can claim an order on its subtotal and steal it from the charge
    #    that matches its grand total exactly.
    for order in candidates:
        if order.order_id in used:
            continue
        if abs(order.total - amount) <= tolerance:
            return order, list(order.items)

    for order in candidates:
        if order.order_id in used:
            continue
        if abs(order.item_total - amount) <= tolerance:
            return order, list(order.items)

    # 2. The charge equals one shipment -- a subset of an order's items.
    for order in candidates:
        subset = _subset_summing_to(order.items, amount, tolerance)
        if subset:
            return order, subset

    # 3. One charge covering several orders placed together -- Amazon bundles
    #    same-day orders into a single settlement more often than it looks.
    from itertools import combinations

    unused = [o for o in candidates if o.order_id not in used][:8]
    for size in (2, 3):
        for combo in combinations(unused, size):
            if abs(sum((o.total for o in combo), Decimal("0")) - amount) <= tolerance:
                items: list[AmazonItem] = []
                for order in combo:
                    items.extend(order.items)
                return combo[0], items

    # No amount actually adds up. Refuse to guess: returning the nearest order
    # would attach the wrong items and then classify the charge from them.
    return None, []


def _subset_summing_to(
    items: list[AmazonItem], target: Decimal, tolerance: Decimal
) -> list[AmazonItem]:
    """Smallest set of items adding up to the charge. Orders are small; brute force is fine."""
    from itertools import combinations

    if len(items) > 12:
        return []
    for size in range(1, len(items) + 1):
        for combo in combinations(items, size):
            if abs(sum((i.total for i in combo), Decimal("0")) - target) <= tolerance:
                return list(combo)
    return []


def nearby_orders(
    charge_date: date, orders: dict[str, AmazonOrder], window_days: int = 4
) -> list[AmazonOrder]:
    """Orders placed around a charge, for showing candidates on an unmatched row."""
    near = [
        o for o in orders.values()
        if abs((o.order_date - charge_date).days) <= window_days
    ]
    near.sort(key=lambda o: abs((o.order_date - charge_date).days))
    return near[:4]
