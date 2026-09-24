"""响应模型。`Meta` 是每个响应必带的口径元信息（backend-fastapi-duckdb skill）。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

from moneytool.types import DataStatus


class Meta(BaseModel):
    trade_date: dt.date | None
    segment: str | None = None
    status: DataStatus
    param_version: str | None = None
    generated_at: dt.datetime = Field(default_factory=dt.datetime.now)
    reason: str | None = None


class Envelope(BaseModel):
    meta: Meta
    data: Any = None


class StatusData(BaseModel):
    latest_confirmed: dt.date | None
    today: dt.date
    is_trading_day: bool
    segments_captured: list[str]
    risk_gate: bool
    data_status: str
    degraded_reason: str | None
    backfill: dict[str, dict[str, int]]
    quality: list[dict[str, Any]]
    counts: dict[str, int]
    param_version: str | None
    version: str


class RecomputeRequest(BaseModel):
    trade_date: dt.date
    segment: str | None = None


class MarkRequest(BaseModel):
    code: str
    mark: str  # bought / sold / ignored
    note: str | None = None
