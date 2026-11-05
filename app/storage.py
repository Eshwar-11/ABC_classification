"""Run storage. Simple file-based store; swap for a DB in production.

Uses pickle for the SKU-level cache (fast, preserves dtypes, no extra
dependencies) and xlsx for the user-facing export. Parquet was dropped
because pyarrow lacks 3.15 wheels at the moment and the dep isn't worth it.
"""
from __future__ import annotations
import json
import pickle
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
RUNS_DIR.mkdir(exist_ok=True)


def new_run_id() -> str:
    return "R-" + datetime.utcnow().strftime("%Y-%m-%d-%H%M%S") + "-" + uuid.uuid4().hex[:4].upper()


def save_run(run_id: str, result, quality_checks: list) -> Path:
    """Persist the full run result for later retrieval + export."""
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(exist_ok=True)

    # Metadata JSON
    meta = {
        "run_id": run_id,
        "created_at": datetime.utcnow().isoformat(),
        "config": result.config,
        "summary": result.summary,
        "pareto_scan": result.pareto_scan,
        "universe_size": result.universe_size,
        "scorable_size": result.scorable_size,
        "a_cutoff_rank": result.a_cutoff_rank,
        "b_cutoff_rank": result.b_cutoff_rank,
        "window_start": result.window_start.isoformat() if result.window_start else None,
        "window_end": result.window_end.isoformat() if result.window_end else None,
        "quality_checks": [
            {"name": c.name, "severity": c.severity, "passed": c.passed, "detail": c.detail}
            for c in quality_checks
        ],
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))

    # SKU-level results
    # Pickle for fast app-side reloads (preserves dtypes), xlsx for user export.
    with open(run_dir / "sku_level.pkl", "wb") as f:
        pickle.dump(result.sku_level, f, protocol=pickle.HIGHEST_PROTOCOL)
    result.sku_level.to_excel(run_dir / "sku_level.xlsx", index=False)

    return run_dir


def load_run(run_id: str) -> tuple[dict, pd.DataFrame]:
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"Run {run_id} not found")
    meta = json.loads((run_dir / "meta.json").read_text())
    # Prefer pickle (new format); fall back to parquet for older runs if present.
    pkl = run_dir / "sku_level.pkl"
    pq = run_dir / "sku_level.parquet"
    if pkl.exists():
        with open(pkl, "rb") as f:
            sku_level = pickle.load(f)
    elif pq.exists():
        sku_level = pd.read_parquet(pq)   # requires pyarrow
    else:
        # Fall back to xlsx if neither cache exists.
        sku_level = pd.read_excel(run_dir / "sku_level.xlsx")
    return meta, sku_level


def list_runs() -> list[dict]:
    out = []
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        if d.is_dir() and (d / "meta.json").exists():
            meta = json.loads((d / "meta.json").read_text())
            out.append({
                "run_id": meta["run_id"],
                "created_at": meta["created_at"],
                "site_name": meta["config"].get("site_name"),
                "basis": meta["config"].get("basis"),
                "window_days": meta["config"].get("historical_window_days"),
                "universe_size": meta["universe_size"],
                "scorable_size": meta["scorable_size"],
            })
    return out


def export_xlsx_path(run_id: str) -> Path:
    return RUNS_DIR / run_id / "sku_level.xlsx"
