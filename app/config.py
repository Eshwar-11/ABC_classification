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


# ---------------------------------------------------------------------------
# Order Profile pre-analytics config
# ---------------------------------------------------------------------------
# All values here are Admin-configurable per client. Starter values are from
# Vinayak's reference methodology (06 Nov 2026 walkthrough) and are NOT fixed.
# FMS is a display-only axis; the classifier assigns one ABC class per SKU.

@dataclass
class OrderProfileConfig:
    # FMS cuts: cumulative order-line share thresholds. SKUs ranked by line
    # frequency descending. F+ holds the top band, F the next, M the next,
    # S the remainder. Three thresholds define four bands.
    fms_cut_f_plus: float = 0.60
    fms_cut_f: float = 0.80
    fms_cut_m: float = 0.95

    # DOH (Days-on-Hand) targets per ABC class. Used by panel D2 to flag SKUs
    # where actual DOI drifts beyond 1.5x target.
    doh_target_a_plus: float = 6.0
    doh_target_a: float = 8.0
    doh_target_b: float = 15.0
    doh_target_c: float = 20.0

    # Heatmap display controls
    top_n_skus: int = 100              # Rows in C1, C2. Allowed: 25, 50, 100, 200.
    bin_equivalent_mode: bool = False  # Global toggle: units vs bin-equivalents.

    # DOI flag threshold: panel D2 flags actual > (target * doi_alert_multiplier)
    doi_alert_multiplier: float = 1.5

    def fms_bands(self) -> list[tuple[str, float]]:
        """Return the (label, cumulative_upper_bound) list in rank order."""
        return [
            ("F+", self.fms_cut_f_plus),
            ("F",  self.fms_cut_f),
            ("M",  self.fms_cut_m),
            ("S",  1.0),
        ]

    def doh_targets(self) -> dict[str, float]:
        return {
            "A+": self.doh_target_a_plus,
            "A":  self.doh_target_a,
            "B":  self.doh_target_b,
            "C":  self.doh_target_c,
        }

# Quality gates — soft limits for warnings; hard limits block the run
QUALITY_GATES = {
    "sku_coverage_min": 0.97,
    "sku_master_freshness_hours": 24,
    "stock_freshness_hours": 24,
    "max_negative_qty_rows": 0,
    "max_duplicate_sku_in_master": 0,
}
