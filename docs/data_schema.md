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
