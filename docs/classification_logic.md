# Classification logic and backing

This document explains what the classifier does, why each choice was made, and what evidence supports each default. It is the single source of truth for the scoring engine. Code in `app/classifier.py` mirrors this document step for step.

## 1. Scope

One run produces one approved ABC class per SKU for one warehouse, based on past outbound activity, optionally blended with forecast. Classes feed slotting, cycle count frequency, and replenishment policy downstream. Slotting itself is out of scope for this module (handed off to a separate consumer).

## 2. Inputs

Three tabular files, schema in `docs/data_schema.md`:

1. **order_lines** — aggregated outbound picks, SKU × order × day grain
2. **sku_master** — the SKU universe with status flags
3. **stock_on_hand** — current snapshot for DOI computation (optional)

A fourth asset, demand_plan, is required only when basis is Forecast or Combined.

## 3. Scoring

For each SKU over the chosen window:

- `frequency` = sum of `n_tasks` — number of pick tasks performed on that SKU
- `volume` = sum of `qty_shipped` — units moved

Why both: frequency alone rewards SKUs picked often even at small quantities (impulse items), volume alone rewards bulky movers even if picked infrequently (B2B pallets). Blending captures both.

### 3.1 Percentile ranks, not raw values

Each metric is converted to its percentile rank within the scorable universe (SKUs with `frequency > 0`). Reason: raw values differ by orders of magnitude between A and C SKUs; ranks preserve ordering while being robust to outliers. A single viral SKU does not compress everyone else's rank.

**D1 default: percentile rank** (vs min-max scaling). Rationale: lumpy portfolios like PetMaxi have heavy tails; min-max would let the top 1% of SKUs flatten the rest.

### 3.2 Weighted blend

```
score_internal = frequency_weight × freq_percentile + volume_weight × vol_percentile
```

Defaults: 70% frequency, 30% volume. Satwik's doc R1 default.

Why frequency dominates: in warehouse operations, pick-task count drives labour cost and slotting value. A SKU picked 500 times at 1 unit each needs a prime slot more than one picked twice at 500 units each.

**The score is internal only. It is never shown to the user.** The UI shows rank positions (`#1,310 of 12,480`). This is Satwik's R13 explicit requirement, with two reasons: (1) composite scores invite arguments over inputs; (2) ranks are self-explanatory and operationally actionable.

### 3.3 Combined basis

When basis is Combined, daily demand per SKU is computed as:

```
daily_demand = m × historical_avg_daily + (1 - m) × forecast_avg_daily
```

Then frequency on the forecast portion is estimated as `historical lines-per-unit × forecast units`. This assumes the pick-task-to-unit ratio is stable per SKU, which holds for most retail picking where case-pack size is fixed.

**D2 default:** historical lines-per-unit applied to forecast units.

## 4. Classification

SKUs are ranked by `score_internal` descending. Cut-offs are applied as **% of SKUs by rank over the scorable universe**:

- `A` = top `a_cutoff_pct`% of scorable SKUs
- `B` = next `b_cutoff_pct`% of scorable SKUs
- `C` = remainder of scorable SKUs
- `N` = non-scorable (`frequency = 0` or `status_derived = 'no-history'`)

Why rank-% and not cumulative-value: rank-% is the semantics Satwik's doc specifies (R4). Cumulative-value (textbook 80/15/5) was the original brief intention but revised during design review. Rank-% is also easier to reason about for warehouse operators ("top 10% of my SKUs", not "SKUs that make up 80% of value").

### 4.1 Preset cut-offs

- **Standard (textbook)** 20/30/50 — classic ABC over a well-shaped portfolio
- **E-com / pets (10/30/60)** — PetMaxi default; tighter A to fit prime-slot capacity
- **Pharma / spares (5/15/80)** — flatter head where many critical SKUs sit at low velocity

### 4.2 Dormant and no-history

- SKUs with `status_derived = 'dormant'` (last pick >180 days ago by default) are still classified A/B/C if they have any activity in the window, but carry a `dormant_flag = 1`. They are not auto-demoted; the operator decides.
- SKUs with `status_derived = 'no-history'` or zero frequency in the window land in class `N`. They are not slotted.

## 5. DOI (Days of Inventory)

```
avg_daily_demand = volume / window_days
DOI = qty_on_hand / avg_daily_demand   (NaN if demand = 0)
```

DOI is reported per class and is used by the Setup page to suggest an appropriate historical window (R5). A warehouse holding ~60 days of A-class stock should classify on a 60-day window, not a 7-day window.

## 6. New SKUs

SKUs with less than `minimum_history_days` of data (D6 default: 30) are flagged for manual decision. Treatments supported (Satwik 6.6):

- **Default** — treat as C or the site default class
- **Mark as A / B / C** — manual pin
- **Complementary to `<ref>` with factor `f`** — new SKU demand = f × reference demand, added alongside the reference
- **Cannibalizes `<ref>` with factor `f`** — new SKU demand = f × reference demand, subtracted from the reference (can move the reference's class → approval needed)

Factor defaults to 1.00. The "Compute factor" button estimates it from the median of similar launches in the same material category.

## 7. Snapshot + patch

A run scores the SKU master as it was at `snapshot_time`. Late-arriving SKUs or treatment changes made during the run are **patched** at completion rather than re-running the batch:

- A patch recomputes only the affected SKU and its reference (for cannibalizes)
- A patch that would move a reference SKU's class goes to Approvals

This keeps run time bounded and audit trails clean.

## 8. Overrides

An override is one promote or demote step on one SKU with a captured reason. The classifier's role is to compute the impact (SKU count, volume share, line share, prime-slot delta) and to flag overrides whose new rank is outside the target class boundary. Approval routing follows Satwik's rules 6.3.

## 9. Design decisions (Satwik §9, resolved)

| ID | Default | Rationale |
|---|---|---|
| D1 | percentile rank | robust to outliers in lumpy data |
| D2 | hist lines-per-unit × forecast units | stable per-SKU ratio |
| D3 | lines + units | lines ≈ pick labour, units ≈ volume |
| D4 | Ops Head + Admin trigger | WM focuses on review |
| D5 | restricted group greyed | transparency |
| D6 | 30 days min history | balances new-launch coverage against noise |
| D7 | within-cutoff auto-apply | avoids approval queue noise |
| D8 | factor × ref subtracted from ref | resolved 30 Oct 2026 |
| D9 | block if batch awaits approval | avoids dual-batch confusion |
| D10 | site config | prime-slot capacity is a physical constraint |

## 10. What is not in v1

- XYZ overlay (R15 proposed) — demand variability, drives safety-stock multiplier
- Exception rules R16 proposed — seasonal, promotional, discontinued, co-pick
- Dynamic slotting (v2) — consumes approved classes; separate module
- Multi-criteria scoring beyond frequency + volume (e.g. margin, criticality)

Each is scoped and tracked separately so adding them does not reshape the core.

## 11. Evidence from PetMaxi POC Run #1

Run on 03 Nov 2026, 90-day window, 70/30 weights, 10/30/60 cut-offs:

- 1,176 SKUs in master, 287 scorable
- A class: 29 SKUs → 58% of volume, 57% of frequency
- B class: 86 SKUs → 33% of volume
- C class: 172 SKUs → 7% of volume
- N class: 889 SKUs (76% of master, no 90d activity)
- Agreement with existing `product.velocity` field: 17.2% (the existing field is stale)

The Pareto curve is textbook-clean; top 20% of scorable carries 78.6% of volume.
