# Addverb ABC Classifier

WMS SKU classification module. Scores every SKU on frequency + volume over a configurable window, ranks them, and assigns ABC classes by rank-% cut-offs. Satwik v2.1 scope.

Built as a FastAPI app with HTML/JS frontend. Upload the three data extracts, pick the basis (Historical / Forecast / Combined) and parameters, run. Results include a Pareto curve, class summary, SKU-level table, and Excel/JSON export.

---

## Quick start

### With Docker

```bash
docker compose up --build
# then open http://localhost:8000
```

### Without Docker

**Requires Python 3.11, 3.12 or 3.13.** Python 3.14+ will fail because pandas / numpy / pyarrow do not yet ship wheels for those versions; pip will try to build from C source and need Visual Studio build tools on Windows. If you have multiple Pythons installed, use `py -3.12` on Windows or `python3.12` on macOS/Linux.

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# then open http://localhost:8000
```

---

## What it does

1. **Upload three files** (xlsx, csv, or parquet):
   - `order_lines` — aggregated picks, SKU × order × day
   - `sku_master` — SKU universe with status flags
   - `stock_on_hand` — optional, enables DOI
2. **Pick basis** — Historical / Forecast / Combined. No silent default.
3. **Pick parameters** — frequency vs volume weight, window length, cut-offs (A %, B %).
4. **Run classification** — the engine aggregates, ranks by percentile, cuts into A/B/C/N.
5. **Review results** — class KPIs, Pareto curve, top-A SKUs, N-class sample, quality checks.
6. **Export** — Excel or JSON for downstream consumers (slotting, WES, decks).

---

## Logic and backing

The scoring rules, defaults and their rationale live in [`docs/classification_logic.md`](docs/classification_logic.md). Hit `/docs/logic` in the running app for the same content rendered.

Key invariants:

- **Score is never shown** — rank positions only (Satwik R13).
- **Cut-offs are % of SKUs by rank**, not cumulative value.
- **Percentile rank** normalisation (D1 default), robust to lumpy portfolios.
- **Dormant SKUs** get classified and carry a flag; `no-history` SKUs go to class N.
- **Manual picks included** — frequency is at pick-task grain, not order grain.

---

## Data schema

Expected columns in [`docs/data_schema.md`](docs/data_schema.md). Source SQL for PetMaxi WMS is in [`sample_data/sql/`](sample_data/sql/). Supports xlsx, csv and parquet; files with a `Data` sheet (as produced by the DBeaver `.dbcnb` exports) are handled transparently.

---

## API

```
GET  /                            setup + upload page
POST /classify                    run (multipart form)
GET  /runs                        run history
GET  /runs/{run_id}               results page
GET  /runs/{run_id}/export.xlsx   Excel download
GET  /api/runs/{run_id}           run meta JSON
GET  /api/runs/{run_id}/skus      per-SKU rows (?cls=A&limit=100)
GET  /health                      healthcheck
GET  /docs                        OpenAPI (FastAPI auto)
```

The `/api/` endpoints are what a future slotting module or WMS integration consumes.

---

## Project structure

```
addverb-abc-classifier/
├── app/
│   ├── classifier.py        # scoring engine
│   ├── loaders.py           # Excel/CSV/Parquet ingestion
│   ├── validators.py        # quality gates
│   ├── config.py            # defaults + presets
│   ├── storage.py           # run persistence
│   ├── models.py            # pydantic shapes
│   └── main.py              # FastAPI routes
├── static/styles.css        # design tokens (industrial palette)
├── templates/               # Jinja templates for UI
├── docs/
│   ├── classification_logic.md
│   └── data_schema.md
├── sample_data/sql/         # the three WMS extract queries
├── tests/
├── runs/                    # persisted classification outputs
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## Roadmap

The module is scoped to classification only. Adjacent work parked separately:

- **R15 (proposed) — XYZ overlay** — demand variability (CoV), drives safety-stock multiplier and forecast model choice
- **R16 (proposed) — Exception rules** — seasonal, promotional, discontinued, co-pick pairs
- **v2 — Dynamic slotting** — consumes approved classes; separate module

See [`docs/classification_logic.md`](docs/classification_logic.md) section 10 for scope boundaries.

---

## POC results (PetMaxi, 03 Nov 2026)

First full run against real PetMaxi Noida DC-1 data:

| Class | SKUs | % of universe | Volume share | Line share |
|---|---|---|---|---|
| A | 29 | 2.5% | 59.8% | 57.6% |
| B | 86 | 7.3% | 32.9% | 33.6% |
| C | 172 | 14.6% | 7.3% | 8.7% |
| N | 889 | 75.6% | 0.0% | 0.0% |

Top 10% of scorable SKUs carry 58% of volume. Clean Pareto.

---

## License

Internal to Addverb Technologies. Not for distribution.
