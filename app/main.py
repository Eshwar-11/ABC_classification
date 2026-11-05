"""FastAPI entrypoint for the ABC Classifier.

Routes:
    GET  /                  -> setup + upload page
    POST /classify          -> run classification, returns run_id + redirect
    GET  /runs              -> run history
    GET  /runs/{run_id}     -> results page
    GET  /runs/{run_id}/export.xlsx     -> Excel download
    GET  /api/runs/{run_id}             -> JSON for integrations
    GET  /docs/logic        -> classification logic doc

Health: GET /health
"""
from __future__ import annotations
import io
import json
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, UploadFile, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .classifier import classify
from .config import RunConfig, PRESETS
from .loaders import load_order_lines, load_sku_master, load_stock_on_hand
from .storage import (
    new_run_id, save_run, load_run, list_runs, export_xlsx_path,
)
from .validators import run_quality_checks, is_blocking

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DOCS_DIR = BASE_DIR / "docs"

app = FastAPI(
    title="Addverb ABC Classifier",
    version=__version__,
    description="WMS SKU classification module. Satwik v2.1 scope.",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "presets": PRESETS,
            "defaults": RunConfig(),
            "version": __version__,
        },
    )


@app.get("/runs", response_class=HTMLResponse)
def runs_index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="runs.html",
        context={
            "runs": list_runs(),
            "version": __version__,
        },
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    try:
        meta, sku_level = load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    # Prepare displayable slice
    top_a = sku_level[sku_level["class_abc"] == "A"].head(25).to_dict(orient="records")
    top_c = sku_level[sku_level["class_abc"] == "C"].head(10).to_dict(orient="records")
    n_sample = sku_level[sku_level["class_abc"] == "N"].head(10).to_dict(orient="records")
    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={
            "meta": meta,
            "top_a": top_a,
            "top_c": top_c,
            "n_sample": n_sample,
            "version": __version__,
        },
    )


@app.get("/docs/logic", response_class=HTMLResponse)
def logic_doc(request: Request):
    logic_path = DOCS_DIR / "classification_logic.md"
    content = logic_path.read_text() if logic_path.exists() else "Logic doc missing."
    return templates.TemplateResponse(
        request=request,
        name="doc.html",
        context={
            "title": "Classification logic",
            "markdown": content,
            "version": __version__,
        },
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@app.post("/classify")
async def do_classify(
    request: Request,
    order_lines_file: Annotated[UploadFile, File()],
    sku_master_file: Annotated[UploadFile, File()],
    stock_on_hand_file: Annotated[UploadFile | None, File()] = None,
    site_name: Annotated[str, Form()] = "PetMaxi Noida DC-1",
    warehouse_code: Annotated[str, Form()] = "Noida DC-1",
    basis: Annotated[str, Form()] = "historical",
    historical_window_days: Annotated[int, Form()] = 90,
    forecast_horizon_days: Annotated[int, Form()] = 30,
    combined_mix_history_pct: Annotated[int, Form()] = 60,
    frequency_weight: Annotated[float, Form()] = 0.7,
    volume_weight: Annotated[float, Form()] = 0.3,
    a_cutoff_pct: Annotated[float, Form()] = 10.0,
    b_cutoff_pct: Annotated[float, Form()] = 30.0,
    minimum_history_days: Annotated[int, Form()] = 30,
    dormant_days_threshold: Annotated[int, Form()] = 180,
    operator: Annotated[str, Form()] = "",
    run_label: Annotated[str | None, Form()] = None,
    prime_slot_capacity: Annotated[int | None, Form()] = None,
):
    # --- validate basis ---
    if basis not in ("historical", "forecast", "combined"):
        raise HTTPException(400, "basis must be one of: historical, forecast, combined")

    # --- load ---
    try:
        order_lines = load_order_lines(await order_lines_file.read(), order_lines_file.filename)
        sku_master = load_sku_master(await sku_master_file.read(), sku_master_file.filename)
        stock_on_hand = None
        if stock_on_hand_file is not None and stock_on_hand_file.filename:
            stock_on_hand = load_stock_on_hand(await stock_on_hand_file.read(), stock_on_hand_file.filename)
    except ValueError as e:
        raise HTTPException(400, f"Load error: {e}")

    # --- cutoff sanity ---
    if a_cutoff_pct + b_cutoff_pct >= 100:
        raise HTTPException(400, "a_cutoff_pct + b_cutoff_pct must be < 100")

    config = RunConfig(
        site_name=site_name,
        warehouse_code=warehouse_code,
        basis=basis,  # type: ignore[arg-type]
        historical_window_days=historical_window_days,
        forecast_horizon_days=forecast_horizon_days,
        combined_mix_history_pct=combined_mix_history_pct,
        frequency_weight=frequency_weight,
        volume_weight=volume_weight,
        a_cutoff_pct=a_cutoff_pct,
        b_cutoff_pct=b_cutoff_pct,
        minimum_history_days=minimum_history_days,
        dormant_days_threshold=dormant_days_threshold,
        prime_slot_capacity=prime_slot_capacity,
        operator=operator,
        run_label=run_label,
    )

    # --- quality gates ---
    qc = run_quality_checks(order_lines, sku_master, stock_on_hand if stock_on_hand is not None else _empty_soh())
    if is_blocking(qc):
        return JSONResponse(
            status_code=422,
            content={
                "error": "blocking_quality_check_failed",
                "checks": [{"name": c.name, "severity": c.severity, "passed": c.passed, "detail": c.detail} for c in qc],
            },
        )

    # --- classify ---
    result = classify(order_lines, sku_master, stock_on_hand, config)

    # --- persist ---
    run_id = new_run_id()
    save_run(run_id, result, qc)

    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


# ---------------------------------------------------------------------------
# API + downloads
# ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    try:
        meta, _ = load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(404, "run not found")
    return meta


@app.get("/api/runs/{run_id}/skus")
def api_run_skus(run_id: str, limit: int = 100, cls: str | None = None):
    try:
        _, sku_level = load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(404, "run not found")
    if cls:
        sku_level = sku_level[sku_level["class_abc"] == cls]
    return sku_level.head(limit).to_dict(orient="records")


@app.get("/runs/{run_id}/export.xlsx")
def export_xlsx(run_id: str):
    p = export_xlsx_path(run_id)
    if not p.exists():
        raise HTTPException(404, "export not found")
    return FileResponse(
        p,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{run_id}_sku_level.xlsx",
    )


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _empty_soh():
    import pandas as pd
    return pd.DataFrame(columns=["sku_code", "qty_on_hand"])
