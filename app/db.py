"""SQLite persistence for the ABC Classifier.

Schema:
    runs                   - one row per classification run, with config + metrics bundle
    sku_classifications    - one row per (run, SKU), full SKU-level result
    derivative_links       - no-history SKUs linked to a parent, with link type + factor
"""
from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "runs" / "classifier.db"
DB_PATH.parent.mkdir(exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft',   -- draft | applied | discarded
    approved_at         TEXT,
    approved_by         TEXT,
    operator            TEXT,
    run_label           TEXT,
    client_name         TEXT,
    warehouse_code      TEXT,
    basis               TEXT,
    config_json         TEXT NOT NULL,
    metrics_json        TEXT NOT NULL,
    universe_size       INTEGER,
    scorable_size       INTEGER,
    a_cutoff_rank       INTEGER,
    b_cutoff_rank       INTEGER,
    window_start        TEXT,
    window_end          TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS sku_classifications (
    run_id              TEXT NOT NULL,
    sku_code            TEXT NOT NULL,
    sku_description     TEXT,
    status_derived      TEXT,
    class_abc           TEXT,
    rank_overall        INTEGER,
    freq_tasks          REAL,
    vol_qty             REAL,
    picks_days          INTEGER,
    qty_on_hand         REAL,
    avg_daily_demand    REAL,
    doi_days            REAL,
    freq_pct            REAL,
    vol_pct             REAL,
    dormant_flag        INTEGER,
    first_pick          TEXT,
    last_pick           TEXT,
    velocity_declared   TEXT,
    PRIMARY KEY (run_id, sku_code),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sku_class ON sku_classifications(run_id, class_abc);
CREATE INDEX IF NOT EXISTS idx_sku_rank ON sku_classifications(run_id, rank_overall);

CREATE TABLE IF NOT EXISTS derivative_links (
    run_id              TEXT NOT NULL,
    child_sku           TEXT NOT NULL,
    parent_sku          TEXT NOT NULL,
    link_type           TEXT NOT NULL,                 -- complementary | cannibalistic | derivative
    factor              REAL DEFAULT 1.0,
    similarity_score    REAL,
    suggested_by        TEXT,                          -- auto | user
    confirmed_by        TEXT,
    confirmed_at        TEXT,
    PRIMARY KEY (run_id, child_sku),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
"""


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


# ---------- writes ----------

def save_run(run_id: str, result, quality_checks: list, client_name: str = "") -> None:
    """Persist a classification run as a full bundle."""
    cfg = result.config
    summary = result.summary
    pareto = result.pareto_scan
    checks = [
        {"name": c.name, "severity": c.severity, "passed": c.passed, "detail": c.detail}
        for c in quality_checks
    ]
    metrics = {"summary": summary, "pareto_scan": pareto, "quality_checks": checks}

    now = datetime.utcnow().isoformat()

    with conn() as c:
        c.execute("""
            INSERT INTO runs (
                run_id, created_at, status, operator, run_label,
                client_name, warehouse_code, basis,
                config_json, metrics_json,
                universe_size, scorable_size, a_cutoff_rank, b_cutoff_rank,
                window_start, window_end
            ) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, now,
            cfg.get("operator"), cfg.get("run_label"),
            client_name or cfg.get("site_name", ""),
            cfg.get("warehouse_code"), cfg.get("basis"),
            json.dumps(cfg, default=str),
            json.dumps(metrics, default=str),
            result.universe_size, result.scorable_size,
            result.a_cutoff_rank, result.b_cutoff_rank,
            str(result.window_start) if result.window_start else None,
            str(result.window_end) if result.window_end else None,
        ))

        # Bulk-insert SKU rows
        df = result.sku_level.copy()
        for col in ("first_pick", "last_pick"):
            if col in df.columns:
                df[col] = df[col].astype(str).where(df[col].notna(), None)
        rows = [
            (
                run_id,
                r.get("sku_code"), r.get("sku_description"), r.get("status_derived"),
                r.get("class_abc"), int(r["rank_overall"]) if pd.notna(r.get("rank_overall")) else None,
                _f(r.get("freq_tasks")), _f(r.get("vol_qty")),
                int(r["picks_days"]) if pd.notna(r.get("picks_days")) else None,
                _f(r.get("qty_on_hand")), _f(r.get("avg_daily_demand")), _f(r.get("doi_days")),
                _f(r.get("freq_pct")), _f(r.get("vol_pct")),
                int(r["dormant_flag"]) if pd.notna(r.get("dormant_flag")) else 0,
                r.get("first_pick") if r.get("first_pick") not in (None, "nan", "NaT") else None,
                r.get("last_pick") if r.get("last_pick") not in (None, "nan", "NaT") else None,
                r.get("velocity_declared"),
            )
            for r in df.to_dict(orient="records")
        ]
        c.executemany("""
            INSERT INTO sku_classifications (
                run_id, sku_code, sku_description, status_derived,
                class_abc, rank_overall,
                freq_tasks, vol_qty, picks_days,
                qty_on_hand, avg_daily_demand, doi_days,
                freq_pct, vol_pct, dormant_flag,
                first_pick, last_pick, velocity_declared
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return float(v)


def approve_run(run_id: str, approver: str) -> None:
    now = datetime.utcnow().isoformat()
    with conn() as c:
        # Any currently-applied run for same client/warehouse is superseded? Keep it simple: just mark this one applied.
        c.execute(
            "UPDATE runs SET status = 'applied', approved_at = ?, approved_by = ? WHERE run_id = ?",
            (now, approver, run_id),
        )


def discard_run(run_id: str) -> None:
    with conn() as c:
        c.execute("UPDATE runs SET status = 'discarded' WHERE run_id = ?", (run_id,))


def save_derivative_link(
    run_id: str, child_sku: str, parent_sku: str, link_type: str,
    factor: float = 1.0, similarity_score: float | None = None,
    suggested_by: str = "auto", confirmed_by: str = "",
) -> None:
    now = datetime.utcnow().isoformat() if confirmed_by else None
    with conn() as c:
        c.execute("""
            INSERT INTO derivative_links
                (run_id, child_sku, parent_sku, link_type, factor,
                 similarity_score, suggested_by, confirmed_by, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, child_sku) DO UPDATE SET
                parent_sku=excluded.parent_sku,
                link_type=excluded.link_type,
                factor=excluded.factor,
                confirmed_by=excluded.confirmed_by,
                confirmed_at=excluded.confirmed_at
        """, (run_id, child_sku, parent_sku, link_type, factor,
              similarity_score, suggested_by, confirmed_by, now))


# ---------- reads ----------

def list_runs(limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT run_id, created_at, status, operator, run_label,
                   client_name, warehouse_code, basis,
                   universe_size, scorable_size,
                   approved_at, approved_by
            FROM runs
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_run_meta(run_id: str) -> dict:
    with conn() as c:
        row = c.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            raise KeyError(f"run {run_id} not in db")
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        metrics = json.loads(d.pop("metrics_json"))
        d["summary"] = metrics.get("summary", {})
        d["pareto_scan"] = metrics.get("pareto_scan", [])
        d["quality_checks"] = metrics.get("quality_checks", [])
        return d


def get_run_skus(run_id: str) -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql_query(
            "SELECT * FROM sku_classifications WHERE run_id = ? ORDER BY rank_overall",
            c, params=(run_id,),
        )


def get_derivative_links(run_id: str) -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql_query(
            "SELECT * FROM derivative_links WHERE run_id = ?",
            c, params=(run_id,),
        )


def compare_runs(run_a: str, run_b: str) -> dict:
    """Produce the delta bundle used by the 'What Changes' dashboard.

    run_a = older baseline, run_b = newer comparison.
    """
    a = get_run_skus(run_a)[["sku_code", "sku_description", "class_abc", "rank_overall", "freq_tasks", "vol_qty"]]
    b = get_run_skus(run_b)[["sku_code", "sku_description", "class_abc", "rank_overall", "freq_tasks", "vol_qty"]]
    a = a.rename(columns={"class_abc": "class_a", "rank_overall": "rank_a", "freq_tasks": "freq_a", "vol_qty": "vol_a"})
    b = b.rename(columns={"class_abc": "class_b", "rank_overall": "rank_b", "freq_tasks": "freq_b", "vol_qty": "vol_b"})
    merged = a.merge(b, on=["sku_code", "sku_description"], how="outer")

    new_skus = merged[merged["class_a"].isna() & merged["class_b"].notna()][["sku_code", "sku_description", "class_b", "rank_b"]].rename(columns={"class_b": "class", "rank_b": "rank"}).to_dict("records")
    dropped_skus = merged[merged["class_b"].isna() & merged["class_a"].notna()][["sku_code", "sku_description", "class_a", "rank_a"]].rename(columns={"class_a": "class", "rank_a": "rank"}).to_dict("records")

    both = merged[merged["class_a"].notna() & merged["class_b"].notna()].copy()

    def _rank(c: str) -> int:
        return {"A": 1, "B": 2, "C": 3, "N": 4}.get(c, 5)

    both["change"] = "unchanged"
    both.loc[both.apply(lambda r: _rank(r["class_b"]) < _rank(r["class_a"]), axis=1), "change"] = "promoted"
    both.loc[both.apply(lambda r: _rank(r["class_b"]) > _rank(r["class_a"]), axis=1), "change"] = "demoted"

    movements = both[both["change"] != "unchanged"][
        ["sku_code", "sku_description", "class_a", "class_b", "rank_a", "rank_b", "change"]
    ].sort_values(by=["change", "class_b", "rank_b"]).to_dict("records")

    # Class pivot
    cross = pd.crosstab(both["class_a"], both["class_b"]).fillna(0).astype(int)
    cross_tbl = {
        from_cls: {to_cls: int(cross.loc[from_cls, to_cls]) if to_cls in cross.columns and from_cls in cross.index else 0
                   for to_cls in ["A", "B", "C", "N"]}
        for from_cls in ["A", "B", "C", "N"]
    }

    # Config diff
    meta_a = get_run_meta(run_a)
    meta_b = get_run_meta(run_b)
    config_keys = ["basis", "historical_window_days", "forecast_horizon_days",
                   "combined_mix_history_pct", "frequency_weight", "volume_weight",
                   "a_cutoff_pct", "b_cutoff_pct", "minimum_history_days"]
    config_diff = []
    for k in config_keys:
        va = meta_a["config"].get(k)
        vb = meta_b["config"].get(k)
        if va != vb:
            config_diff.append({"key": k, "run_a": va, "run_b": vb})

    return {
        "run_a": meta_a,
        "run_b": meta_b,
        "movements": movements[:500],
        "movement_count": len(movements),
        "new_skus": new_skus[:200],
        "new_sku_count": len(new_skus),
        "dropped_skus": dropped_skus[:200],
        "dropped_sku_count": len(dropped_skus),
        "class_cross": cross_tbl,
        "config_diff": config_diff,
    }


# ---------- bootstrap ----------
init_db()
