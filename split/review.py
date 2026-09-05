"""Generate the interactive review page from a month's ledger.

The page is published as an Artifact with the `db` capability, so the choices
you tick are saved server-side and can be read back here to rebuild the ledger.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from .classify import Config, is_partner_payment
from .ledger import Settlement
from .model import HIS, PERSONAL, REVIEW, SHARED, Txn

TEMPLATE = Path(__file__).parent / "review_template.html"


def _txn_payload(txn: Txn, config: Config) -> dict:
    items = []
    for label in txn.items:
        # Items are stored as "[bucket] Product name $12.34" by the classifier.
        bucket, _, rest = label.partition("] ")
        bucket = bucket.lstrip("[") or REVIEW
        name, _, amount = rest.rpartition(" $")
        try:
            value = float(amount)
        except ValueError:
            name, value = rest, 0.0
        items.append({"name": name.strip(), "amount": value, "split": bucket})

    return {
        "id": txn.txn_id,
        "date": txn.date.isoformat(),
        "source": txn.source,
        "account": txn.account,
        "desc": txn.description,
        "raw": txn.raw_description,
        "amount": float(txn.amount),
        "split": txn.split,
        "category": txn.category,
        "rule": txn.rule,
        "note": txn.note,
        "order_id": txn.order_id,
        "items": items,
        "is_payment": is_partner_payment(txn, config) and txn.amount < 0,
    }


def pretty_period(label: str) -> str:
    """2026-08 -> August 2026; anything else passes through unchanged."""
    try:
        year, month = label.split("-")
        names = ["January", "February", "March", "April", "May", "June", "July",
                 "August", "September", "October", "November", "December"]
        return f"{names[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return label


def build_page(
    months: list[tuple[str, list[Txn]]],
    config: Config,
    sample_note: str = "",
) -> str:
    """Render the review page over one or more months.

    `months` is [(period_label, rows), ...]. All months ship in the page so it
    works as the settlement record itself, not a view onto one run.
    """
    dropped = 0
    payload_months = []
    for label, rows in months:
        visible = [t for t in rows if not t.duplicate_of]
        dropped += len(rows) - len(visible)
        payload_months.append({
            "label": label,
            "pretty": pretty_period(label),
            "txns": [_txn_payload(t, config) for t in visible],
        })

    payload = {
        "meta": {
            "me": config.me,
            "partner": config.partner,
            "default_share": float(config.default_share),
            "sample_note": sample_note,
            "footer": (
                "Amounts are what left your accounts. Skip covers card payments, "
                "transfers, round-ups and charges already counted from another "
                "statement, so they never reach the total. Amazon charges split by "
                "their items. "
                f"{dropped} duplicate rows were removed before this page."
            ),
        },
        "months": payload_months,
    }

    encoded = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.read_text().replace("__LEDGER_JSON__", encoded)


def apply_decisions(rows: list[Txn], decisions: dict[str, dict], config: Config) -> int:
    """Fold decisions read back from the artifact store into the ledger.

    `decisions` maps txn_id -> {"split": str, "items": [str, ...]}. Returns how
    many rows changed, so the caller can report it.
    """
    changed = 0
    for txn in rows:
        decision = decisions.get(txn.txn_id)
        if not decision:
            continue

        item_splits = decision.get("items") or []
        if item_splits and len(item_splits) == len(txn.items):
            # Re-label the stored items with your choices, then let the shared
            # fraction of the box decide the share of the charge.
            txn.items = [
                f"[{bucket}] {label.partition('] ')[2]}"
                for bucket, label in zip(item_splits, txn.items)
            ]
            txn.split, txn.share = _from_items(item_splits, txn.items, config.default_share)
        else:
            new = decision.get("split")
            if new:
                txn.split = new
                txn.share = Decimal("1") if new == HIS else config.default_share
        txn.rule = (txn.rule + " · confirmed by you").strip(" ·")
        changed += 1
    return changed


def _from_items(
    splits: list[str], labels: list[str], default_share: Decimal
) -> tuple[str, Decimal]:
    """Derive a charge's split and its share from the decisions on its items.

    Shared dollars count at the default share, dollars you fronted for him count
    in full, so one Amazon box can legitimately hold both.
    """
    total = shared = his = Decimal("0")
    for bucket, label in zip(splits, labels):
        _, _, amount = label.rpartition(" $")
        try:
            value = Decimal(amount)
        except Exception:
            value = Decimal("0")
        total += value
        if bucket == SHARED:
            shared += value
        elif bucket == HIS:
            his += value

    if total == 0:
        return (SHARED if SHARED in splits else PERSONAL), default_share
    if his == total:
        return HIS, Decimal("1")
    if shared == total:
        return SHARED, default_share
    if shared == 0 and his == 0:
        return PERSONAL, Decimal("0")
    share = (default_share * shared + his) / total
    return SHARED, share.quantize(Decimal("0.0001"))
