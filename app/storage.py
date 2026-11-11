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


# ---------------------------------------------------------------------------
# Order Profile latest-run cache
# ---------------------------------------------------------------------------
# Single-slot cache: the most recent /order-profile run is persisted so that
# navigating away from the tab (sidebar switch, refresh, app restart) does
# not drop the result. No run history for the Order Profile tab; each new
# POST overwrites the cache.
#
# Stored as a pickle under runs/order_profile_latest.pkl with a small meta
# dict so the template can show "last run at ...". Pickle is appropriate
# here because the payload is an internal dataclass (OrderProfileResult)
# plus a config dataclass; neither leaves the server.
# ---------------------------------------------------------------------------

_LATEST_PROFILE_PATH = RUNS_DIR / "order_profile_latest.pkl"


def save_latest_profile(result, op_config, window_days: int) -> Path:
    """Persist the most recent Order Profile result (single slot, overwritten)."""
    payload = {
        "result": result,
        "op_config": op_config,
        "window_days": window_days,
        "saved_at": datetime.now(timezone.utc),
    }
    tmp = _LATEST_PROFILE_PATH.with_suffix(".pkl.tmp")
    with open(tmp, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(_LATEST_PROFILE_PATH)  # atomic swap; no partial reads
    return _LATEST_PROFILE_PATH


def load_latest_profile() -> dict | None:
    """Return {result, op_config, window_days, saved_at} or None if empty."""
    if not _LATEST_PROFILE_PATH.exists():
        return None
    try:
        with open(_LATEST_PROFILE_PATH, "rb") as f:
            return pickle.load(f)
    except (pickle.UnpicklingError, EOFError, AttributeError):
        # Cache file corrupted or written by an incompatible code version.
        # Treat as empty rather than crash the GET.
        return None


def clear_latest_profile() -> bool:
    if _LATEST_PROFILE_PATH.exists():
        _LATEST_PROFILE_PATH.unlink()
        return True
    return False
