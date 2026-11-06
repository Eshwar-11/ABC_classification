"""The scoring engine.

Supports three basis modes:
  - historical: rank SKUs on their past N days of pick-task frequency and volume
  - forecast:   rank SKUs on their next H days of forecast units (frequency derived
                from historical lines-per-unit ratio if a history file is also provided;
                else forecast-only without frequency)
  - combined:   blend historical and forecast daily demand with weight m

See docs/classification_logic.md for the full rationale.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import RunConfig


@dataclass
class ClassificationResult:
    config: dict
    summary: dict
    pareto_scan: list[dict]
    sku_level: pd.DataFrame
    universe_size: int
    scorable_size: int
    a_cutoff_rank: int
    b_cutoff_rank: int
    window_start: date | None
    window_end: date | None
    calendar_days: int = 0
    active_days: int = 0


def classify(
    order_lines: pd.DataFrame,
    sku_master: pd.DataFrame,
    stock_on_hand: pd.DataFrame | None,
    config: RunConfig,
    forecast: pd.DataFrame | None = None,
) -> ClassificationResult:
    today = date.today()

    # --- window filter on history ---
    window_start = today - timedelta(days=config.historical_window_days)
    window_end = today
    ol = order_lines.copy()
    ol = ol[(ol["pick_date"] >= window_start) & (ol["pick_date"] <= window_end)]

    # --- historical aggregates (needed even for combined basis) ---
    hist_agg = ol.groupby("sku_code", as_index=False).agg(
        hist_freq=("n_tasks", "sum"),
        hist_vol=("qty_shipped", "sum"),
        picks_days=("pick_date", "nunique"),
        first_pick=("pick_date", "min"),
        last_pick=("pick_date", "max"),
    )

    # Operating-calendar metrics: distinct dates with any pick activity in the window.
    # 'Active day' threshold: a date counts as active if total picks across all SKUs
    # for that date exceeds ~25% of the median daily pick count. This filters out
    # shutdown days and weekend/half-day fragments that would skew per-day rates.
    calendar_days = int(config.historical_window_days)
    active_days = 0
    if len(ol):
        daily_tot = ol.groupby("pick_date")["n_tasks"].sum()
        if len(daily_tot):
            med = float(daily_tot.median())
            threshold = max(med * 0.25, 1.0)
            active_days = int((daily_tot >= threshold).sum())
    active_days = max(active_days, 1)

    # --- forecast aggregates ---
    fc_agg = None
    if forecast is not None and len(forecast):
        fc_start = today
        fc_end = today + timedelta(days=config.forecast_horizon_days)
        fc = forecast.copy()
        fc = fc[(fc["forecast_date"] >= fc_start) & (fc["forecast_date"] <= fc_end)]
        fc_agg = fc.groupby("sku_code", as_index=False).agg(fc_vol=("forecast_units", "sum"))

    # --- derive the metrics used for scoring ---
    master_cols = ["sku_code", "sku_description", "status_derived"]
    if "velocity_declared" in sku_master.columns:
        master_cols.append("velocity_declared")
    df = sku_master[master_cols].drop_duplicates("sku_code").merge(hist_agg, on="sku_code", how="left")
    df["hist_freq"] = df["hist_freq"].fillna(0)
    df["hist_vol"] = df["hist_vol"].fillna(0)
    df["picks_days"] = df["picks_days"].fillna(0).astype(int)

    if fc_agg is not None:
        df = df.merge(fc_agg, on="sku_code", how="left")
        df["fc_vol"] = df["fc_vol"].fillna(0)
    else:
        df["fc_vol"] = 0

    # Basis-specific freq and vol columns
    if config.basis == "historical":
        df["freq_tasks"] = df["hist_freq"]
        df["vol_qty"] = df["hist_vol"]
    elif config.basis == "forecast":
        df["vol_qty"] = df["fc_vol"]
        # Frequency on forecast basis = historical lines-per-unit * forecast units
        # Fallback to 0 for SKUs with no history (they'd be N anyway).
        lpu = np.where(df["hist_vol"] > 0, df["hist_freq"] / df["hist_vol"], 0)
        df["freq_tasks"] = lpu * df["fc_vol"]
    elif config.basis == "combined":
        m = config.combined_mix_history_pct / 100.0
        wd = max(config.historical_window_days, 1)
        hd = max(config.forecast_horizon_days, 1)
        hist_daily_vol = df["hist_vol"] / wd
        fc_daily_vol = df["fc_vol"] / hd
        blended_daily_vol = m * hist_daily_vol + (1 - m) * fc_daily_vol
        # Project onto the historical window length for interpretability
        df["vol_qty"] = blended_daily_vol * wd
        lpu = np.where(df["hist_vol"] > 0, df["hist_freq"] / df["hist_vol"], 0)
        df["freq_tasks"] = lpu * df["vol_qty"]
    else:
        raise ValueError(f"unknown basis: {config.basis}")

    # --- stock on hand ---
    if stock_on_hand is not None and len(stock_on_hand):
        df = df.merge(
            stock_on_hand[["sku_code", "qty_on_hand"]].drop_duplicates("sku_code"),
            on="sku_code", how="left",
        )
        df["qty_on_hand"] = df["qty_on_hand"].fillna(0)
    else:
        df["qty_on_hand"] = 0

    # --- percentile ranks on scorable only ---
    scorable = df["freq_tasks"] > 0
    df["freq_pct"] = 0.0
    df["vol_pct"] = 0.0
    df.loc[scorable, "freq_pct"] = df.loc[scorable, "freq_tasks"].rank(pct=True, method="average")
    df.loc[scorable, "vol_pct"] = df.loc[scorable, "vol_qty"].rank(pct=True, method="average")

    wF, wV = _normalise_weights(config)
    df["_score_internal"] = wF * df["freq_pct"] + wV * df["vol_pct"]
    df["rank_overall"] = (-df["_score_internal"]).rank(method="first").astype(int)

    # --- class assignment ---
    n_scorable = int(scorable.sum())
    a_end = max(int(np.ceil(n_scorable * config.a_cutoff_pct / 100)), 1) if n_scorable else 0
    b_end = max(int(np.ceil(n_scorable * (config.a_cutoff_pct + config.b_cutoff_pct) / 100)), a_end) if n_scorable else 0

    def _class(row):
        if not scorable.iloc[row.name] or row.get("status_derived") == "no-history":
            return "N"
        r = row["rank_overall"]
        if r <= a_end:
            return "A"
        if r <= b_end:
            return "B"
        return "C"

    df["class_abc"] = df.apply(_class, axis=1)
    df["dormant_flag"] = (df["status_derived"].fillna("") == "dormant").astype(int)

    # --- DOI (on calendar days, the standard convention) ---
    window_days = max(config.historical_window_days, 1)
    df["avg_daily_demand"] = df["vol_qty"] / window_days
    df["doi_days"] = np.where(
        df["avg_daily_demand"] > 0,
        df["qty_on_hand"] / df["avg_daily_demand"],
        np.nan,
    )

    # --- Secondary rate: qty per active-day (fair to SKUs that missed noisy days) ---
    # Reported for diagnostic; does NOT affect the A/B/C class assignment.
    df["qty_per_active_day"] = df["vol_qty"] / active_days
    df["tasks_per_active_day"] = df["freq_tasks"] / active_days

    # --- summary ---
    # Make calendar vs active days available to the summary
    total_vol = max(df["vol_qty"].sum(), 1)
    total_freq = max(df["freq_tasks"].sum(), 1)
    summary_rows = []
    for cls in ["A", "B", "C", "N"]:
        sub = df[df["class_abc"] == cls]
        summary_rows.append({
            "class": cls,
            "skus": int(len(sub)),
            "sku_pct": round(len(sub) / max(len(df), 1) * 100, 2),
            "volume": float(sub["vol_qty"].sum()),
            "volume_pct": round(sub["vol_qty"].sum() / total_vol * 100, 2),
            "frequency": float(sub["freq_tasks"].sum()),
            "frequency_pct": round(sub["freq_tasks"].sum() / total_freq * 100, 2),
            "dormant_count": int(sub["dormant_flag"].sum()),
            "avg_doi_days": float(sub["doi_days"].mean()) if sub["doi_days"].notna().any() else None,
        })

    # --- pareto scan ---
    pareto = []
    if n_scorable:
        sc = df[scorable].sort_values("rank_overall").reset_index(drop=True)
        sc["cum_vol"] = sc["vol_qty"].cumsum() / sc["vol_qty"].sum() * 100
        sc["cum_freq"] = sc["freq_tasks"].cumsum() / sc["freq_tasks"].sum() * 100
        for pct in [5, 10, 20, 30, 50, 80]:
            k = max(int(np.ceil(len(sc) * pct / 100)), 1)
            row = sc.iloc[k - 1]
            pareto.append({
                "top_pct": pct,
                "skus": k,
                "cum_volume_pct": round(float(row["cum_vol"]), 2),
                "cum_frequency_pct": round(float(row["cum_freq"]), 2),
            })

    # --- output ---
    out_cols = [
        "sku_code", "sku_description", "status_derived", "velocity_declared",
        "freq_tasks", "vol_qty", "picks_days", "qty_on_hand", "avg_daily_demand",
        "doi_days", "qty_per_active_day", "tasks_per_active_day",
        "freq_pct", "vol_pct", "rank_overall", "class_abc", "dormant_flag",
        "first_pick", "last_pick",
    ]
    out_cols = [c for c in out_cols if c in df.columns]
    sku_level = df[out_cols].sort_values("rank_overall").reset_index(drop=True)

    return ClassificationResult(
        config=asdict(config),
        summary={"classes": summary_rows,
                 "total_skus": int(len(df)),
                 "scorable_skus": n_scorable,
                 "a_ends_at_rank": a_end,
                 "b_ends_at_rank": b_end,
                 "calendar_days": calendar_days,
                 "active_days": active_days},
        pareto_scan=pareto,
        sku_level=sku_level,
        universe_size=int(len(df)),
        scorable_size=n_scorable,
        a_cutoff_rank=a_end,
        b_cutoff_rank=b_end,
        window_start=window_start,
        window_end=window_end,
        calendar_days=calendar_days,
        active_days=active_days,
    )


def _normalise_weights(config: RunConfig) -> tuple[float, float]:
    total = config.frequency_weight + config.volume_weight
    if total <= 0:
        return 0.5, 0.5
    return config.frequency_weight / total, config.volume_weight / total
