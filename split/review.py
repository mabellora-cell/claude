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
        "line_id": txn.line_id,
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


def apply_decisions(
    rows: list[Txn], decisions: dict[str, dict], config: Config
) -> int:
    """Fold decisions read back from the artifact store into the ledger."""
    changed = 0
    for txn in rows:
        decision = decisions.get(txn.txn_id)
        if not decision:
            continue
        new = decision.get("split")
        if new:
            txn.split = new
            txn.share = Decimal("1") if new == HIS else config.default_share
            txn.rule = (txn.rule + " · confirmed by you").strip(" ·")
            changed += 1
    return changed
