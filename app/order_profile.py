"""Order Profile pre-analytics engine.

Computes the 12 panels of the Order Profile tab (spec: `order-profile-tab-spec-v1.md`):

    Section A - 4 headline tiles (units/order, lines/order, units/line, active SKUs)
    Section B - 3 ABC x FMS matrices (SKUs, units, lines)
    Section C - 3 heatmaps (SKU x day, SKU x hour, category x week)
    Section D - 2 composition panels (order stratification, DOI vs DOH target)

Inputs are the same files the classifier accepts. The module runs independent
of a classification run so stakeholders can explore the data before tuning
the classifier parameters. FMS is a display-only axis; the classifier itself
assigns one ABC class per SKU.

Roll-up rule applied here (statistically correct for order-profile metrics):

    Orderlines are aggregated at `(order_id, pick_date, sku_code)` grain before
    any profile metric is computed. One SKU on one order on one date = one
    orderline, regardless of how many pick tasks or totes fulfilled it.
    `qty_shipped` = sum. `n_tasks` is preserved for classifier use but is not
    the frequency measure for the profile (the profile counts orderlines, not
    pick tasks, which matches Vinayak's FMS methodology).

If the source was tote-grain (multiple rows per `(order_id, sku_code, date)`
with duplicate pick_type), the roll-up collapses them. If it was already
orderline-grain, the roll-up is a no-op. Verified against Vinayak's
Data_examples_r7.xlsx (11,769 rows / 11,769 unique keys, zero duplicates).
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .config import OrderProfileConfig


# ---------------------------------------------------------------------------
# Public data class
# ---------------------------------------------------------------------------

@dataclass
class OrderProfileResult:
    """Everything a template needs to render the 12 panels."""
    config: dict
    window_start: date | None
    window_end: date | None
    # Diagnostics
    rollup_applied_rows: int        # rows collapsed by the profile-level rollup
    rollup_input_rows: int
    rollup_output_rows: int
    hour_of_day_available: bool
    category_available: bool
    bin_equivalent_available: bool
    sku_count_missing_bin: int
    null_order_rows: int            # manual-pick orderlines with no order_id
    # Section A - tiles
    tiles: dict
    # Section B - ABC x FMS matrices (3 heatmaps)
    abc_fms_sku_count: list[dict]
    abc_fms_units: list[dict]
    abc_fms_lines: list[dict]
    # Section C - heatmaps
    sku_day_heatmap: dict           # {axes: {skus: [...], dates: [...]}, cells: [...]}
    sku_hour_heatmap: dict
    category_week_heatmap: dict
    # Section D - composition
    order_stratification: list[dict]
    doi_vs_doh: list[dict]
    # Suggestions (Section 5 of the spec)
    suggestions: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def profile(
    order_lines: pd.DataFrame,
    sku_master: pd.DataFrame,
    stock_on_hand: pd.DataFrame | None,
    window_days: int,
    op_config: OrderProfileConfig,
    classifier_sku_level: pd.DataFrame | None = None,
) -> OrderProfileResult:
    """Produce a full OrderProfileResult over the given window.

    If `classifier_sku_level` is provided (SKU-level dataframe from a past
    classification run), the ABC class is pulled from there. Otherwise a
    quick-pass ABC is computed inline using the same rank method as the
    classifier (frequency and volume percentile ranks blended 70/30 on the
    window) so the Order Profile tab still works as a standalone pre-analytics
    step with no classification run yet.
    """
    # Anchor the window on the data's own max(pick_date) rather than wall-clock
    # today. This makes the Order Profile tab robust for pre-run exploration on
    # any historical extract: no SKU falls out of the window just because the
    # file was uploaded weeks after it was pulled. Statistically correct for
    # the pre-analytics intent.
    if len(order_lines) and order_lines["pick_date"].notna().any():
        max_date = order_lines["pick_date"].dropna().max()
        if hasattr(max_date, "date"):
            max_date = max_date.date()
        window_end = max_date
    else:
        window_end = date.today()
    window_start = window_end - timedelta(days=window_days)

    # 1. Window filter
    ol = order_lines.copy()
    ol = ol[(ol["pick_date"] >= window_start) & (ol["pick_date"] <= window_end)].copy()

    # 2. Roll-up (statistically correct for orderline-grain metrics)
    rollup_input_rows = len(ol)
    null_order_rows = int(ol["order_id"].isna().sum()) if "order_id" in ol.columns else 0
    ol_profile = _rollup_to_orderline_grain(ol)
    rollup_output_rows = len(ol_profile)
    rollup_applied_rows = rollup_input_rows - rollup_output_rows

    # 3. ABC class per SKU (pull from classifier run if available, else quick compute)
    if classifier_sku_level is not None and "class_abc" in classifier_sku_level.columns:
        abc_map = classifier_sku_level[["sku_code", "class_abc"]].drop_duplicates("sku_code")
        abc_map = dict(zip(abc_map["sku_code"].astype(str), abc_map["class_abc"]))
    else:
        abc_map = _quick_abc(ol_profile)

    # 4. FMS display-only class (configurable cumulative line-share bands)
    fms_map = _compute_fms(ol_profile, op_config)

    # 5. Bin-equivalent availability
    bin_equivalent_available = "sku_per_bin" in sku_master.columns and sku_master["sku_per_bin"].notna().any()
    sku_count_missing_bin = 0
    if bin_equivalent_available:
        sku_count_missing_bin = int(sku_master["sku_per_bin"].isna().sum())

    # If toggle is on and data available, swap the volume measure throughout
    use_bin_eq = bool(op_config.bin_equivalent_mode and bin_equivalent_available)
    if use_bin_eq:
        bin_map = dict(zip(sku_master["sku_code"].astype(str),
                           pd.to_numeric(sku_master["sku_per_bin"], errors="coerce")))
        ol_profile["vol_measure"] = ol_profile.apply(
            lambda r: (r["qty_shipped"] / bin_map[r["sku_code"]])
            if bin_map.get(r["sku_code"]) and bin_map[r["sku_code"]] > 0
            else np.nan,
            axis=1,
        )
    else:
        ol_profile["vol_measure"] = ol_profile["qty_shipped"]

    # 6. Attach ABC + FMS to orderlines for grouped panels
    ol_profile["class_abc"] = ol_profile["sku_code"].map(abc_map).fillna("N")
    ol_profile["class_fms"] = ol_profile["sku_code"].map(fms_map).fillna("S")

    # ------------- Section A: headline tiles -------------
    tiles = _tiles(ol_profile, use_bin_eq)

    # ------------- Section B: ABC x FMS matrices -------------
    classes_abc = ["A+", "A", "B", "C", "N"]   # A+ reserved (empty under single-class ABC until classifier emits it)
    # Actually: our classifier emits A/B/C/N only. Keep A+ as an empty row so
    # the UI slot is stable if the classifier is later extended; don't pad with fake data.
    classes_abc_present = sorted({c for c in ol_profile["class_abc"].unique() if c})
    # Union of what's in data with the canonical list; keep A/B/C/N ordering
    ordered = [c for c in classes_abc if c in classes_abc_present]
    classes_fms = ["F+", "F", "M", "S"]

    abc_fms_sku_count = _matrix(ol_profile, ordered, classes_fms, agg="sku_count")
    abc_fms_units     = _matrix(ol_profile, ordered, classes_fms, agg="units")
    abc_fms_lines     = _matrix(ol_profile, ordered, classes_fms, agg="lines")

    # ------------- Section C: heatmaps -------------
    sku_day_heatmap = _sku_day_heatmap(ol_profile, op_config)
    hour_available, sku_hour_heatmap = _sku_hour_heatmap(ol, op_config)
    category_available, category_week_heatmap = _category_week_heatmap(ol_profile, sku_master, use_bin_eq, bin_map if use_bin_eq else None)

    # ------------- Section D: composition -------------
    order_stratification = _order_stratification(ol_profile)
    doi_vs_doh = _doi_vs_doh(ol_profile, stock_on_hand, abc_map, window_days, op_config)

    # ------------- Section 5: suggestions to Setup -------------
    suggestions = _suggestions(ol_profile, op_config)

    return OrderProfileResult(
        config=asdict(op_config),
        window_start=window_start,
        window_end=window_end,
        rollup_applied_rows=rollup_applied_rows,
        rollup_input_rows=rollup_input_rows,
        rollup_output_rows=rollup_output_rows,
        hour_of_day_available=hour_available,
        category_available=category_available,
        bin_equivalent_available=bin_equivalent_available,
        sku_count_missing_bin=sku_count_missing_bin,
        null_order_rows=null_order_rows,
        tiles=tiles,
        abc_fms_sku_count=abc_fms_sku_count,
        abc_fms_units=abc_fms_units,
        abc_fms_lines=abc_fms_lines,
        sku_day_heatmap=sku_day_heatmap,
        sku_hour_heatmap=sku_hour_heatmap,
        category_week_heatmap=category_week_heatmap,
        order_stratification=order_stratification,
        doi_vs_doh=doi_vs_doh,
        suggestions=suggestions,
    )


# ---------------------------------------------------------------------------
# Roll-up to orderline grain
# ---------------------------------------------------------------------------

def _rollup_to_orderline_grain(ol: pd.DataFrame) -> pd.DataFrame:
    """Collapse the loader's `(order_id, pick_date, sku_code, pick_type)` rows
    down to one row per `(order_id, pick_date, sku_code)`.

    `order_id` NULL (manual picks) is kept as a single pseudo-null group per
    (date, sku), which is the pre-existing loader behaviour. Panels that treat
    order_id as the unit of analysis warn when null_order_rows is high.
    """
    group_cols = ["order_id", "pick_date", "sku_code"]
    present = [c for c in group_cols if c in ol.columns]
    if not present:
        return ol.copy()

    agg_kwargs: dict[str, tuple[str, str]] = {
        "qty_shipped": ("qty_shipped", "sum"),
    }
    if "n_tasks" in ol.columns:
        agg_kwargs["n_tasks"] = ("n_tasks", "sum")
    for pass_col in ("warehouse_code", "zone_code", "uom", "line_type",
                     "first_task_ts", "last_task_ts"):
        if pass_col in ol.columns:
            if pass_col == "first_task_ts":
                agg_kwargs[pass_col] = (pass_col, "min")
            elif pass_col == "last_task_ts":
                agg_kwargs[pass_col] = (pass_col, "max")
            else:
                agg_kwargs[pass_col] = (pass_col, "first")

    out = ol.groupby(present, as_index=False, dropna=False).agg(**agg_kwargs)
    return out


# ---------------------------------------------------------------------------
# Quick inline ABC (used only when no prior classification run is passed in)
# ---------------------------------------------------------------------------

def _quick_abc(ol: pd.DataFrame) -> dict[str, str]:
    """70/30 freq/vol percentile rank, 10/30/60 cut. Matches the classifier
    default. Returns {sku_code: 'A'|'B'|'C'|'N'}."""
    if len(ol) == 0:
        return {}
    agg = ol.groupby("sku_code", as_index=False).agg(
        freq=("order_id", "count"),   # orderlines, post-rollup
        vol=("qty_shipped", "sum"),
    )
    scorable = agg["freq"] > 0
    if not scorable.any():
        return {sku: "N" for sku in agg["sku_code"]}

    agg["freq_pct"] = 0.0
    agg["vol_pct"] = 0.0
    agg.loc[scorable, "freq_pct"] = agg.loc[scorable, "freq"].rank(pct=True, method="average")
    agg.loc[scorable, "vol_pct"] = agg.loc[scorable, "vol"].rank(pct=True, method="average")
    agg["score"] = 0.7 * agg["freq_pct"] + 0.3 * agg["vol_pct"]
    agg["rank"] = (-agg["score"]).rank(method="first").astype(int)

    n = int(scorable.sum())
    a_end = max(int(np.ceil(n * 0.10)), 1)
    b_end = max(int(np.ceil(n * 0.40)), a_end)

    def cls(row) -> str:
        if not scorable.iloc[row.name]:
            return "N"
        if row["rank"] <= a_end:
            return "A"
        if row["rank"] <= b_end:
            return "B"
        return "C"

    agg["class_abc"] = agg.apply(cls, axis=1)
    return dict(zip(agg["sku_code"].astype(str), agg["class_abc"]))


# ---------------------------------------------------------------------------
# FMS (display-only, configurable cumulative-on-lines bands)
# ---------------------------------------------------------------------------

def _compute_fms(ol: pd.DataFrame, cfg: OrderProfileConfig) -> dict[str, str]:
    """Rank SKUs by orderline count descending, walk cumulative share, bucket.

    This is the statistically clean read of Vinayak's F+/F/M/S methodology:
    the top band captures the SKUs whose cumulative line count hits the first
    threshold; subsequent bands capture the next bands. Configurable thresholds
    live in site_config; the engine does not hard-code them.
    """
    if len(ol) == 0:
        return {}
    per_sku = (
        ol.groupby("sku_code", as_index=False)["order_id"]
        .count()
        .rename(columns={"order_id": "lines"})
        .sort_values("lines", ascending=False)
        .reset_index(drop=True)
    )
    total_lines = per_sku["lines"].sum()
    if total_lines <= 0:
        return {sku: "S" for sku in per_sku["sku_code"]}

    per_sku["cum_share"] = per_sku["lines"].cumsum() / total_lines
    bands = cfg.fms_bands()

    def band_of(cum: float) -> str:
        for label, ub in bands:
            if cum <= ub:
                return label
        return "S"

    per_sku["class_fms"] = per_sku["cum_share"].apply(band_of)
    return dict(zip(per_sku["sku_code"].astype(str), per_sku["class_fms"]))


# ---------------------------------------------------------------------------
# Section A: headline tiles
# ---------------------------------------------------------------------------

def _tiles(ol: pd.DataFrame, use_bin_eq: bool) -> dict:
    vol_label = "bin-equivalents" if use_bin_eq else "units"
    if len(ol) == 0:
        return {
            "units_per_order": None, "lines_per_order": None,
            "units_per_line": None, "active_skus_per_day": None,
            "vol_label": vol_label,
            "total_orders": 0, "total_lines": 0, "total_units": 0.0,
        }

    # One "order" is a distinct order_id. Null order_id pseudo-groups
    # are counted once per date for the manual-pick cohort.
    if "order_id" in ol.columns:
        ol = ol.copy()
        ol["_order_key"] = ol["order_id"].fillna("__manual_" + ol["pick_date"].astype(str))
    else:
        ol["_order_key"] = ol["pick_date"].astype(str)

    per_order = ol.groupby("_order_key").agg(
        units=("vol_measure", "sum"),
        lines=("sku_code", "nunique"),
    )

    total_orders = int(len(per_order))
    total_lines = int(per_order["lines"].sum())
    total_units = float(per_order["units"].sum())

    units_per_order = float(per_order["units"].mean()) if total_orders else None
    lines_per_order = float(per_order["lines"].mean()) if total_orders else None
    units_per_line = (total_units / total_lines) if total_lines else None

    # Active SKUs/day = mean over days of distinct SKUs with any pick
    per_day = ol.groupby("pick_date")["sku_code"].nunique()
    active_skus_per_day = float(per_day.mean()) if len(per_day) else None

    return {
        "units_per_order": _round(units_per_order),
        "lines_per_order": _round(lines_per_order),
        "units_per_line":  _round(units_per_line),
        "active_skus_per_day": _round(active_skus_per_day),
        "vol_label": vol_label,
        "total_orders": total_orders,
        "total_lines": total_lines,
        "total_units": _round(total_units),
    }


# ---------------------------------------------------------------------------
# Section B: ABC x FMS matrices
# ---------------------------------------------------------------------------

def _matrix(ol: pd.DataFrame, classes_abc: list[str], classes_fms: list[str], agg: str) -> list[dict]:
    """Return a list of {abc, fms, value} cells covering the full grid."""
    if len(ol) == 0 or not classes_abc:
        return []
    if agg == "sku_count":
        pivot = (
            ol.groupby(["class_abc", "class_fms"])["sku_code"]
            .nunique()
            .unstack(fill_value=0)
        )
    elif agg == "units":
        pivot = (
            ol.groupby(["class_abc", "class_fms"])["vol_measure"]
            .sum()
            .unstack(fill_value=0)
        )
    elif agg == "lines":
        # One orderline = one row after rollup. Count rows.
        pivot = (
            ol.groupby(["class_abc", "class_fms"])["sku_code"]
            .count()
            .unstack(fill_value=0)
        )
    else:
        raise ValueError(f"unknown agg: {agg}")

    pivot = pivot.reindex(index=classes_abc, columns=classes_fms, fill_value=0)
    return [
        {"abc": abc, "fms": fms, "value": _round(float(pivot.loc[abc, fms]))}
        for abc in classes_abc
        for fms in classes_fms
    ]


# ---------------------------------------------------------------------------
# Section C: heatmaps
# ---------------------------------------------------------------------------

def _sku_day_heatmap(ol: pd.DataFrame, cfg: OrderProfileConfig) -> dict:
    if len(ol) == 0:
        return _empty_heatmap("skus", "dates")
    top = (
        ol.groupby("sku_code")["sku_code"].count().sort_values(ascending=False).head(cfg.top_n_skus).index.tolist()
    )
    sub = ol[ol["sku_code"].isin(top)].copy()
    dates = sorted(sub["pick_date"].unique())
    cells_df = sub.groupby(["sku_code", "pick_date"])["vol_measure"].sum().reset_index()
    date_strs = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in dates]

    # Pre-built nested lookup: {sku: {date_str: value}} for O(1) template reads.
    lookup: dict[str, dict[str, float]] = {str(s): {} for s in top}
    max_v = 0.0
    for _, r in cells_df.iterrows():
        s = str(r["sku_code"])
        d = r["pick_date"].isoformat() if hasattr(r["pick_date"], "isoformat") else str(r["pick_date"])
        v = _round(float(r["vol_measure"]))
        lookup[s][d] = v
        if isinstance(v, (int, float)) and v > max_v:
            max_v = float(v)

    return {
        "skus": [str(s) for s in top],
        "dates": date_strs,
        "lookup": lookup,
        "max_v": max_v,
    }


def _empty_heatmap(rows_key: str, cols_key: str) -> dict:
    return {rows_key: [], cols_key: [], "lookup": {}, "max_v": 0}


def _sku_hour_heatmap(ol_raw: pd.DataFrame, cfg: OrderProfileConfig) -> tuple[bool, dict]:
    """Hour-of-day heatmap. Requires timestamp granularity on first_task_ts."""
    hours = list(range(24))
    empty = {"skus": [], "hours": hours, "lookup": {}, "max_v": 0}
    if "first_task_ts" not in ol_raw.columns or ol_raw["first_task_ts"].isna().all():
        return False, empty
    ts = pd.to_datetime(ol_raw["first_task_ts"], errors="coerce")
    if ts.isna().all():
        return False, empty

    tmp = ol_raw.assign(_hour=ts.dt.hour).dropna(subset=["_hour"])
    tmp["_hour"] = tmp["_hour"].astype(int)
    top = (
        tmp.groupby("sku_code")["sku_code"].count().sort_values(ascending=False).head(cfg.top_n_skus).index.tolist()
    )
    sub = tmp[tmp["sku_code"].isin(top)]
    cells_df = sub.groupby(["sku_code", "_hour"]).size().reset_index(name="lines")

    lookup: dict[str, dict[str, int]] = {str(s): {} for s in top}
    max_v = 0
    for _, r in cells_df.iterrows():
        s = str(r["sku_code"]); h = str(int(r["_hour"])); v = int(r["lines"])
        lookup[s][h] = v
        if v > max_v:
            max_v = v

    return True, {
        "skus": [str(s) for s in top],
        "hours": [str(h) for h in hours],
        "lookup": lookup,
        "max_v": max_v,
    }


def _category_week_heatmap(
    ol: pd.DataFrame,
    sku_master: pd.DataFrame,
    use_bin_eq: bool,
    bin_map: dict | None,
) -> tuple[bool, dict]:
    empty = {"categories": [], "weeks": [], "lookup": {}, "max_v": 0}
    if "material_category" not in sku_master.columns or sku_master["material_category"].isna().all():
        return False, empty

    cat_map = dict(zip(sku_master["sku_code"].astype(str), sku_master["material_category"]))
    tmp = ol.copy()
    tmp["category"] = tmp["sku_code"].map(cat_map)
    tmp = tmp.dropna(subset=["category"])
    if len(tmp) == 0:
        return False, empty

    dates = pd.to_datetime(tmp["pick_date"], errors="coerce")
    tmp["_week"] = dates.dt.strftime("%Y-W%V")
    tmp = tmp.dropna(subset=["_week"])

    cells_df = tmp.groupby(["category", "_week"])["vol_measure"].sum().reset_index()
    categories = sorted(tmp["category"].unique().tolist())
    weeks = sorted(tmp["_week"].unique().tolist())

    lookup: dict[str, dict[str, float]] = {c: {} for c in categories}
    max_v = 0.0
    for _, r in cells_df.iterrows():
        c = str(r["category"]); w = str(r["_week"]); v = _round(float(r["vol_measure"]))
        lookup[c][w] = v
        if isinstance(v, (int, float)) and v > max_v:
            max_v = float(v)

    return True, {
        "categories": categories,
        "weeks": weeks,
        "lookup": lookup,
        "max_v": max_v,
    }


# ---------------------------------------------------------------------------
# Section D: composition
# ---------------------------------------------------------------------------

def _order_stratification(ol: pd.DataFrame) -> list[dict]:
    """For each order_id, build the sorted set of ABC classes present. Bucket
    orders by that pattern (e.g. 'A B C', 'A', 'B C', 'A+ A B C')."""
    if len(ol) == 0 or "order_id" not in ol.columns:
        return []
    # Only real order_ids (drop null-order pseudo-groups to avoid mixing cohorts)
    real = ol.dropna(subset=["order_id"])
    if len(real) == 0:
        return []
    classes_per_order = (
        real.groupby("order_id")["class_abc"].apply(lambda s: " ".join(sorted(set(s))))
    )
    buckets = classes_per_order.value_counts().reset_index()
    buckets.columns = ["pattern", "orders"]
    total = int(buckets["orders"].sum())
    return [
        {"pattern": r["pattern"], "orders": int(r["orders"]), "pct": round(r["orders"] / total * 100, 2)}
        for _, r in buckets.iterrows()
    ]


def _doi_vs_doh(
    ol: pd.DataFrame,
    stock_on_hand: pd.DataFrame | None,
    abc_map: dict[str, str],
    window_days: int,
    cfg: OrderProfileConfig,
) -> list[dict]:
    """Actual DOI (from stock / avg daily demand) vs target DOH per class.
    Flag when actual > target * doi_alert_multiplier."""
    if stock_on_hand is None or len(stock_on_hand) == 0 or len(ol) == 0:
        return []
    daily = (
        ol.groupby("sku_code")["qty_shipped"].sum().reset_index(name="vol_total")
    )
    daily["avg_daily_demand"] = daily["vol_total"] / max(window_days, 1)

    merged = daily.merge(
        stock_on_hand[["sku_code", "qty_on_hand"]].drop_duplicates("sku_code"),
        on="sku_code", how="inner",
    )
    merged["doi"] = np.where(
        merged["avg_daily_demand"] > 0,
        merged["qty_on_hand"] / merged["avg_daily_demand"],
        np.nan,
    )
    merged["class_abc"] = merged["sku_code"].map(abc_map).fillna("N")

    targets = cfg.doh_targets()
    rows = []
    for cls in ["A+", "A", "B", "C", "N"]:
        sub = merged[(merged["class_abc"] == cls) & merged["doi"].notna()]
        if len(sub) == 0:
            continue
        actual = float(sub["doi"].mean())
        target = targets.get(cls)  # None for N
        flag = (target is not None and actual > target * cfg.doi_alert_multiplier)
        rows.append({
            "class": cls,
            "actual_doi": _round(actual),
            "target_doh": _round(target) if target is not None else None,
            "skus": int(len(sub)),
            "flag": bool(flag),
        })
    return rows


# ---------------------------------------------------------------------------
# Section 5: suggestions
# ---------------------------------------------------------------------------

def _suggestions(ol: pd.DataFrame, cfg: OrderProfileConfig) -> list[dict]:
    """Produce 0..N short suggestions the Ops Head can apply into the Setup
    sliders. Each carries a target field, a value, and the reason."""
    out: list[dict] = []
    if len(ol) == 0:
        return out

    # Suggestion 1 (from D1-like analysis): if more than half of orders mix A and C classes,
    # frequency weight may be under-weighted; recommend 60/40.
    if "order_id" in ol.columns:
        real = ol.dropna(subset=["order_id"])
        if len(real):
            per_order = real.groupby("order_id")["class_abc"].apply(set)
            mixed = sum(1 for s in per_order if {"A", "C"}.issubset(s))
            pct_mixed = mixed / len(per_order)
            if pct_mixed >= 0.30:
                out.append({
                    "target": "frequency_weight",
                    "value": 0.60,
                    "reason": f"{pct_mixed:.0%} of orders span A and C classes. "
                              f"Consider lifting frequency weight to 60/40 so fast-moving SKUs anchor pick-path.",
                    "source_panel": "D1",
                })

    # Suggestion 2 (from C1-like analysis): if the last 14 days' daily lines
    # deviate from the window mean by more than 25%, suggest shortening the window.
    daily_lines = ol.groupby("pick_date").size()
    if len(daily_lines) >= 30:
        recent = daily_lines.tail(14).mean()
        full = daily_lines.mean()
        if full > 0 and abs(recent - full) / full >= 0.25:
            out.append({
                "target": "historical_window_days",
                "value": 60,
                "reason": f"Recent 14-day daily lines ({recent:.0f}) deviate from window mean ({full:.0f}) by "
                          f"{abs(recent - full)/full:.0%}. A shorter 60-day window may better reflect current activity.",
                "source_panel": "C1",
            })
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round(v: Any) -> Any:
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return v
