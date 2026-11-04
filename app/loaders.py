"""Excel / CSV loaders for the three input assets.

Accepts the schema produced by the SQL in docs/data_schema.md. Non-matching columns
are tolerated with a warning, as long as the required fields are present.
"""
from __future__ import annotations
import io
from pathlib import Path
from typing import BinaryIO

import pandas as pd

from .config import REQUIRED_FIELDS


def _read_tabular(stream: BinaryIO | bytes | str | Path, filename: str) -> pd.DataFrame:
    """Read an xlsx or csv file into a DataFrame. Picks the 'Data' sheet if present."""
    name = filename.lower()
    data = stream.read() if hasattr(stream, "read") else stream
    if isinstance(data, (str, Path)):
        data_bytes = Path(data).read_bytes()
    else:
        data_bytes = data

    if name.endswith(".xlsx") or name.endswith(".xls"):
        xls = pd.ExcelFile(io.BytesIO(data_bytes))
        sheet = "Data" if "Data" in xls.sheet_names else xls.sheet_names[0]
        return pd.read_excel(xls, sheet_name=sheet)
    if name.endswith(".csv"):
        return pd.read_csv(io.BytesIO(data_bytes))
    if name.endswith(".parquet"):
        return pd.read_parquet(io.BytesIO(data_bytes))
    raise ValueError(f"Unsupported file type: {filename}. Use xlsx, csv, or parquet.")


def load_order_lines(stream, filename: str) -> pd.DataFrame:
    df = _read_tabular(stream, filename)
    _assert_required(df, "order_lines")
    # Normalise
    df["sku_code"] = df["sku_code"].astype(str).str.strip()
    df["pick_date"] = pd.to_datetime(df["pick_date"]).dt.date
    df["qty_shipped"] = pd.to_numeric(df["qty_shipped"], errors="coerce").fillna(0)
    df["n_tasks"] = pd.to_numeric(df["n_tasks"], errors="coerce").fillna(0).astype(int)
    df = df[df["qty_shipped"] > 0].copy()
    return df


def load_sku_master(stream, filename: str) -> pd.DataFrame:
    df = _read_tabular(stream, filename)
    _assert_required(df, "sku_master")
    df["sku_code"] = df["sku_code"].astype(str).str.strip()
    # Normalise status
    if "status_derived" in df.columns:
        df["status_derived"] = df["status_derived"].fillna("no-history").astype(str).str.strip().str.lower()
    # Map empty strings in velocity_declared to NaN
    if "velocity_declared" in df.columns:
        df["velocity_declared"] = df["velocity_declared"].astype(str).str.strip()
        df.loc[df["velocity_declared"].isin(["", "nan", "None"]), "velocity_declared"] = None
    return df


def load_stock_on_hand(stream, filename: str) -> pd.DataFrame:
    df = _read_tabular(stream, filename)
    _assert_required(df, "stock_on_hand")
    df["sku_code"] = df["sku_code"].astype(str).str.strip()
    df["qty_on_hand"] = pd.to_numeric(df["qty_on_hand"], errors="coerce").fillna(0)
    return df


def _assert_required(df: pd.DataFrame, asset: str) -> None:
    missing = REQUIRED_FIELDS[asset] - set(df.columns)
    if missing:
        raise ValueError(
            f"{asset} missing required columns: {sorted(missing)}. "
            f"See docs/data_schema.md for the expected schema."
        )
