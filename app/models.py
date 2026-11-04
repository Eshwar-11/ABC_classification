"""Pydantic models for API requests and responses."""
from __future__ import annotations
from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class ClassSummary(BaseModel):
    cls: str = Field(alias="class")
    skus: int
    sku_pct: float
    volume: float
    volume_pct: float
    frequency: int
    frequency_pct: float
    dormant_count: int
    avg_doi_days: float | None

    class Config:
        populate_by_name = True


class ParetoPoint(BaseModel):
    top_pct: int
    skus: int
    cum_volume_pct: float
    cum_frequency_pct: float


class RunMetaResponse(BaseModel):
    run_id: str
    created_at: str
    warehouse_code: str
    site_name: str
    basis: str
    window_days: int
    frequency_weight: float
    volume_weight: float
    a_cutoff_pct: float
    b_cutoff_pct: float
    universe_size: int
    scorable_size: int
    a_cutoff_rank: int
    b_cutoff_rank: int
    window_start: date | None
    window_end: date | None


class RunResultResponse(BaseModel):
    meta: RunMetaResponse
    classes: list[ClassSummary]
    pareto_scan: list[ParetoPoint]
    quality_checks: list[dict[str, Any]]
