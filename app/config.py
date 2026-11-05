"""Configuration defaults for classification runs.

All defaults are generic; the client name and warehouse code are required
inputs on every run. Decisions carried over from the requirements doc are
documented inline; each can be overridden per run from the UI.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Basis = Literal["historical", "forecast", "combined"]


@dataclass
class RunConfig:
    # Scope — required inputs, no client-specific defaults
    warehouse_code: str = ""
    site_name: str = ""

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
    minimum_history_days: int = 30
    dormant_days_threshold: int = 180

    # Behaviour flags
    include_manual_picks: bool = True
    dormant_scored_normally: bool = True
    carry_forward_overrides: bool = True

    # Add-on modules (all optional; classifier runs without them)
    enable_xyz: bool = False
    enable_exceptions: bool = False
    enable_derivative_suggestions: bool = True

    # XYZ thresholds (only used if enable_xyz)
    xyz_x_cov_max: float = 0.5
    xyz_y_cov_max: float = 1.0

    # Capacity (optional, informational warning only)
    prime_slot_capacity: int | None = None

    # Metadata
    operator: str = ""
    triggered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_label: str | None = None


# Preset cut-off bundles
PRESETS = {
    "standard_20_30_50": {"label": "Standard FMCG", "a": 20, "b": 30, "note": "Textbook ABC on SKU count."},
    "distributor_10_30_60": {"label": "Distributor / tight head", "a": 10, "b": 30, "note": "Narrow A to fit prime-slot capacity. Default."},
    "pharma_5_15_80": {"label": "Pharma / spares", "a": 5, "b": 15, "note": "Flatter head; many critical slow-movers."},
    "ecommerce_15_25_60": {"label": "E-commerce", "a": 15, "b": 25, "note": "Mid-range head for mixed B2C catalogs."},
}

# Minimum field requirements per asset
REQUIRED_FIELDS = {
    "order_lines": {"sku_code", "pick_date", "qty_shipped", "n_tasks"},
    "sku_master":  {"sku_code", "sku_description", "status_derived"},
    "stock_on_hand": {"sku_code", "qty_on_hand"},
    "forecast":    {"sku_code", "forecast_date", "forecast_units"},
}

# Quality gates — soft limits for warnings; hard limits block the run
QUALITY_GATES = {
    "sku_coverage_min": 0.97,
    "sku_master_freshness_hours": 24,
    "stock_freshness_hours": 24,
    "max_negative_qty_rows": 0,
    "max_duplicate_sku_in_master": 0,
}
