"""Backward-compatibility shim.

Earlier builds persisted each run as flat files under runs/<run_id>/. New builds
persist to a SQLite DB (app/db.py). This shim keeps the save_run/load_run API
stable for any older code that still imports it, and routes through app.db.
"""
from __future__ import annotations
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import db

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
RUNS_DIR.mkdir(exist_ok=True)


def new_run_id() -> str:
    import uuid
    return "R-" + datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S") + "-" + uuid.uuid4().hex[:4].upper()


def save_run(run_id: str, result, quality_checks: list, client_name: str = "") -> Path:
    """Persist to SQLite + also drop an xlsx export file for convenience."""
    db.save_run(run_id, result, quality_checks, client_name=client_name)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(exist_ok=True)
    result.sku_level.to_excel(run_dir / "sku_level.xlsx", index=False)
    return run_dir


def load_run(run_id: str) -> tuple[dict, pd.DataFrame]:
    """Try SQLite first; fall back to legacy pickle / parquet / xlsx files."""
    try:
        meta = db.get_run_meta(run_id)
        sku_level = db.get_run_skus(run_id)
        if len(sku_level):
            return meta, sku_level
    except KeyError:
        pass

    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"Run {run_id} not found")
    meta = json.loads((run_dir / "meta.json").read_text())
    pkl = run_dir / "sku_level.pkl"
    pq = run_dir / "sku_level.parquet"
    if pkl.exists():
        with open(pkl, "rb") as f:
            sku_level = pickle.load(f)
    elif pq.exists():
        sku_level = pd.read_parquet(pq)
    else:
        sku_level = pd.read_excel(run_dir / "sku_level.xlsx")
    return meta, sku_level


def list_runs() -> list[dict]:
    return db.list_runs()


def export_xlsx_path(run_id: str) -> Path:
    return RUNS_DIR / run_id / "sku_level.xlsx"
