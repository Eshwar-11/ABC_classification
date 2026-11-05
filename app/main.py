"""FastAPI entrypoint for the Addverb ABC Classifier.

Routes:
    GET  /                                  setup + upload page
    POST /classify                          run (multipart form)
    GET  /runs                              run history
    GET  /runs/{run_id}                     results page
    POST /runs/{run_id}/approve             mark Draft -> Applied
    POST /runs/{run_id}/discard             mark Draft -> Discarded
    GET  /runs/{run_id}/export.xlsx         Excel download
    GET  /dashboard                         "What Changes" dashboard
    GET  /dashboard/{run_a}/{run_b}         compare two runs
    POST /derivatives/{run_id}              save derivative link confirmations
    GET  /api/runs                          list runs (JSON)
    GET  /api/runs/{run_id}                 run meta (JSON)
    GET  /api/runs/{run_id}/skus            per-SKU rows (JSON)
    GET  /docs/logic                        classification logic doc
    GET  /health                            healthcheck
"""
from __future__ import annotations
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, UploadFile, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, db
from .classifier import classify
from .config import RunConfig, PRESETS
from .derivatives import suggest_parents
from .loaders import load_order_lines, load_sku_master, load_stock_on_hand, load_forecast
from .storage import new_run_id, save_run, load_run, export_xlsx_path
from .validators import run_quality_checks, is_blocking

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DOCS_DIR = BASE_DIR / "docs"

app = FastAPI(
    title="Addverb ABC Classifier",
    version=__version__,
    description="WMS SKU classification module with draft/applied workflow and run-vs-run change dashboard.",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"presets": PRESETS, "defaults": RunConfig(), "version": __version__},
    )


@app.get("/runs", response_class=HTMLResponse)
def runs_index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="runs.html",
        context={"runs": db.list_runs(limit=200), "version": __version__},
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    try:
        meta, sku_level = load_run(run_id)
    except (FileNotFoundError, KeyError):
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    top_a = sku_level[sku_level["class_abc"] == "A"].head(25).to_dict(orient="records")
    n_sample = sku_level[sku_level["class_abc"] == "N"].head(20).to_dict(orient="records")

    # Derivative suggestions for no-history SKUs (auto)
    suggestions = []
    if meta["config"].get("enable_derivative_suggestions", True):
        suggestions = suggest_parents(sku_level, max_suggestions_per_child=3, similarity_threshold=0.72)
        suggestions = suggestions[:80]  # cap for render

    # existing confirmed links
    links_df = db.get_derivative_links(run_id)
    confirmed = links_df.to_dict(orient="records") if len(links_df) else []

    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={
            "meta": meta,
            "top_a": top_a,
            "n_sample": n_sample,
            "suggestions": suggestions,
            "confirmed_links": confirmed,
            "version": __version__,
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_index(request: Request):
    runs = db.list_runs(limit=100)
    return templates.TemplateResponse(
        request=request,
        name="dashboard_pick.html",
        context={"runs": runs, "version": __version__},
    )


@app.get("/dashboard/{run_a}/{run_b}", response_class=HTMLResponse)
def dashboard_compare(request: Request, run_a: str, run_b: str):
    try:
        bundle = db.compare_runs(run_a, run_b)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"c": bundle, "version": __version__},
    )


@app.get("/docs/logic", response_class=HTMLResponse)
def logic_doc(request: Request):
    logic_path = DOCS_DIR / "classification_logic.md"
    content = logic_path.read_text() if logic_path.exists() else "Logic doc missing."
    return templates.TemplateResponse(
        request=request,
        name="doc.html",
        context={"title": "Classification logic", "markdown": content, "version": __version__},
    )


# ---------- classify ----------

