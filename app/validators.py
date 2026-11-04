"""Data-quality checks run before the scoring stage.

Gates can fail soft (warn, run proceeds) or hard (abort). See docs/classification_logic.md
for the policy.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .config import QUALITY_GATES

Severity = Literal["info", "warn", "block"]


@dataclass
class Check:
    name: str
    severity: Severity
    passed: bool
    detail: str


def run_quality_checks(
    order_lines: pd.DataFrame,
    sku_master: pd.DataFrame,
    stock_on_hand: pd.DataFrame,
) -> list[Check]:
    checks: list[Check] = []

    # 1. SKU coverage: every SKU in orders should exist in master
    orders_skus = set(order_lines["sku_code"])
    master_skus = set(sku_master["sku_code"])
    uncovered = orders_skus - master_skus
    coverage = (len(orders_skus) - len(uncovered)) / max(len(orders_skus), 1)
    checks.append(Check(
        "sku_coverage_of_orders_in_master",
        "warn",
        coverage >= QUALITY_GATES["sku_coverage_min"],
        f"{coverage*100:.1f}% of picked SKUs ({len(orders_skus)-len(uncovered)} of {len(orders_skus)}) exist in SKU master. "
        f"{len(uncovered)} SKU(s) in picks but missing from master.",
    ))

    # 2. Duplicate SKUs in master
    dupe_count = int(sku_master["sku_code"].duplicated().sum())
    checks.append(Check(
        "no_duplicate_sku_in_master",
        "block",
        dupe_count == QUALITY_GATES["max_duplicate_sku_in_master"],
        f"{dupe_count} duplicate sku_code row(s) in master.",
    ))

    # 3. Negative qty rows in order lines
    neg = int((order_lines["qty_shipped"] < 0).sum())
    checks.append(Check(
        "no_negative_qty_in_orders",
        "warn",
        neg == QUALITY_GATES["max_negative_qty_rows"],
        f"{neg} order-line row(s) have negative qty_shipped.",
    ))

    # 4. Stock on hand coverage
    on_hand_skus = set(stock_on_hand["sku_code"])
    orphan_on_hand = on_hand_skus - master_skus
    checks.append(Check(
        "stock_on_hand_skus_in_master",
        "warn",
        len(orphan_on_hand) == 0,
        f"{len(orphan_on_hand)} SKU(s) have stock but are missing from SKU master.",
    ))

    # 5. Universe size
    checks.append(Check(
        "universe_size",
        "info",
        True,
        f"SKU master contains {len(sku_master):,} SKUs. "
        f"{len(orders_skus):,} were picked in the window. "
        f"{len(on_hand_skus):,} have stock on hand.",
    ))

    # 6. Order lines row count
    checks.append(Check(
        "order_lines_rows",
        "info",
        True,
        f"{len(order_lines):,} aggregated order-line rows over the window.",
    ))

    return checks


def is_blocking(checks: list[Check]) -> bool:
    return any(c.severity == "block" and not c.passed for c in checks)
