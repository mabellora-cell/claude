"""Apply your rules to normalized transactions."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .model import EXCLUDED, HIS, REVIEW, SHARED, Txn


@dataclass
class Rule:
    name: str
    split: str
    category: str
    patterns: list[re.Pattern]
    share: Decimal | None = None

    def match_strength(self, text: str) -> int:
        """Length of the longest span this rule matches, or 0 for no match.

        Rules are scored rather than taken in file order, because merchant names
        overlap: "LITTLE SPROUTS PRESCHOOL" contains the grocery keyword
        "sprouts", but "little sprouts" and "preschool" are longer and more
        specific, so the baby rule wins regardless of file order.
        """
        best = 0
        for pattern in self.patterns:
            for m in pattern.finditer(text):
                best = max(best, len(m.group(0)))
        return best


@dataclass
class Config:
    me: str
    partner: str
    partner_aliases: list[re.Pattern]
    default_share: Decimal
    rules: list[Rule]
    item_rules: list[Rule]


def _compile(pattern: str) -> re.Pattern:
    """Anchor keyword patterns on word boundaries so "heb" cannot match "the best",
    while still letting "diaper" match "Diapers" and "bottle" match "Bottles"."""
    if re.match(r"^\w", pattern):
        pattern = r"\b" + pattern
    if re.search(r"\w$", pattern):
        pattern = pattern + r"(?:e?s)?\b"
    return re.compile(pattern, re.I)


def _build_rules(entries: list[dict]) -> list[Rule]:
    rules = []
    for entry in entries:
        rules.append(
            Rule(
                name=entry.get("name", "unnamed"),
                split=entry.get("split", REVIEW),
                category=entry.get("category", ""),
                patterns=[_compile(p) for p in entry.get("match", [])],
                share=Decimal(str(entry["share"])) if "share" in entry else None,
            )
        )
    return rules


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text())
    household = data.get("household", {})
    return Config(
        me=household.get("me", "Me"),
        partner=household.get("partner", "Partner"),
        partner_aliases=[re.compile(p, re.I) for p in household.get("partner_aliases", [])],
        default_share=Decimal(str(household.get("default_share", 0.5))),
        rules=_build_rules(data.get("rule", [])),
        item_rules=_build_rules(data.get("item_rule", [])),
    )


def classify(txn: Txn, config: Config) -> None:
    """Set split/category/rule on a transaction in place."""
    text = f"{txn.description} {txn.raw_description} {txn.category}"
    best: Rule | None = None
    best_strength = 0
    for rule in config.rules:
        strength = rule.match_strength(text)
        if strength > best_strength:
            best, best_strength = rule, strength

    if best is None:
        txn.split = REVIEW
        txn.share = config.default_share
        txn.rule = ""
        return

    txn.split = best.split
    txn.rule = best.name
    if best.category:
        txn.category = best.category
    if best.share is not None:
        txn.share = best.share
    else:
        # He owes all of an on-his-behalf expense, half of a shared one.
        txn.share = Decimal("1") if best.split == HIS else config.default_share


def classify_items(items: list, config: Config) -> tuple[Decimal, Decimal, Decimal, list[str]]:
    """Split an Amazon order's items into shared / personal / unknown dollars.

    Returns (shared, personal, unknown, labels) where labels describe each item
    and how it was bucketed, for the review sheet.
    """
    shared = personal = unknown = Decimal("0")
    labels: list[str] = []
    for item in items:
        bucket, strength = REVIEW, 0
        for rule in config.item_rules:
            score = rule.match_strength(item.name)
            if score > strength:
                bucket, strength = rule.split, score
        if bucket == SHARED:
            shared += item.total
        elif bucket == "personal":
            personal += item.total
        else:
            unknown += item.total
        labels.append(f"[{bucket}] {item.name[:70]} ${item.total:.2f}")
    return shared, personal, unknown, labels


def is_partner_payment(txn: Txn, config: Config) -> bool:
    text = f"{txn.description} {txn.raw_description}"
    return any(p.search(text) for p in config.partner_aliases)
