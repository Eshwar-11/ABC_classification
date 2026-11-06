# Input data schema

Three files, in xlsx / csv / parquet. If xlsx has a sheet named `Data`, it is used; otherwise the first sheet.

## Asset 1 — order_lines (aggregated)

| Column | Type | Required | Notes |
|---|---|---|---|
| sku_code | string | yes | matches sku_master |
| pick_date | date | yes | YYYY-MM-DD |
| qty_shipped | number | yes | positive outbound units |
| n_tasks | int | yes | count of pick tasks grouped into this row |
| order_id | string | no | can be null for manual picks |
| pick_type | string | no | FULL / MANUAL / PARTIAL PICKING |
| warehouse_code | string | no | |
| line_type | string | no | always 'pick' in current extract |
| first_task_ts | timestamp | no | |
| last_task_ts | timestamp | no | |
| zone_code | string | no | ignored for classification |
| uom | string | no | base UOM, per sku_master |

Grain: one row per (`order_id`, `pick_date`, `sku_code`, `pick_type`). Manual picks with null order_id are merged across the day for the same SKU.

**Source SQL (PetMaxi WMS):** see `/sample_data/sql/order_lines.sql`.

## Asset 2 — sku_master

| Column | Type | Required | Notes |
|---|---|---|---|
| sku_code | string | yes | primary key |
| sku_description | string | yes | |
| status_derived | string | yes | one of: `no-history`, `new-sku`, `active`, `dormant` |
| launch_date_derived | date | no | first pick date if not in master |
| last_pick_at | timestamp | no | |
| velocity_declared | string | no | existing WMS velocity field; ignored |
| launch_date_source | string | no | 'derived-first-pick' or 'master' |
| material_category | string | no | enables the Order Profile Category × Week heatmap (C3) |
| sku_per_bin | int | no | enables the Order Profile bin-equivalent toggle. Units of this SKU that fit in one standard bin. See derivation ladder below. |

**`sku_per_bin` derivation ladder** (data team picks the applicable method per client, records choice in `extract_notes.md`):

1. Direct column if WMS stores it
2. `floor(bin_usable_volume / sku_unit_volume)` with a configurable pack-factor (default 0.80) when SKU and bin dimensions are both stored
3. P90 of historical "qty per occupied bin" per SKU from WMS slot history
4. Flat per-category default supplied by the client

Non-positive values are treated as null; SKUs missing `sku_per_bin` are excluded from bin-equivalent panels and surfaced in the Order Profile diagnostics strip.

**Status derivation rule:**

- `no-history` if the SKU has no picks ever
- `new-sku` if first pick was within 60 days
- `dormant` if last pick was > 180 days ago
- `active` otherwise

**Source SQL:** see `/sample_data/sql/sku_master.sql`.

## Asset 3 — stock_on_hand (optional, enables DOI)

| Column | Type | Required | Notes |
|---|---|---|---|
| sku_code | string | yes | |
| qty_on_hand | number | yes | total across all locations in the warehouse |
| warehouse_code | string | no | |
| n_containers | int | no | diagnostic |
| n_bins_occupied | int | no | diagnostic |
| snapshot_date | date | no | |

**Source SQL:** see `/sample_data/sql/stock_on_hand.sql`.

## Validation

Running a classification performs these checks before scoring:

- SKU coverage (picks present in master) ≥ 97% → warn below
- Zero duplicate sku_code in master → block
- Zero negative qty_shipped rows → warn above
- Stock-on-hand SKUs are in master → warn

A `block` severity failure aborts the run. Warnings are shown but do not stop scoring.

## Order Profile pre-analytics (`/order-profile` route)

Standalone tab that reads the same files as a classification run and builds:

- Section A: 4 headline tiles (units/order, lines/order, units/line, active SKUs/day)
- Section B: ABC × FMS matrices (SKU count, units, lines). FMS is a display-only axis configurable via `fms_cut_f_plus`, `fms_cut_f`, `fms_cut_m` on the page.
- Section C: heatmaps (SKU × day, SKU × hour-of-day, Category × week). Hour-of-day needs `first_task_ts` on order_lines; Category needs `material_category` on sku_master. Missing data collapses the panel with a banner.
- Section D: order stratification by class spread, DOI vs DOH (per-class targets configurable).

**Statistically correct roll-up applied inside the engine:** before any panel computes, order_lines are aggregated at `(order_id, pick_date, sku_code)` grain. One SKU on one order on one date = one orderline. On orderline-grain inputs (like Vinayak's reference xlsx) the roll-up is a no-op; on tote-grain inputs it collapses duplicates and sums qty.

**Window anchor:** the Order Profile tab anchors its window on `max(pick_date)` of the uploaded data, not wall-clock today. Pre-analytics on a historical extract runs correctly without needing a date filter workaround.
