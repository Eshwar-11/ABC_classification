"""Loader tests for the extended set of upload formats.

Covers: CSV (plain + gz), TSV, JSON (array + lines), Feather, Parquet,
Excel, SQLite with single table, SQLite with multiple tables (hint hits),
SQLite with multiple tables (hint misses, raises).
"""
from __future__ import annotations
import gzip
import io
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from app.loaders import load_order_lines


def _sample_frame() -> pd.DataFrame:
    return pd.DataFrame([
        {"order_id": "o1", "pick_date": "2026-11-01", "sku_code": "A", "qty_shipped": 10, "n_tasks": 1},
        {"order_id": "o1", "pick_date": "2026-11-01", "sku_code": "B", "qty_shipped": 5,  "n_tasks": 1},
        {"order_id": "o2", "pick_date": "2026-11-02", "sku_code": "A", "qty_shipped": 3,  "n_tasks": 1},
    ])


# ---------- text formats ----------

def test_csv_plain():
    buf = io.BytesIO()
    _sample_frame().to_csv(buf, index=False)
    buf.seek(0)
    df = load_order_lines(buf, "x.csv")
    assert len(df) == 3 and "sku_code" in df.columns


def test_csv_gzipped():
    raw = _sample_frame().to_csv(index=False).encode()
    buf = io.BytesIO(gzip.compress(raw))
    df = load_order_lines(buf, "x.csv.gz")
    assert len(df) == 3


def test_tsv():
    raw = _sample_frame().to_csv(index=False, sep="\t").encode()
    df = load_order_lines(io.BytesIO(raw), "x.tsv")
    assert len(df) == 3


def test_txt_with_tab_delimiter_sniffed():
    """.txt files have their delimiter sniffed; a tab-delimited .txt should load."""
    raw = _sample_frame().to_csv(index=False, sep="\t").encode()
    df = load_order_lines(io.BytesIO(raw), "x.txt")
    assert len(df) == 3


# ---------- binary / columnar ----------

def test_parquet():
    buf = io.BytesIO()
    _sample_frame().to_parquet(buf, index=False)
    buf.seek(0)
    df = load_order_lines(buf, "x.parquet")
    assert len(df) == 3


def test_feather():
    buf = io.BytesIO()
    _sample_frame().to_feather(buf)
    buf.seek(0)
    df = load_order_lines(buf, "x.feather")
    assert len(df) == 3


def test_xlsx():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        _sample_frame().to_excel(w, sheet_name="Data", index=False)
    buf.seek(0)
    df = load_order_lines(buf, "x.xlsx")
    assert len(df) == 3


# ---------- JSON ----------

def test_json_array():
    raw = _sample_frame().to_json(orient="records").encode()
    df = load_order_lines(io.BytesIO(raw), "x.json")
    assert len(df) == 3


def test_jsonl():
    raw = _sample_frame().to_json(orient="records", lines=True).encode()
    df = load_order_lines(io.BytesIO(raw), "x.jsonl")
    assert len(df) == 3


# ---------- SQLite ----------

def test_sqlite_single_table_loads():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        with sqlite3.connect(path) as conn:
            _sample_frame().to_sql("order_lines", conn, index=False)
        buf = io.BytesIO(Path(path).read_bytes())
        df = load_order_lines(buf, "x.db")
        assert len(df) == 3
    finally:
        Path(path).unlink(missing_ok=True)


def test_sqlite_multi_table_asset_hint_picks_right_one():
    """A db with both order_lines and sku_master should resolve to order_lines
    when load_order_lines is called, via the asset_hint."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        with sqlite3.connect(path) as conn:
            _sample_frame().to_sql("order_lines", conn, index=False)
            pd.DataFrame([{"sku_code": "X", "sku_description": "d", "status_derived": "active"}]).to_sql(
                "sku_master", conn, index=False
            )
        buf = io.BytesIO(Path(path).read_bytes())
        df = load_order_lines(buf, "x.sqlite")
        assert len(df) == 3
        assert "sku_description" not in df.columns
    finally:
        Path(path).unlink(missing_ok=True)


def test_sqlite_multi_table_no_hint_match_raises():
    """Two tables, neither matches 'order_lines' or 'sku_master' etc; must error
    with a message listing the tables so the ask can be fixed."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        with sqlite3.connect(path) as conn:
            _sample_frame().to_sql("picks_table", conn, index=False)
            _sample_frame().to_sql("other_table", conn, index=False)
        buf = io.BytesIO(Path(path).read_bytes())
        with pytest.raises(ValueError, match="multiple tables"):
            load_order_lines(buf, "x.db")
    finally:
        Path(path).unlink(missing_ok=True)


# ---------- rejection ----------

def test_unsupported_extension_raises():
    with pytest.raises(ValueError, match="Unsupported file type"):
        load_order_lines(io.BytesIO(b""), "x.pdf")
