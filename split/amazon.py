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

    @property
    def total(self) -> Decimal:
        return sum((i.total for i in self.items), Decimal("0"))

    @property
    def card_last4(self) -> str:
        for item in self.items:
            if item.card_last4:
                return item.card_last4
        return ""


# Amazon's export headers have shifted over the years; accept the known variants.
_COLS = {
    "order_id": ["order id", "orderid"],
    "order_date": ["order date", "orderdate", "shipment date"],
    "name": ["product name", "title", "item name"],
    "total": ["total owed", "item total", "total charged", "shipment item subtotal"],
    "unit_price": ["unit price", "purchase price per unit"],
    "quantity": ["quantity", "qty"],
    "payment": ["payment instrument type", "payment instrument"],
    "status": ["order status", "shipment status"],
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
                continue

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
    return orders


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
    """Find the order (and the specific items) behind one Amazon card charge.

    Returns the order plus the items that add up to this charge. An empty item
    list means we identified the order but not which shipment -- the caller
    should surface the whole order for you to eyeball.
    """
    candidates = [
        o for o in orders.values()
        if abs((o.order_date - charge_date).days) <= window_days
    ]
    candidates.sort(key=lambda o: abs((o.order_date - charge_date).days))

    # 1. The charge equals a whole order.
    for order in candidates:
        if order.order_id in used:
            continue
        if abs(order.total - amount) <= tolerance:
            return order, list(order.items)

    # 2. The charge equals one shipment -- a subset of an order's items.
    for order in candidates:
        subset = _subset_summing_to(order.items, amount, tolerance)
        if subset:
            return order, subset

    # 3. Closest order by date, items unresolved.
    for order in candidates:
        if order.order_id not in used:
            return order, []
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
