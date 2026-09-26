"""依赖：只读连接、交易日解析、状态解析。"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from collections.abc import Iterator
from typing import Any

import duckdb
import polars as pl
from fastapi import Depends, HTTPException, Request

from moneytool.api.schemas import Meta
from moneytool.storage.conn import Database
from moneytool.types import DataStatus, Segment

VALID_SEGMENTS = {s.value for s in Segment} | {"close"}


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_ro_conn(db: Database = Depends(get_db)) -> Iterator[duckdb.DuckDBPyConnection]:
    with db.read() as conn:
        yield conn


def scalar(conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> Any:
    row = conn.execute(sql, params or []).fetchone()
    return None if row is None else row[0]


def check_segment(segment: str | None) -> str | None:
    if segment is not None and segment not in VALID_SEGMENTS:
        raise HTTPException(status_code=422, detail=f"未知分段 {segment}")
    return segment


def resolve_trade_date(
    conn: duckdb.DuckDBPyConnection, trade_date: dt.date | None
) -> dt.date | None:
    """缺省为最近一个有 confirmed 结果的交易日；无结果则最近交易日。"""
    if trade_date is not None:
        return trade_date
    latest = scalar(conn, "SELECT max(trade_date) FROM sector_stage_confirmed")
    if latest is not None:
        return dt.date.fromisoformat(str(latest))
    latest = scalar(
        conn, "SELECT max(trade_date) FROM trade_calendar WHERE is_open AND trade_date <= today()"
    )
    return None if latest is None else dt.date.fromisoformat(str(latest))


def latest_segment(conn: duckdb.DuckDBPyConnection, day: dt.date) -> str | None:
    """当日最后一个已计算的盘中分段（无则 None）。"""
    seg = scalar(
        conn,
        "SELECT segment FROM market_daily WHERE trade_date = ? AND segment <> 'close' "
        "ORDER BY segment DESC LIMIT 1",
        [day],
    )
    return None if seg is None else str(seg)


def market_status(
    conn: duckdb.DuckDBPyConnection, day: dt.date | None, segment: str | None
) -> tuple[DataStatus, str | None]:
    if day is None:
        return (
            DataStatus.MISSING,
            "尚无计算结果：首次运行需先回补历史数据（进度见「数据状态」页），完成后自动计算",
        )
    row = conn.execute(
        "SELECT data_status FROM market_daily WHERE trade_date = ? AND segment = ? ORDER BY param_version DESC LIMIT 1",
        [day, segment or "close"],
    ).fetchone()
    if row is None:
        return DataStatus.MISSING, "该日无计算结果"
    return DataStatus(row[0]), None


def latest_version(
    conn: duckdb.DuckDBPyConnection, day: dt.date | None, segment: str | None = None
) -> str | None:
    if day is None:
        return None
    v = scalar(
        conn,
        "SELECT param_version FROM market_daily WHERE trade_date = ? AND segment = ? ORDER BY param_version DESC LIMIT 1",
        [day, segment or "close"],
    )
    return None if v is None else str(v)


def make_meta(
    conn: duckdb.DuckDBPyConnection, day: dt.date | None, segment: str | None = None
) -> Meta:
    status, reason = market_status(conn, day, segment)
    return Meta(
        trade_date=day,
        segment=segment,
        status=status,
        param_version=latest_version(conn, day, segment),
        reason=reason,
    )


def rows(df: pl.DataFrame, json_cols: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """DataFrame → dict 列表；JSON 字串列反序列化。"""
    out = df.to_dicts()
    for r in out:
        for c in json_cols:
            v = r.get(c)
            if isinstance(v, str):
                with contextlib.suppress(json.JSONDecodeError):
                    r[c] = json.loads(v)
    return out


def stage_table(segment: str | None) -> tuple[str, str]:
    """(表名, 分段过滤子句)。intraday 表多一列 segment。"""
    if segment is None or segment == "close":
        return "sector_stage_confirmed", ""
    return "sector_stage_intraday", " AND s.segment = ?"
