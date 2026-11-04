"""Smoke tests for the classifier. Run with: pytest."""
from __future__ import annotations
from datetime import date, timedelta

import pandas as pd

from app.classifier import classify
from app.config import RunConfig
from app.validators import run_quality_checks, is_blocking


def _synthetic():
    today = date.today()
    # 10 SKUs, varying activity
    rows = []
    for i, sku in enumerate([f"SKU-{n:03d}" for n in range(1, 11)]):
        # top 2 are heavy movers, next 3 medium, rest low
        picks = 200 if i < 2 else (60 if i < 5 else 5)
        for d_off in range(picks):
            rows.append({
                "sku_code": sku,
                "pick_date": today - timedelta(days=d_off % 30),
                "qty_shipped": 10 * (i + 1),
                "n_tasks": 1,
            })
    ol = pd.DataFrame(rows)
    sm = pd.DataFrame([
        {"sku_code": f"SKU-{n:03d}", "sku_description": f"desc {n}", "status_derived": "active"}
        for n in range(1, 11)
    ] + [
        {"sku_code": "SKU-DEAD", "sku_description": "never picked", "status_derived": "no-history"}
    ])
    soh = pd.DataFrame([
        {"sku_code": f"SKU-{n:03d}", "qty_on_hand": 100} for n in range(1, 11)
    ])
    return ol, sm, soh


def test_classifies_without_errors():
    ol, sm, soh = _synthetic()
    qc = run_quality_checks(ol, sm, soh)
    assert not is_blocking(qc)
    cfg = RunConfig(historical_window_days=90, a_cutoff_pct=20, b_cutoff_pct=30)
    r = classify(ol, sm, soh, cfg)
    assert r.universe_size == 11
    assert r.scorable_size == 10


def test_cutoffs_respected():
    ol, sm, soh = _synthetic()
    cfg = RunConfig(historical_window_days=90, a_cutoff_pct=20, b_cutoff_pct=30)
    r = classify(ol, sm, soh, cfg)
    counts = r.sku_level["class_abc"].value_counts().to_dict()
    assert counts.get("A", 0) == 2   # top 20% of 10
    assert counts.get("B", 0) == 3   # next 30%
    assert counts.get("C", 0) == 5   # rest
    assert counts.get("N", 0) == 1   # SKU-DEAD


def test_top_sku_is_highest_frequency():
    ol, sm, soh = _synthetic()
    cfg = RunConfig(historical_window_days=90, a_cutoff_pct=10, b_cutoff_pct=30, frequency_weight=1.0, volume_weight=0.0)
    r = classify(ol, sm, soh, cfg)
    top = r.sku_level.sort_values("rank_overall").iloc[0]
    # top SKU has 200 picks (SKU-001 or SKU-002)
    assert top["sku_code"] in ("SKU-001", "SKU-002")


def test_dormant_flag_set_but_classified():
    ol, sm, soh = _synthetic()
    sm.loc[sm["sku_code"] == "SKU-001", "status_derived"] = "dormant"
    cfg = RunConfig(historical_window_days=90)
    r = classify(ol, sm, soh, cfg)
    row = r.sku_level[r.sku_level["sku_code"] == "SKU-001"].iloc[0]
    assert row["dormant_flag"] == 1
    assert row["class_abc"] in ("A", "B", "C")  # still classified
