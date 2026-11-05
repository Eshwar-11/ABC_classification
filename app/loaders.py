"""Excel / CSV loaders for the input assets."""
from __future__ import annotations
import io
from pathlib import Path
from typing import BinaryIO

import pandas as pd

from .config import REQUIRED_FIELDS


def _read_tabular(stream: BinaryIO | bytes | str | Path, filename: str) -> pd.DataFrame:
    name = filename.lower()
    data = stream.read() if hasattr(stream, "read") else stream
    if isinstance(data, (str, Path)):
        data_bytes = Path(data).read_bytes()
    else:
        data_bytes = data

    if name.endswith((".xlsx", ".xls")):
        xls = pd.ExcelFile(io.BytesIO(data_bytes))
        sheet = "Data" if "Data" in xls.sheet_names else xls.sheet_names[0]
        return pd.read_excel(xls, sheet_name=sheet)
    if name.endswith(".csv"):
        return pd.read_csv(io.BytesIO(data_bytes))
    if name.endswith(".parquet"):
        try:
            return pd.read_parquet(io.BytesIO(data_bytes))
        except ImportError as e:
            raise ValueError(
                f"Parquet support requires pyarrow. Install it or convert the file to xlsx/csv. {e}"
            )
    raise ValueError(f"Unsupported file type: {filename}. Use xlsx, csv, or parquet.")


def load_order_lines(stream, filename: str) -> pd.DataFrame:
    df = _read_tabular(stream, filename)
    _assert_required(df, "order_lines")
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
    if "status_derived" in df.columns:
        df["status_derived"] = df["status_derived"].fillna("no-history").astype(str).str.strip().str.lower()
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


def load_forecast(stream, filename: str) -> pd.DataFrame:
    df = _read_tabular(stream, filename)
    _assert_required(df, "forecast")
    df["sku_code"] = df["sku_code"].astype(str).str.strip()
    df["forecast_date"] = pd.to_datetime(df["forecast_date"]).dt.date
    df["forecast_units"] = pd.to_numeric(df["forecast_units"], errors="coerce").fillna(0)
    return df


def _assert_required(df: pd.DataFrame, asset: str) -> None:
    missing = REQUIRED_FIELDS[asset] - set(df.columns)
    if missing:
        raise ValueError(
            f"{asset} missing required columns: {sorted(missing)}. "
            f"See docs/data_schema.md for the expected schema."
        )
