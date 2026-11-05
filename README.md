# Addverb ABC Classifier

WMS SKU classification module. Scores every SKU on frequency + volume over a configurable window, ranks them, and assigns ABC classes by rank-% cut-offs. Draft → Applied workflow with a run-vs-run change dashboard.

Built as a FastAPI app with HTML/JS frontend. Upload the data extracts, pick basis (Historical / Forecast / Combined) and parameters, run. Results include a Pareto curve, class summary, SKU-level table, derivative-SKU suggestions, and Excel/JSON export. All runs persist in a local SQLite DB and are revisitable from Run history or the Changes dashboard.

---

## Quick start

### With Docker

```bash
docker compose up --build
# open http://localhost:8000
```

### Without Docker

**Works on Python 3.11 through 3.15.** Pins in `requirements.txt` are loose so pip picks a wheel for your version:

- 3.11 / 3.12 / 3.13: any pandas 2.2 through 3.1 works
- 3.14: pandas 2.3.x (2.2.3 has no 3.14 wheels)
- 3.15: pandas >= 3.0, numpy >= 2.5 (earlier versions have no 3.15 wheels, which is what the "pandas failed to build from source" error means)

If Windows `python` resolves to a Microsoft Store stub, use `py` instead — the Python launcher. pyarrow is optional on 3.15 and only needed for `.parquet` uploads; `.xlsx` and `.csv` work without it.

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
# open http://localhost:8000
```

For hot-reload during development, also `pip install watchfiles` and add `--reload` to the uvicorn command.

---

## What it does

1. **Upload data** (xlsx, csv, or parquet): order_lines, sku_master, optional stock_on_hand, optional forecast
2. **Configure**: client/warehouse, basis, window, weights, cut-offs, add-ons
3. **Run classification** — engine aggregates, ranks by percentile, cuts into A/B/C/N
4. **Review** — class KPIs, Pareto, top-A SKUs, N-class sample, derivative suggestions
5. **Confirm derivatives** — accept, re-pick, or leave blank for each no-history SKU
6. **Approve** — Draft → Applied; slotting consumes only Applied runs
7. **Compare** — pick any two past runs; the dashboard shows class movements, catalog turnover, config diff, Pareto comparison
8. **Export** — Excel per run; JSON API for integrations

---

## Add-on modules (opt-in per run)

- **Derivative suggestions** (default on) — fuzzy-match no-history SKUs to parent SKUs; user confirms
- **XYZ overlay** — demand variability for safety-stock sizing
- **Exception rules** — seasonal, promotional, co-pick overlays

Core classifier runs without any of them.

---

## Logic and backing

Full scoring rules, defaults, and rationale in [`docs/classification_logic.md`](docs/classification_logic.md). Rendered at `/docs/logic` in the running app.

Key invariants:
- Score is never shown — rank positions only
- Cut-offs are % of SKUs by rank, not cumulative value
- Percentile-rank normalisation (robust to lumpy portfolios)
- Dormant SKUs classified normally + flagged; `no-history` SKUs → class N
- Manual picks included (frequency at pick-task grain, not order grain)

---

## Data schema

See [`docs/data_schema.md`](docs/data_schema.md) for expected columns on each asset. Source SQL for a typical WMS stack in [`sample_data/sql/`](sample_data/sql/). Files with a `Data` sheet (as produced by DBeaver `.dbcnb` exports) are handled transparently.

---

## API

```
GET  /                            setup + upload page
POST /classify                    run (multipart form)
GET  /runs                        run history
GET  /runs/{run_id}               results page
POST /runs/{run_id}/approve       Draft -> Applied
POST /runs/{run_id}/discard       Draft -> Discarded
GET  /runs/{run_id}/export.xlsx   Excel download
GET  /dashboard                   pick two runs to compare
GET  /dashboard/{a}/{b}           change dashboard
POST /derivatives/{run_id}        save derivative link confirmations
GET  /api/runs                    list runs (JSON)
GET  /api/runs/{run_id}           run meta JSON
GET  /api/runs/{run_id}/skus      per-SKU rows JSON (?cls=A&limit=100)
GET  /health                      healthcheck
GET  /docs                        OpenAPI
```

The `/api/` endpoints are what a future slotting module or WMS integration consumes.

---

## Persistence

All runs live in `runs/classifier.db` (SQLite). Tables: `runs`, `sku_classifications`, `derivative_links`. The file survives restarts, is portable, and can be committed to Git or migrated to Postgres for multi-user deployments.

---

## Project structure

```
addverb-abc-classifier/
├── app/
│   ├── classifier.py        # scoring engine
│   ├── derivatives.py       # fuzzy parent-SKU suggestion
│   ├── db.py                # SQLite persistence + compare_runs
│   ├── loaders.py           # Excel/CSV/Parquet ingestion
│   ├── validators.py        # quality gates
│   ├── config.py            # defaults + presets
│   ├── storage.py           # backward-compat shim over db
│   ├── models.py            # pydantic shapes
│   └── main.py              # FastAPI routes
├── static/styles.css        # Addverb design tokens
├── templates/               # Jinja templates for UI
├── docs/
│   ├── classification_logic.md
│   └── data_schema.md
├── sample_data/sql/         # the WMS extract queries
├── tests/
├── runs/                    # SQLite DB + per-run xlsx exports
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## Roadmap

- Enterprise approval queue (per-line overrides, bulk actions)
- API-driven forecast ingestion (vs file upload today)
- Dynamic slotting — consumes approved classes, separate module
- Multi-tenant deployment (per-client data isolation)

---

## License

Internal to Addverb Technologies. Not for distribution.
