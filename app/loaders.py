"""Tabular loaders for the input assets.

Supported upload types (read):
  - .xlsx / .xls      Excel
  - .csv              plain CSV
  - .csv.gz / .csv.zip gzipped / zipped CSV (pandas unwraps)
  - .tsv / .txt       tab-separated
  - .parquet          columnar, best for >1M rows
  - .feather          Arrow IPC (via pyarrow)
  - .json / .jsonl    JSON records (array or line-delimited)
  - .html / .htm      single-table HTML (streamed via lxml.iterparse)
  - .db / .sqlite / .sqlite3  SQLite database (first user table, or a
                      table named like the asset if present)

Every supported type normalises down to a pandas DataFrame and round-trips
cleanly through `df.to_csv()` so downstream exports stay uniform.

Design notes:
  - Column aliasing: upstream extracts may use client-specific names
    (SAS: n_events, PetMaxi: n_tasks). We normalise on ingest.
  - Order lines may arrive pre-aggregated (PetMaxi pattern) or raw
    (one row per pick task, SAS pattern). We always aggregate to the
    canonical grain (order_id, pick_date, sku_code, pick_type).
  - Excel has a hard 1,048,576-row cap; extracts beyond that must come
    as parquet, csv, or sqlite.
"""
from __future__ import annotations
import csv
import io
import sqlite3
import tempfile
from pathlib import Path
from typing import BinaryIO, IO

import pandas as pd

from .config import REQUIRED_FIELDS


