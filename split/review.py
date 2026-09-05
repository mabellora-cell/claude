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
from .model import REVIEW, Txn

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
    rows: list[Txn],
    settlement: Settlement,
    config: Config,
    period_label: str,
    sample_note: str = "",
) -> str:
    visible = [t for t in rows if not t.duplicate_of]
    pretty = pretty_period(period_label)
    payload = {
        "meta": {
            "title": pretty,
            "period": f"{config.me} and {config.partner} · what to split",
            "me": config.me,
            "partner": config.partner,
            "default_share": float(config.default_share),
            "sample_note": sample_note,
            "footer": (
                f"Amounts are what left your accounts. Rows marked Skip are card payments, "
                f"transfers, or charges already counted from another statement, so they never "
                f"reach the total. Amazon orders split by their items: if half the dollars in "
                f"the box are shared, half the charge is. "
                f"{len(rows) - len(visible)} duplicate rows were removed before this page."
            ),
        },
        "txns": [_txn_payload(t, config) for t in visible],
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
            txn.split, fraction = _from_items(item_splits, txn.items)
            txn.share = (config.default_share * fraction).quantize(Decimal("0.0001"))
        else:
            new = decision.get("split")
            if new:
                txn.split = new
                txn.share = config.default_share
        txn.rule = (txn.rule + " · confirmed by you").strip(" ·")
        changed += 1
    return changed


def _from_items(splits: list[str], labels: list[str]) -> tuple[str, Decimal]:
    """Derive a charge's split and shared fraction from its item decisions."""
    total = Decimal("0")
    shared = Decimal("0")
    for bucket, label in zip(splits, labels):
        _, _, amount = label.rpartition(" $")
        try:
            value = Decimal(amount)
        except Exception:
            value = Decimal("0")
        total += value
        if bucket == "shared":
            shared += value
    if total == 0:
        return ("shared" if "shared" in splits else "personal"), Decimal("1")
    if shared == total:
        return "shared", Decimal("1")
    if shared == 0:
        return "personal", Decimal("0")
    return "shared", (shared / total)
