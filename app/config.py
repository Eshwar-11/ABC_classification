"""Configuration defaults for classification runs.

These defaults come from Satwik's v2.1 requirements doc and the design decisions
captured in docs/classification_logic.md. Each can be overridden per run from the UI.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Basis = Literal["historical", "forecast", "combined"]


@dataclass
class RunConfig:
    # Scope
    warehouse_code: str = "Noida DC-1"
    site_name: str = "PetMaxi Noida DC-1"

    # Basis selection (user must pick, no silent default in UI)
    basis: Basis = "historical"

    # Windows
    historical_window_days: int = 90
    forecast_horizon_days: int = 30
    combined_mix_history_pct: int = 60  # 60% history, 40% forecast

    # Weights (frequency vs volume) — percentile-rank blend
    frequency_weight: float = 0.70
    volume_weight: float = 0.30

    # Cut-offs (% of SKUs by rank over the scorable universe)
    a_cutoff_pct: float = 10.0
    b_cutoff_pct: float = 30.0

    # Thresholds
    minimum_history_days: int = 30  # D6 default
    dormant_days_threshold: int = 180

    # Behaviour flags
    include_manual_picks: bool = True
    dormant_scored_normally: bool = True  # dormant SKUs classified then flagged
    carry_forward_overrides: bool = True
    hold_results_for_approval: bool = True

    # Capacity (for prime-slot overflow warning only, informational)
    prime_slot_capacity: int | None = None

    # Metadata
    operator: str = ""
    triggered_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    run_label: str | None = None


# Preset cut-off bundles clients can pick from
PRESETS = {
    "standard_80_15_5": {"label": "Standard FMCG (80/15/5 cumulative value, textbook)", "a": 20, "b": 30, "note": "Textbook ABC on SKU count."},
    "ecom_pets_10_30_60": {"label": "E-com / pets (10/30/60)", "a": 10, "b": 30, "note": "PetMaxi default. Fits pet distributor Pareto."},
    "pharma_spares_5_15_80": {"label": "Pharma / spares (5/15/80)", "a": 5, "b": 15, "note": "Flatter head for critical-but-slow portfolios."},
}

# Minimum field requirements per asset
REQUIRED_FIELDS = {
    "order_lines": {"sku_code", "pick_date", "qty_shipped", "n_tasks"},
    "sku_master":  {"sku_code", "sku_description", "status_derived"},
    "stock_on_hand": {"sku_code", "qty_on_hand"},
}

# Quality gates — if breached, run can still proceed with warnings
QUALITY_GATES = {
    "sku_coverage_min": 0.97,          # >= 97% of picked SKUs should exist in master
    "sku_master_freshness_hours": 24,
    "stock_freshness_hours": 24,
    "max_negative_qty_rows": 0,
    "max_duplicate_sku_in_master": 0,
}
