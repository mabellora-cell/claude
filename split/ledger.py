"""Assemble the month's ledger: enrich, de-duplicate, classify, total."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from .amazon import AmazonOrder, is_amazon_charge, load_orders, match_charge, order_id_in
from .classify import Config, classify, classify_items, is_partner_payment
from .model import EXCLUDED, PERSONAL, REVIEW, SHARED, Txn

# Sources that represent money actually leaving an account you control.
MONEY_SOURCES = {"bofa", "capitalone", "amex"}
# Sources that mostly *explain* a charge already captured above.
DETAIL_SOURCES = {"paypal", "klarna", "amazon"}

_WALLET = re.compile(r"paypal|klarna|afterpay|affirm", re.I)


@dataclass
class Settlement:
    period: str
    shared_total: Decimal = Decimal("0")
    personal_total: Decimal = Decimal("0")
    excluded_total: Decimal = Decimal("0")
    review_total: Decimal = Decimal("0")
    partner_paid: Decimal = Decimal("0")
    owed: Decimal = Decimal("0")
    net: Decimal = Decimal("0")
    review_count: int = 0
    by_category: dict[str, Decimal] = field(default_factory=dict)


def in_period(txn: Txn, start: date | None, end: date | None) -> bool:
    if start and txn.date < start:
        return False
    if end and txn.date > end:
        return False
    return True


def build(
    txns: list[Txn],
    orders: dict[str, AmazonOrder],
    config: Config,
    start: date | None = None,
    end: date | None = None,
) -> tuple[list[Txn], Settlement]:
    rows = [t for t in txns if in_period(t, start, end)]
    rows.sort(key=lambda t: (t.date, t.source, t.description))

    _dedupe_wallets(rows)
    _enrich_amazon(rows, orders, config)

    for txn in rows:
        if txn.split == REVIEW and not txn.rule:  # untouched by Amazon enrichment
            classify(txn, config)

    settlement = _total(rows, config, start, end)
    return rows, settlement


def _dedupe_wallets(rows: list[Txn]) -> None:
    """A PayPal or Klarna charge on your card and the PayPal/Klarna export are
    the same dollars. Keep the card row (that's the real money movement) but
    take the merchant name from the wallet export, then mark the wallet row as
    a duplicate so it isn't counted twice."""
    money = [t for t in rows if t.source in MONEY_SOURCES]
    for wallet in rows:
        if wallet.source not in ("paypal", "klarna") or wallet.duplicate_of:
            continue
        for card in money:
            if card.duplicate_of or not _WALLET.search(card.description):
                continue
            if abs(card.amount - wallet.amount) > Decimal("0.01"):
                continue
            if abs((card.date - wallet.date).days) > 3:
                continue
            wallet.duplicate_of = card.txn_id
            wallet.split = EXCLUDED
            wallet.rule = "wallet duplicate"
            wallet.note = f"same charge as {card.source} {card.date}"
            card.description = f"{wallet.description} (via {wallet.source})"
            card.note = (card.note + f" detail from {wallet.source}").strip()
            break
        else:
            # No card charge behind it -- funded by PayPal balance or a linked
            # bank account, so it is real money out and must be counted.
            wallet.note = (wallet.note + " no matching card charge; counted as its own spend").strip()


def _enrich_amazon(rows: list[Txn], orders: dict[str, AmazonOrder], config: Config) -> None:
    """Attach item detail to Amazon charges and split mixed orders proportionally."""
    if not orders:
        return
    used: set[str] = set()
    for txn in rows:
        if txn.duplicate_of or txn.source not in MONEY_SOURCES:
            continue
        if not is_amazon_charge(txn.description) and not is_amazon_charge(txn.raw_description):
            continue

        explicit = order_id_in(txn.raw_description)
        order = orders.get(explicit) if explicit else None
        items = list(order.items) if order else []
        if order is None:
            order, items = match_charge(txn.date, txn.amount, orders, used)

        if order is None:
            txn.split = REVIEW
            txn.rule = "amazon: no matching order"
            txn.note = "Amazon charge with no order in the export -- check the date range"
            continue

        used.add(order.order_id)
        txn.order_id = order.order_id
        if not items:
            items = list(order.items)
            txn.note = (
                f"charge ${txn.amount:.2f} does not match order total "
                f"${order.total:.2f} (partial shipment) -- confirm the items"
            )

        shared, personal, unknown, labels = classify_items(items, config)
        txn.items = labels
        total = shared + personal + unknown

        if unknown > 0 or total == 0:
            txn.split = REVIEW
            txn.rule = "amazon: items need a decision"
            txn.share = config.default_share
        elif personal == 0:
            txn.split = SHARED
            txn.rule = "amazon: all items shared"
            txn.category = "Amazon"
            txn.share = config.default_share
        elif shared == 0:
            txn.split = PERSONAL
            txn.rule = "amazon: all items personal"
            txn.category = "Amazon"
        else:
            # Mixed order: he owes half of only the shared portion of this charge.
            fraction = shared / total
            txn.split = SHARED
            txn.category = "Amazon"
            txn.rule = "amazon: mixed order, split proportionally"
            txn.share = (config.default_share * fraction).quantize(Decimal("0.0001"))
            txn.note = (
                f"${shared:.2f} of ${total:.2f} is shared "
                f"({fraction * 100:.0f}% of this charge)"
            )


def _total(rows: list[Txn], config: Config, start, end) -> Settlement:
    period = f"{start or 'start'} to {end or 'end'}"
    s = Settlement(period=period)

    for txn in rows:
        if txn.duplicate_of:
            continue
        if is_partner_payment(txn, config) and txn.amount < 0:
            s.partner_paid += -txn.amount
            continue
        if txn.split == SHARED:
            s.shared_total += txn.amount
            key = txn.category or "Uncategorized"
            s.by_category[key] = s.by_category.get(key, Decimal("0")) + txn.amount
            s.owed += txn.owed
        elif txn.split == PERSONAL:
            s.personal_total += txn.amount
        elif txn.split == REVIEW:
            s.review_total += txn.amount
            s.review_count += 1
        else:
            s.excluded_total += txn.amount

    s.owed = s.owed.quantize(Decimal("0.01"))
    s.net = (s.owed - s.partner_paid).quantize(Decimal("0.01"))
    return s