@app.post("/classify")
async def do_classify(
    request: Request,
    order_lines_file: Annotated[UploadFile, File()],
    sku_master_file: Annotated[UploadFile, File()],
    stock_on_hand_file: Annotated[UploadFile | None, File()] = None,
    forecast_file: Annotated[UploadFile | None, File()] = None,
    site_name: Annotated[str, Form()] = "",
    warehouse_code: Annotated[str, Form()] = "",
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
    enable_xyz: Annotated[bool, Form()] = False,
    enable_exceptions: Annotated[bool, Form()] = False,
    enable_derivative_suggestions: Annotated[bool, Form()] = True,
    operator: Annotated[str, Form()] = "",
    run_label: Annotated[str | None, Form()] = None,
    prime_slot_capacity: Annotated[int | None, Form()] = None,
):
    if basis not in ("historical", "forecast", "combined"):
        raise HTTPException(400, "basis must be one of: historical, forecast, combined")
    if a_cutoff_pct + b_cutoff_pct >= 100:
        raise HTTPException(400, "a_cutoff_pct + b_cutoff_pct must be < 100")
    if basis in ("forecast", "combined") and (forecast_file is None or not forecast_file.filename):
        raise HTTPException(400, f"Basis '{basis}' requires a forecast file upload.")

    try:
        order_lines = load_order_lines(await order_lines_file.read(), order_lines_file.filename)
        sku_master = load_sku_master(await sku_master_file.read(), sku_master_file.filename)
        stock_on_hand = None
        if stock_on_hand_file is not None and stock_on_hand_file.filename:
            stock_on_hand = load_stock_on_hand(await stock_on_hand_file.read(), stock_on_hand_file.filename)
        forecast = None
        if forecast_file is not None and forecast_file.filename:
            forecast = load_forecast(await forecast_file.read(), forecast_file.filename)
    except ValueError as e:
        raise HTTPException(400, f"Load error: {e}")

    config = RunConfig(
        site_name=site_name, warehouse_code=warehouse_code, basis=basis,  # type: ignore[arg-type]
        historical_window_days=historical_window_days,
        forecast_horizon_days=forecast_horizon_days,
        combined_mix_history_pct=combined_mix_history_pct,
        frequency_weight=frequency_weight, volume_weight=volume_weight,
        a_cutoff_pct=a_cutoff_pct, b_cutoff_pct=b_cutoff_pct,
        minimum_history_days=minimum_history_days,
        dormant_days_threshold=dormant_days_threshold,
        enable_xyz=enable_xyz, enable_exceptions=enable_exceptions,
        enable_derivative_suggestions=enable_derivative_suggestions,
        prime_slot_capacity=prime_slot_capacity,
        operator=operator, run_label=run_label,
    )

    qc = run_quality_checks(order_lines, sku_master, stock_on_hand if stock_on_hand is not None else _empty_df())
    if is_blocking(qc):
        return JSONResponse(
            status_code=422,
            content={"error": "blocking_quality_check_failed",
                     "checks": [{"name": c.name, "severity": c.severity, "passed": c.passed, "detail": c.detail} for c in qc]},
        )

    result = classify(order_lines, sku_master, stock_on_hand, config, forecast=forecast)

    run_id = new_run_id()
    save_run(run_id, result, qc, client_name=site_name)

    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


# ---------- approvals ----------

@app.post("/runs/{run_id}/approve")
def approve(run_id: str, approver: Annotated[str, Form()] = ""):
    approver = approver or "unknown"
    db.approve_run(run_id, approver)
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/discard")
def discard(run_id: str):
    db.discard_run(run_id)
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


# ---------- derivatives ----------

@app.post("/derivatives/{run_id}")
async def save_derivatives(run_id: str, request: Request):
    form = await request.form()
    confirmed_by = form.get("confirmed_by", "user")
    saved = 0
    # Fields come as derivative[childSku][parent_sku], [link_type], [factor]
    children: dict[str, dict] = {}
    for key, value in form.items():
        if not key.startswith("deriv["):
            continue
        # parse deriv[childsku][field]
        end = key.find("]")
        child = key[6:end]
        field = key[end + 2:-1]
        children.setdefault(child, {})[field] = value

    for child, fields in children.items():
        parent_sku = (fields.get("parent_sku") or "").strip()
        if not parent_sku:
            continue
        link_type = fields.get("link_type", "derivative")
        try:
            factor = float(fields.get("factor", 1.0))
        except ValueError:
            factor = 1.0
        db.save_derivative_link(
            run_id=run_id, child_sku=child, parent_sku=parent_sku,
            link_type=link_type, factor=factor,
            suggested_by=fields.get("suggested_by", "user"),
            confirmed_by=confirmed_by,
        )
        saved += 1

    return RedirectResponse(url=f"/runs/{run_id}#derivatives", status_code=303)


# ---------- API + downloads ----------

@app.get("/api/runs")
def api_runs(limit: int = 50):
    return db.list_runs(limit=limit)


@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    try:
        return db.get_run_meta(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")


@app.get("/api/runs/{run_id}/skus")
def api_run_skus(run_id: str, limit: int = 100, cls: str | None = None):
    try:
        skus = db.get_run_skus(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    if cls:
        skus = skus[skus["class_abc"] == cls]
    return skus.head(limit).to_dict(orient="records")


@app.get("/runs/{run_id}/export.xlsx")
def export_xlsx(run_id: str):
    p = export_xlsx_path(run_id)
    if not p.exists():
        # rebuild from db on demand
        try:
            skus = db.get_run_skus(run_id)
        except KeyError:
            raise HTTPException(404, "run not found")
        p.parent.mkdir(exist_ok=True)
        skus.to_excel(p, index=False)
    return FileResponse(
        p,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{run_id}_sku_level.xlsx",
    )


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__, "time": datetime.now(timezone.utc).isoformat()}


def _empty_df():
    import pandas as pd
    return pd.DataFrame(columns=["sku_code", "qty_on_hand"])
