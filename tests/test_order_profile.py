"""Tests for the Order Profile engine.

Covers:
  - Roll-up is correct (no-op on orderline-grain input, collapses tote-grain)
  - FMS bands sum to the full universe (no SKU unassigned)
  - Tiles compute non-null for a healthy fixture
  - Diagnostics flags set correctly when features are missing
  - Config knobs (FMS cuts, DOH targets) drive output
"""
from __future__ import annotations
from datetime import date, timedelta

import pandas as pd
import pytest

from app.config import OrderProfileConfig
from app.order_profile import profile, _rollup_to_orderline_grain, _compute_fms


# ------------------- fixtures -------------------

def _mk_ol(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["pick_date"] = pd.to_datetime(df["pick_date"]).dt.date
    return df


def _mk_master(skus: list[str], **extras) -> pd.DataFrame:
    data = [{"sku_code": s, "sku_description": f"SKU {s}", "status_derived": "active"}
            for s in skus]
    df = pd.DataFrame(data)
    for col, val in extras.items():
        df[col] = val
    return df


def _mk_soh(mapping: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame([{"sku_code": s, "qty_on_hand": q} for s, q in mapping.items()])


# ------------------- roll-up -------------------

def test_rollup_is_noop_on_orderline_grain():
    """Each (order_id, sku, date) appears once. Rollup should not change count."""
    today = date.today()
    ol = _mk_ol([
        {"order_id": "o1", "pick_date": today, "sku_code": "A", "qty_shipped": 10, "n_tasks": 1},
        {"order_id": "o1", "pick_date": today, "sku_code": "B", "qty_shipped": 5,  "n_tasks": 1},
        {"order_id": "o2", "pick_date": today, "sku_code": "A", "qty_shipped": 3,  "n_tasks": 1},
    ])
    out = _rollup_to_orderline_grain(ol)
    assert len(out) == 3


def test_rollup_collapses_tote_grain_correctly():
    """Two rows for same (order, sku, date) collapse into one with summed qty."""
    today = date.today()
    ol = _mk_ol([
        {"order_id": "o1", "pick_date": today, "sku_code": "A", "qty_shipped": 25, "n_tasks": 1},
        {"order_id": "o1", "pick_date": today, "sku_code": "A", "qty_shipped": 25, "n_tasks": 1},
        {"order_id": "o1", "pick_date": today, "sku_code": "B", "qty_shipped": 10, "n_tasks": 1},
    ])
    out = _rollup_to_orderline_grain(ol)
    assert len(out) == 2
    a_row = out[out["sku_code"] == "A"].iloc[0]
    assert a_row["qty_shipped"] == 50
    assert a_row["n_tasks"] == 2  # n_tasks summed


# ------------------- FMS bands -------------------

def test_fms_bands_cover_all_skus():
    """Every SKU in the universe gets an FMS band. Cumulative bands sum to 1."""
    today = date.today()
    rows = []
    # 10 SKUs, SKU_01 is 50% of lines, SKU_02-03 are 15% each, rest 5% each
    freq = [50, 15, 15] + [5, 4, 3, 2, 2, 2, 2]  # sum = 100
    for i, f in enumerate(freq, 1):
        for _ in range(f):
            rows.append({"order_id": f"o{i}_{_}", "pick_date": today, "sku_code": f"SKU_{i:02d}",
                         "qty_shipped": 1, "n_tasks": 1})
    ol = _mk_ol(rows)
    cfg = OrderProfileConfig()  # 0.60 / 0.80 / 0.95
    fms = _compute_fms(ol, cfg)
    assert set(fms.keys()) == {f"SKU_{i:02d}" for i in range(1, 11)}
    # SKU_01 alone hits 50% cum, so F+. SKU_02 brings cum to 65%, so F+ too.
    assert fms["SKU_01"] == "F+"
    # Each SKU must have exactly one of the four bands
    assert set(fms.values()) <= {"F+", "F", "M", "S"}


def test_fms_cuts_are_configurable():
    """Tighter F+ cut should produce fewer F+ SKUs."""
    today = date.today()
    rows = []
    for i, f in enumerate([40, 30, 20, 10], 1):
        for _ in range(f):
            rows.append({"order_id": f"o{i}_{_}", "pick_date": today, "sku_code": f"SKU_{i}",
                         "qty_shipped": 1, "n_tasks": 1})
    ol = _mk_ol(rows)
    # Tight cut 0.30 is BELOW SKU_1's 0.40 share, so no SKU lands in F+.
    cfg_tight = OrderProfileConfig(fms_cut_f_plus=0.30, fms_cut_f=0.60, fms_cut_m=0.90)
    cfg_loose = OrderProfileConfig(fms_cut_f_plus=0.80, fms_cut_f=0.90, fms_cut_m=0.95)
    fms_tight = _compute_fms(ol, cfg_tight)
    fms_loose = _compute_fms(ol, cfg_loose)

    n_fplus_tight = sum(1 for v in fms_tight.values() if v == "F+")
    n_fplus_loose = sum(1 for v in fms_loose.values() if v == "F+")
    assert n_fplus_tight == 0, f"with F+ cap=0.30 and top SKU at 0.40 share, F+ should be empty; got {fms_tight}"
    # Loose threshold (0.80) captures SKU_1 (0.40 cum) and SKU_2 (0.70 cum) in F+.
    assert n_fplus_loose == 2
    assert n_fplus_loose > n_fplus_tight


# ------------------- end-to-end profile -------------------

def test_profile_end_to_end_minimal():
    """Smoke: a minimal fixture produces non-null tiles and correct shape."""
    today = date.today()
    ol = _mk_ol([
        {"order_id": "o1", "pick_date": today - timedelta(days=i),
         "sku_code": f"SKU_{j}", "qty_shipped": 10 - j, "n_tasks": 1}
        for i in range(5)
        for j in range(1, 6)
    ])
    sm = _mk_master([f"SKU_{j}" for j in range(1, 6)])
    cfg = OrderProfileConfig()
    result = profile(ol, sm, None, window_days=30, op_config=cfg)

    assert result.tiles["units_per_order"] is not None
    assert result.tiles["lines_per_order"] is not None
    assert result.rollup_applied_rows == 0
    assert result.hour_of_day_available is False
    assert result.category_available is False
    assert result.bin_equivalent_available is False
    assert isinstance(result.abc_fms_sku_count, list)
    assert "skus" in result.sku_day_heatmap


def test_profile_window_anchors_on_data_not_today():
    """Pre-analytics should work on old extracts without a today-relative filter."""
    old_date = date(2021, 6, 15)
    ol = _mk_ol([
        {"order_id": "o1", "pick_date": old_date, "sku_code": "A", "qty_shipped": 10, "n_tasks": 1},
        {"order_id": "o2", "pick_date": old_date - timedelta(days=3),
         "sku_code": "A", "qty_shipped": 5, "n_tasks": 1},
    ])
    sm = _mk_master(["A"])
    result = profile(ol, sm, None, window_days=30, op_config=OrderProfileConfig())
    # Window anchored on max(pick_date), not today
    assert result.window_end == old_date
    assert result.tiles["units_per_order"] is not None
    assert result.tiles["total_lines"] == 2


def test_profile_doi_vs_doh_flags_high_doi():
    """When stock is massively above target DOH, flag=True."""
    today = date.today()
    ol = _mk_ol([
        {"order_id": f"o{i}", "pick_date": today - timedelta(days=i % 10),
         "sku_code": "A", "qty_shipped": 10, "n_tasks": 1}
        for i in range(50)
    ])
    sm = _mk_master(["A"])
    # 10_000 units stock / (500 / 30 avg daily) = 600 days DOI, way above 8-day A target
    soh = _mk_soh({"A": 10_000})
    cfg = OrderProfileConfig(doh_target_a=8.0, doi_alert_multiplier=1.5)
    result = profile(ol, sm, soh, window_days=30, op_config=cfg)
    doi_a = [r for r in result.doi_vs_doh if r["class"] == "A"]
    assert len(doi_a) == 1
    assert doi_a[0]["flag"] is True
