# Classification logic and backing

This document explains what the classifier does, why each default is chosen, and what evidence supports it. Code in `app/classifier.py` mirrors this document step for step.

## 1. Scope

One run produces one ABC class per SKU for one warehouse, based on past outbound activity (Historical basis), on forecasted demand (Forecast basis), or a blend (Combined basis). The approved classification feeds downstream slotting, cycle count frequency, and replenishment policy. Slotting itself is a separate module that consumes approved classes; this module does not move SKUs in the warehouse.

## 2. Inputs

Three required tabular files; a fourth needed only for Forecast or Combined basis:

1. **order_lines** — aggregated outbound picks, SKU × order × day grain
2. **sku_master** — SKU universe with status flags
3. **stock_on_hand** — optional; enables DOI
4. **forecast** — required only when basis is Forecast or Combined

Schemas and sample SQL in `docs/data_schema.md` and `sample_data/sql/`.

## 3. Scoring

For each SKU in the chosen window:

- `frequency` = sum of `n_tasks` — number of pick tasks on that SKU
- `volume` = sum of `qty_shipped` — units moved

Both captured because they measure different kinds of effort. Frequency rewards SKUs picked often (labour cost, slot access). Volume rewards bulky movers (travel per unit).

### 3.1 Percentile ranks

Each metric is converted to its percentile rank within the scorable universe (SKUs with `frequency > 0`). Reason: raw values differ by orders of magnitude between A and C; ranks preserve ordering while being robust to outliers.

### 3.2 Weighted blend

```
score_internal = frequency_weight × freq_percentile + volume_weight × vol_percentile
```

Defaults: 70% frequency, 30% volume.

Frequency dominates because in WMS operations, pick-task count drives labour cost and slotting value. A SKU picked 500 times at 1 unit each needs a prime slot more than one picked twice at 500 units each.

**The score is internal only; never shown in the UI.** Screens show rank positions (`#1,310 of 12,480`). This avoids arguments over the composite score and keeps the operational signal concrete.

### 3.3 Basis

- **Historical**: scores on past N days of pick activity. Requires only order_lines.
- **Forecast**: scores on next H days of forecast units. Frequency is estimated by applying each SKU's historical lines-per-unit ratio to its forecast units (stable per SKU for most retail picking where case-pack size is fixed).
- **Combined**: blends historical and forecast daily demand with weight m (default 60% historical). Rebalances when the recent past no longer reflects the near future.

## 4. Classification

SKUs are ranked by `score_internal` descending. Cut-offs apply as **% of SKUs by rank over the scorable universe**:

- `A` = top `a_cutoff_pct`% of scorable SKUs
- `B` = next `b_cutoff_pct`% of scorable SKUs
- `C` = remainder of scorable SKUs
- `N` = non-scorable (`frequency = 0` or `status_derived = 'no-history'`)

Rank-% cut-offs (not cumulative-value) are easier for warehouse operators to reason about ("top 10% of my SKUs").

### 4.1 Preset cut-offs

- **Standard FMCG** 20/30/50 — classic ABC over a well-shaped portfolio
- **Distributor / tight head** 10/30/60 — narrow A to fit prime-slot capacity
- **Pharma / spares** 5/15/80 — flatter head where many critical SKUs sit at low velocity
- **E-commerce** 15/25/60 — mid-range head for mixed B2C catalogs

### 4.2 Dormant and no-history

- `dormant` SKUs (last pick > 180 days ago by default) still classify A/B/C if they have any activity in the window, and carry a `dormant_flag = 1`.
- `no-history` SKUs and any SKU with zero frequency in the window land in class `N`. They aren't slotted.

## 5. DOI (Days of Inventory)

```
avg_daily_demand = volume / window_days
DOI = qty_on_hand / avg_daily_demand   (NaN if demand = 0)
```

Per-class DOI is reported on the results page.

## 6. Add-on modules

All add-ons are opt-in per run. The core classifier produces a complete result without any of them.

### 6.1 Derivative suggestions (default on)

For each no-history SKU, the app fuzzy-matches its code stem + description against the scorable pool and surfaces up to three parent candidates with a similarity score. The operator confirms, re-picks, or leaves blank. Three link types:

- **derivative** — new SKU is a variant/spinoff of the parent; takes no demand from it
- **complementary** — new SKU is sold alongside; demand = factor × parent, added separately
- **cannibalistic** — new SKU eats parent's share; demand = factor × parent, subtracted from parent

Confirmed links persist in the DB per run.

### 6.2 XYZ overlay (opt-in)

Classifies SKUs by coefficient of variation of daily demand. Second axis on top of ABC. Drives safety-stock multiplier and forecast-model selection downstream.

### 6.3 Exception rules (opt-in)

Seasonal, promotional, and co-pick overlays. UI appears on the results page when enabled.

## 7. Approvals

Two states: **Draft** on creation, **Applied** when the operator clicks Approve & apply. The approval records approver and timestamp in the audit fields. Slotting consumes only Applied runs.

For enterprise clients, a full approval queue (per-line overrides with reject/approve) can be added later without changing the core.

## 8. Persistence

Each run is stored in a local SQLite DB (`runs/classifier.db`):

- `runs` — one row per classification, with config + metrics bundle
- `sku_classifications` — one row per (run, SKU)
- `derivative_links` — confirmed new-SKU-to-parent links per run

Previous runs remain readable after app restart. The DB file is portable and can be committed to Git for audit, or moved to Postgres for multi-user deployments.

## 9. Design decisions

| ID | Decision | Default | Rationale |
|---|---|---|---|
| D1 | Score normalisation | Percentile rank | Robust to lumpy outliers |
| D2 | Frequency on forecast basis | Historical lines-per-unit × forecast units | Stable per-SKU ratio |
| D3 | Units of measure | Order lines + units | Lines ≈ labour, units ≈ volume |
| D4 | Who runs a batch | Operator-level (configurable later) | Simple for POC |
| D5 | Restricted group | Shown but greyed (enterprise mode) | Transparency |
| D6 | Min history | 30 days | Balances new launches vs noise |
| D7 | Overrides within cut-off | Auto-apply | Keeps approval queue focused |
| D8 | Cannibalistic semantics | Factor × ref subtracted from ref | Standard |
| D9 | Running while a batch awaits approval | Blocked (not yet enforced in POC) | Avoids double-queue |
| D10 | Prime slot capacity source | Site config input | Physical constraint |

## 10. What is not in v1

- Enterprise approval queue (per-line overrides, bulk actions)
- Multi-tenant row-level security
- API-driven forecast ingestion (file upload only for POC)
- Automated slotting moves (separate module)
