"""Assemble the month's ledger: enrich, de-duplicate, classify, total."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from .amazon import (AmazonOrder, is_amazon_charge, load_orders, match_charge,
                     nearby_orders, order_id_in)
from .classify import Config, classify, classify_item, is_partner_payment
from .model import EXCLUDED, HIS, PERSONAL, REVIEW, SHARED, Txn

# Sources that represent money actually leaving an account you control.
MONEY_SOURCES = {"bofa", "capitalone", "amex"}
# Sources that mostly *explain* a charge already captured above.
DETAIL_SOURCES = {"paypal", "klarna", "amazon"}

_WALLET = re.compile(r"paypal|klarna|afterpay|affirm", re.I)


@dataclass
class Settlement:
    period: str
    shared_total: Decimal = Decimal("0")
    his_total: Decimal = Decimal("0")
    personal_total: Decimal = Decimal("0")
    excluded_total: Decimal = Decimal("0")
    review_total: Decimal = Decimal("0")
    partner_paid: Decimal = Decimal("0")
    income_total: Decimal = Decimal("0")
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
    rows = _explode_amazon(rows, orders, config)

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


def _explode_amazon(
    rows: list[Txn], orders: dict[str, AmazonOrder], config: Config
) -> list[Txn]:
    """Replace each matched Amazon charge with one row per item purchased.

    A single "AMAZON MKTPL*5N2WE6U90 $80.53" line cannot be classified, because
    one box routinely holds nappies and a face serum. Once the order is known,
    the item is the real transaction, so each becomes its own row with its own
    decision.

    Item prices exclude tax and shipping, so allocating them verbatim would lose
    money against the bank. Each item is scaled by charge / item-total and the
    last line absorbs the rounding, so the rows still sum to exactly what left
    the account.
    """
    if not orders:
        return rows

    used: set[str] = set()
    out: list[Txn] = []
    for txn in rows:
        if txn.duplicate_of or txn.source not in MONEY_SOURCES:
            out.append(txn)
            continue
        if not is_amazon_charge(txn.description) and not is_amazon_charge(txn.raw_description):
            out.append(txn)
            continue
        if txn.amount <= 0:  # a refund has no item detail to explode
            out.append(txn)
            continue

        explicit = order_id_in(txn.raw_description)
        order = orders.get(explicit) if explicit else None
        items = list(order.items) if order else []
        if order is None:
            order, items = match_charge(txn.date, txn.amount, orders, used)

        if order is None or not items:
            txn.split = REVIEW
            txn.rule = "amazon: no order matches this amount"
            near = nearby_orders(txn.date, orders)
            if near:
                options = "; ".join(
                    f"{o.order_date} ${o.total:.2f} {o.items[0].name[:34]}" for o in near
                )
                txn.note = f"nearest orders: {options}"
            else:
                txn.note = "no Amazon order near this date in the export"
            out.append(txn)
            continue

        used.add(order.order_id)
        out.extend(_item_rows(txn, order, items, config))

    return out


def _item_rows(
    txn: Txn, order: AmazonOrder, items: list, config: Config
) -> list[Txn]:
    """One row per item, together summing to exactly the charge."""
    item_total = sum((i.total for i in items), Decimal("0"))
    if item_total <= 0:
        return [txn]

    lines: list[Txn] = []
    allocated = Decimal("0")
    for index, item in enumerate(items):
        if index == len(items) - 1:
            amount = txn.amount - allocated       # last line absorbs rounding
        else:
            share = (item.total / item_total) * txn.amount
            amount = share.quantize(Decimal("0.01"))
            allocated += amount

        split, category, rule = classify_item(item.name, config)
        note = f"1 of {len(items)} items on a ${txn.amount:.2f} Amazon charge"
        if amount != item.total:
            note += f" (listed ${item.total:.2f}, plus its share of tax and shipping)"

        lines.append(
            Txn(
                date=txn.date,
                source=txn.source,
                account=txn.account,
                description=item.name,
                raw_description=txn.raw_description,
                amount=amount,
                category=category,
                split=split,
                share=Decimal("1") if split == HIS else config.default_share,
                note=note,
                order_id=order.order_id,
                line_id=f"{order.order_id}#{index}",
                parent_amount=txn.amount,
                rule=f"amazon item: {rule}" if rule else "amazon item: needs a decision",
            )
        )
    return lines


def _total(rows: list[Txn], config: Config, start, end) -> Settlement:
    period = f"{start or 'start'} to {end or 'end'}"
    s = Settlement(period=period)

    for txn in rows:
        if txn.duplicate_of:
            continue
        if is_partner_payment(txn, config) and txn.amount < 0:
            s.partner_paid += -txn.amount
            continue
        if txn.category == "Income":
            s.income_total += -txn.amount
            continue
        if txn.split == SHARED:
            s.shared_total += txn.amount
            key = txn.category or "Uncategorized"
            s.by_category[key] = s.by_category.get(key, Decimal("0")) + txn.amount
            s.owed += txn.owed
        elif txn.split == HIS:
            s.his_total += txn.amount
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
