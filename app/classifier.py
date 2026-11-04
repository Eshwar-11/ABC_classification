"""The scoring engine.

Logic (see docs/classification_logic.md for the full rationale):

1. Aggregate order lines to SKU grain over the chosen window.
2. For each SKU compute:
     frequency = sum of n_tasks over window (pick tasks per SKU)
     volume    = sum of qty_shipped over window
3. Produce percentile ranks (D1: percentile, robust to outliers) within the
   scorable universe (SKUs with frequency > 0).
4. Score = wF * freq_pct + wV * vol_pct  (internal only, not shown in UI).
5. Rank 1 = top of class. Non-scorable SKUs get the highest rank number.
6. Assign classes by rank cut-offs:
     A = top a% of scorable, B = next b%, C = remainder of scorable,
     N = no-history or not scoreable.
7. dormant SKUs still get a class but carry a flag.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .config import RunConfig


@dataclass
class ClassificationResult:
    config: dict
    summary: dict               # class-level aggregates
    pareto_scan: list[dict]     # top k%, cumulative vol/freq
    sku_level: pd.DataFrame     # one row per SKU with class, rank, metrics
    universe_size: int
    scorable_size: int
    a_cutoff_rank: int
    b_cutoff_rank: int
    window_start: date | None
    window_end: date | None


def classify(
    order_lines: pd.DataFrame,
    sku_master: pd.DataFrame,
    stock_on_hand: pd.DataFrame | None,
    config: RunConfig,
) -> ClassificationResult:
    # --- window filter ---
    today = date.today()
    window_start = today - timedelta(days=config.historical_window_days)
    window_end = today
    ol = order_lines.copy()
    ol = ol[(ol["pick_date"] >= window_start) & (ol["pick_date"] <= window_end)]

    # --- aggregate to SKU grain ---
    agg = ol.groupby("sku_code", as_index=False).agg(
        freq_tasks=("n_tasks", "sum"),
        vol_qty=("qty_shipped", "sum"),
        picks_days=("pick_date", "nunique"),
        first_pick=("pick_date", "min"),
        last_pick=("pick_date", "max"),
    )

    # --- join master to get full universe ---
    master_cols = ["sku_code", "sku_description", "status_derived"]
    if "velocity_declared" in sku_master.columns:
        master_cols.append("velocity_declared")
    df = sku_master[master_cols].drop_duplicates("sku_code").merge(
        agg, on="sku_code", how="left"
    )
    df["freq_tasks"] = df["freq_tasks"].fillna(0)
    df["vol_qty"] = df["vol_qty"].fillna(0)
    df["picks_days"] = df["picks_days"].fillna(0).astype(int)

    # --- stock on hand ---
    if stock_on_hand is not None and len(stock_on_hand):
        df = df.merge(
            stock_on_hand[["sku_code", "qty_on_hand"]].drop_duplicates("sku_code"),
            on="sku_code", how="left"
        )
        df["qty_on_hand"] = df["qty_on_hand"].fillna(0)
    else:
        df["qty_on_hand"] = 0

    # --- percentile ranks over scorable only ---
    scorable = df["freq_tasks"] > 0
    df["freq_pct"] = 0.0
    df["vol_pct"] = 0.0
    df.loc[scorable, "freq_pct"] = df.loc[scorable, "freq_tasks"].rank(pct=True, method="average")
    df.loc[scorable, "vol_pct"] = df.loc[scorable, "vol_qty"].rank(pct=True, method="average")

    # --- weighted blend (internal score, never surfaced to UI) ---
    wF, wV = _normalise_weights(config)
    df["_score_internal"] = wF * df["freq_pct"] + wV * df["vol_pct"]

    # --- rank, with non-scorable pushed to the end ---
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

    # --- DOI per SKU (stock / avg daily demand) ---
    window_days = max(config.historical_window_days, 1)
    df["avg_daily_demand"] = df["vol_qty"] / window_days
    df["doi_days"] = np.where(
        df["avg_daily_demand"] > 0,
        df["qty_on_hand"] / df["avg_daily_demand"],
        np.nan,
    )

    # --- class summary ---
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
            "frequency": int(sub["freq_tasks"].sum()),
            "frequency_pct": round(sub["freq_tasks"].sum() / total_freq * 100, 2),
            "dormant_count": int(sub["dormant_flag"].sum()),
            "avg_doi_days": float(sub["doi_days"].mean()) if sub["doi_days"].notna().any() else None,
        })

    # --- pareto scan on scorable only ---
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

    # final tidy of output columns
    out_cols = [
        "sku_code", "sku_description", "status_derived", "velocity_declared",
        "freq_tasks", "vol_qty", "picks_days", "qty_on_hand", "avg_daily_demand",
        "doi_days", "freq_pct", "vol_pct", "rank_overall", "class_abc", "dormant_flag",
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
                 "b_ends_at_rank": b_end},
        pareto_scan=pareto,
        sku_level=sku_level,
        universe_size=int(len(df)),
        scorable_size=n_scorable,
        a_cutoff_rank=a_end,
        b_cutoff_rank=b_end,
        window_start=window_start,
        window_end=window_end,
    )


def _normalise_weights(config: RunConfig) -> tuple[float, float]:
    total = config.frequency_weight + config.volume_weight
    if total <= 0:
        return 0.5, 0.5
    return config.frequency_weight / total, config.volume_weight / total