# -----------------------------------------------------------------------------
# Column aliasing
# -----------------------------------------------------------------------------
# Key = canonical name; value = list of accepted aliases found in the wild.
COLUMN_ALIASES = {
    # order_lines
    "n_tasks":        ["n_tasks", "n_events", "task_count", "event_count", "pick_count"],
    "qty_shipped":    ["qty_shipped", "picked_qty", "qty", "quantity_shipped", "quantity"],
    "pick_date":      ["pick_date", "date", "event_date"],
    "first_task_ts":  ["first_task_ts", "first_pick_ts", "first_ts", "start_ts"],
    "last_task_ts":   ["last_task_ts", "last_pick_ts", "last_ts", "end_ts"],
    # sku_master
    "velocity_declared": ["velocity_declared", "velocity_declared_sas", "velocity_declared_petmaxi",
                          "velocity", "existing_velocity", "abc_label", "abc_existing"],
    "sku_description": ["sku_description", "description", "item_description", "sku_desc"],
    "sku_per_bin":     ["sku_per_bin", "units_per_bin", "bin_capacity", "qty_per_bin", "units_per_slot"],
    "material_category": ["material_category", "category", "product_category", "item_category", "sku_category"],
    # stock
    "qty_on_hand":    ["qty_on_hand", "on_hand_qty", "stock_qty", "quantity_on_hand"],
    # forecast
    "forecast_units": ["forecast_units", "forecast_qty", "units_forecast", "demand_units"],
    "forecast_date":  ["forecast_date", "date", "plan_date"],
}


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename any alias column to its canonical name. Non-alias columns kept as-is."""
    lower = {c.lower(): c for c in df.columns}
    rename_map: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        if canonical in df.columns:
            continue  # canonical is already present
        for alias in aliases:
            if alias.lower() in lower and lower[alias.lower()] != canonical:
                rename_map[lower[alias.lower()]] = canonical
                break
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


# -----------------------------------------------------------------------------
# File readers
# -----------------------------------------------------------------------------
def _read_tabular(stream_or_bytes, filename: str, asset_hint: str | None = None) -> pd.DataFrame:
    """Dispatch by file extension.

    stream_or_bytes may be: bytes, str/Path, or a file-like with .read().
    asset_hint (optional): "order_lines" | "sku_master" | "stock_on_hand" |
        "forecast". Used by the SQLite reader to pick the right table when
        the DB holds more than one.
    """
    name = filename.lower()

    if name.endswith((".xlsx", ".xls")):
        data_bytes = _as_bytes(stream_or_bytes)
        xls = pd.ExcelFile(io.BytesIO(data_bytes))
        sheet = "Data" if "Data" in xls.sheet_names else xls.sheet_names[0]
        return pd.read_excel(xls, sheet_name=sheet)

    # Compressed CSV: pandas handles .gz, .zip, .bz2, .xz natively. Map the
    # extension to the pandas-expected compression name.
    if name.endswith((".csv.gz", ".csv.zip", ".csv.bz2", ".csv.xz")):
        data_bytes = _as_bytes(stream_or_bytes)
        ext = name.rsplit(".", 1)[-1]
        compression = {"gz": "gzip", "zip": "zip", "bz2": "bz2", "xz": "xz"}[ext]
        return pd.read_csv(io.BytesIO(data_bytes), compression=compression)

    if name.endswith(".csv"):
        data_bytes = _as_bytes(stream_or_bytes)
        return pd.read_csv(io.BytesIO(data_bytes))

    # Tab-separated. `.txt` is accepted as TSV when its content has tabs;
    # pandas sniffs the delimiter via sep=None with the python engine.
    if name.endswith((".tsv", ".tab")):
        data_bytes = _as_bytes(stream_or_bytes)
        return pd.read_csv(io.BytesIO(data_bytes), sep="\t")
    if name.endswith(".txt"):
        data_bytes = _as_bytes(stream_or_bytes)
        return pd.read_csv(io.BytesIO(data_bytes), sep=None, engine="python")

    if name.endswith(".parquet"):
        data_bytes = _as_bytes(stream_or_bytes)
        try:
            return pd.read_parquet(io.BytesIO(data_bytes))
        except ImportError as e:
            raise ValueError(
                f"Parquet support requires pyarrow. Install it or convert the file to xlsx/csv. {e}"
            )

    if name.endswith((".feather", ".arrow")):
        data_bytes = _as_bytes(stream_or_bytes)
        try:
            return pd.read_feather(io.BytesIO(data_bytes))
        except ImportError as e:
            raise ValueError(
                f"Feather support requires pyarrow. Install it or convert the file to csv/parquet. {e}"
            )

    # JSON records: either a top-level array of objects, or JSON-lines
    # (one record per line). We sniff the first non-whitespace byte.
    if name.endswith((".json", ".jsonl", ".ndjson")):
        data_bytes = _as_bytes(stream_or_bytes)
        is_lines = name.endswith((".jsonl", ".ndjson"))
        if not is_lines:
            stripped = data_bytes.lstrip()
            # A JSONL file whose extension is .json is rare but possible;
            # sniff: an array starts with '[', a JSONL file starts with '{'.
            if stripped.startswith(b"{"):
                is_lines = True
        return pd.read_json(io.BytesIO(data_bytes), lines=is_lines)

    if name.endswith((".db", ".sqlite", ".sqlite3")):
        return _read_sqlite(stream_or_bytes, asset_hint)

    if name.endswith((".html", ".htm")):
        return _read_html_streaming(stream_or_bytes)

    raise ValueError(
        f"Unsupported file type: {filename}. Supported: xlsx/xls, csv (plain or gz/zip/bz2/xz), "
        f"tsv/tab/txt, parquet, feather/arrow, json/jsonl/ndjson, html/htm, db/sqlite/sqlite3."
    )


def _read_sqlite(stream_or_bytes, asset_hint: str | None) -> pd.DataFrame:
    """Open a SQLite database and return a DataFrame.

    Table selection rules, in order of preference:
      1. Exact-name match on the asset_hint (e.g. 'order_lines')
      2. A table whose name contains the asset_hint
      3. If only one user table exists, return it
      4. Otherwise, raise with the list of tables so the extract can be fixed
    """
    # SQLite needs a real file handle; materialise to a temp file.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as tmp:
        if hasattr(stream_or_bytes, "read"):
            while True:
                chunk = stream_or_bytes.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
        elif isinstance(stream_or_bytes, (str, Path)):
            tmp.write(Path(stream_or_bytes).read_bytes())
        else:
            tmp.write(stream_or_bytes)
        db_path = tmp.name

    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            tables = [r[0] for r in rows]

            if not tables:
                raise ValueError("SQLite file contains no user tables.")

            target = None
            if asset_hint:
                hint = asset_hint.lower()
                for t in tables:
                    if t.lower() == hint:
                        target = t
                        break
                if target is None:
                    for t in tables:
                        if hint in t.lower():
                            target = t
                            break
            if target is None:
                if len(tables) == 1:
                    target = tables[0]
                else:
                    raise ValueError(
                        f"SQLite file has multiple tables ({tables}). Name one of them "
                        f"to match the asset (e.g. 'order_lines') or supply a single-table db."
                    )
            return pd.read_sql_query(f'SELECT * FROM "{target}"', conn)
    finally:
        try:
            Path(db_path).unlink(missing_ok=True)
        except Exception:
            pass


def _as_bytes(obj) -> bytes:
    if hasattr(obj, "read"):
        return obj.read()
    if isinstance(obj, (str, Path)):
        return Path(obj).read_bytes()
    return obj


def _read_html_streaming(stream_or_bytes) -> pd.DataFrame:
    """Parse a single-table HTML via lxml.iterparse, streamed to a temp CSV then loaded.

    Handles files of 100MB+ without loading the whole HTML into memory. Only the
    final DataFrame is held in RAM, which is fine up to a few million rows.
    """
    try:
        from lxml import etree
    except ImportError:
        # Fall back to pandas.read_html (loads all into memory; may OOM on very large files)
        data_bytes = _as_bytes(stream_or_bytes)
        return pd.read_html(io.BytesIO(data_bytes))[0]

    # Materialise the stream into a temp file so lxml can incrementally parse
    with tempfile.NamedTemporaryFile(delete=False, suffix=".html") as tmp_html:
        if hasattr(stream_or_bytes, "read"):
            while True:
                chunk = stream_or_bytes.read(1024 * 1024)
                if not chunk:
                    break
                tmp_html.write(chunk)
        elif isinstance(stream_or_bytes, (str, Path)):
            tmp_html.write(Path(stream_or_bytes).read_bytes())
        else:
            tmp_html.write(stream_or_bytes)
        tmp_html_path = tmp_html.name

    try:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv", newline="") as tmp_csv:
            csv_path = tmp_csv.name
            w = csv.writer(tmp_csv)
            cols = None
            context = etree.iterparse(tmp_html_path, events=("end",), tag="tr", recover=True)
            for _, elem in context:
                ths = elem.findall(".//th")
                if ths:
                    cols = [th.text or "" for th in ths]
                    w.writerow(cols)
                    elem.clear()
                    continue
                tds = elem.findall(".//td")
                if not cols or len(tds) != len(cols):
                    elem.clear(); continue
                w.writerow([(td.text or "") for td in tds])
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]

        df = pd.read_csv(csv_path, dtype=str)
    finally:
        try:
            Path(tmp_html_path).unlink(missing_ok=True)
            if 'csv_path' in locals():
                Path(csv_path).unlink(missing_ok=True)
        except Exception:
            pass
    return df


# -----------------------------------------------------------------------------
# Public loaders (each applies aliasing, type coercion, and aggregation as needed)
# -----------------------------------------------------------------------------
def load_order_lines(stream, filename: str) -> pd.DataFrame:
    df = _read_tabular(stream, filename, asset_hint="order_lines")
    df = _normalise_columns(df)
    _assert_required(df, "order_lines")

    # Type coercion
    df["sku_code"] = df["sku_code"].astype(str).str.strip()
    df["pick_date"] = pd.to_datetime(df["pick_date"], errors="coerce").dt.date
    df["qty_shipped"] = pd.to_numeric(df["qty_shipped"], errors="coerce").fillna(0)
    df["n_tasks"] = pd.to_numeric(df["n_tasks"], errors="coerce").fillna(0).astype(int)
    df = df[df["qty_shipped"] > 0].copy()

    # Auto-aggregate: safe on both raw and pre-aggregated inputs.
    # Raw (SAS) collapses many rows per order+date+sku+type into one.
    # Pre-aggregated (PetMaxi) stays effectively unchanged.
    group_cols = [c for c in ["order_id", "pick_date", "sku_code", "pick_type"] if c in df.columns]
    if "order_id" not in df.columns:
        df["order_id"] = None
    agg_kwargs = {"n_tasks": ("n_tasks", "sum"), "qty_shipped": ("qty_shipped", "sum")}
    for pass_col in ("warehouse_code", "zone_code", "uom", "line_type"):
        if pass_col in df.columns:
            agg_kwargs[pass_col] = (pass_col, "first")
    df = df.groupby(group_cols, as_index=False, dropna=False).agg(**agg_kwargs)
    return df


def load_sku_master(stream, filename: str) -> pd.DataFrame:
    df = _read_tabular(stream, filename, asset_hint="sku_master")
    df = _normalise_columns(df)
    _assert_required(df, "sku_master")

    df["sku_code"] = df["sku_code"].astype(str).str.strip()
    if "status_derived" in df.columns:
        df["status_derived"] = df["status_derived"].fillna("no-history").astype(str).str.strip().str.lower()
    if "velocity_declared" in df.columns:
        df["velocity_declared"] = df["velocity_declared"].astype(str).str.strip()
        df.loc[df["velocity_declared"].isin(["", "nan", "None"]), "velocity_declared"] = None

    # Optional columns used by the Order Profile tab. Both are optional and
    # their absence disables the related panels/toggles rather than failing.
    if "sku_per_bin" in df.columns:
        df["sku_per_bin"] = pd.to_numeric(df["sku_per_bin"], errors="coerce")
        # Non-positive or NaN values mean "no bin data for this SKU"
        df.loc[df["sku_per_bin"] <= 0, "sku_per_bin"] = None
    if "material_category" in df.columns:
        df["material_category"] = df["material_category"].astype(str).str.strip()
        df.loc[df["material_category"].isin(["", "nan", "None"]), "material_category"] = None

    # Auto-dedupe: keep first occurrence. The QC check downstream reports the dupe count.
    dupe_count = int(df["sku_code"].duplicated().sum())
    if dupe_count:
        df = df.drop_duplicates(subset=["sku_code"], keep="first").reset_index(drop=True)
    # Attach a side-channel attr the QC step can read
    df.attrs["_dedupe_count"] = dupe_count
    return df


def load_stock_on_hand(stream, filename: str) -> pd.DataFrame:
    df = _read_tabular(stream, filename, asset_hint="stock_on_hand")
    df = _normalise_columns(df)
    _assert_required(df, "stock_on_hand")
    df["sku_code"] = df["sku_code"].astype(str).str.strip()
    df["qty_on_hand"] = pd.to_numeric(df["qty_on_hand"], errors="coerce").fillna(0)
    return df


def load_forecast(stream, filename: str) -> pd.DataFrame:
    df = _read_tabular(stream, filename, asset_hint="forecast")
    df = _normalise_columns(df)
    _assert_required(df, "forecast")
    df["sku_code"] = df["sku_code"].astype(str).str.strip()
    df["forecast_date"] = pd.to_datetime(df["forecast_date"]).dt.date
    df["forecast_units"] = pd.to_numeric(df["forecast_units"], errors="coerce").fillna(0)
    return df


def _assert_required(df: pd.DataFrame, asset: str) -> None:
    missing = REQUIRED_FIELDS[asset] - set(df.columns)
    if missing:
        raise ValueError(
            f"{asset} is missing required columns after alias normalisation: {sorted(missing)}. "
            f"Columns present: {list(df.columns)}. See docs/data_schema.md."
        )
