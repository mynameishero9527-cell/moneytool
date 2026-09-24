"""回看（需求 9.7 / 9.7.1 / 9.9）：标签事后统计与每日复盘简报。"""

from __future__ import annotations

import datetime as dt

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from moneytool.api.deps import get_ro_conn, make_meta, resolve_trade_date, rows
from moneytool.api.schemas import Envelope, Meta
from moneytool.params import latest_params
from moneytool.types import DataStatus

router = APIRouter()


@router.get("/stats", response_model=Envelope)
def stats(
    request: Request,
    param_version: str | None = None,
    since: dt.date | None = None,
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """按名单 / 买卖点类型 / 持有评估档 × 观察窗口 × 风控状态分组。样本不足的组标 `enough=false`。"""
    p = latest_params(request.app.state.params_dir)
    version = param_version or p.version
    df = conn.execute(
        """
        SELECT list_type, coalesce(point_type, '') AS point_type, horizon, risk_gate,
               count(*) AS n,
               avg(ret) AS mean_ret, median(ret) AS median_ret,
               avg(CASE WHEN ret > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
               avg(excess_vs_sector) AS mean_excess_sector,
               avg(excess_vs_eqw) AS mean_excess_eqw,
               avg(max_drawdown) AS mean_max_drawdown
        FROM label_outcome
        WHERE param_version = ? AND ret IS NOT NULL AND (? IS NULL OR entered_date >= ?)
        GROUP BY ALL
        ORDER BY list_type, point_type, horizon, risk_gate
        """,
        [version, since, since],
    ).pl()
    data = rows(df)
    for r in data:
        r["enough"] = int(r["n"]) >= p.labels.min_samples
    return Envelope(
        meta=Meta(trade_date=None, status=DataStatus.CONFIRMED, param_version=version),
        data={
            "min_samples": p.labels.min_samples,
            "horizons": {k: list(v) for k, v in p.labels.horizons.items()},
            "groups": data,
        },
    )


@router.get("/brief", response_model=Envelope)
def brief(
    trade_date: dt.date | None = None,
    kind: str = Query(default="close", pattern="^(close|premarket)$"),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    day = resolve_trade_date(conn, trade_date)
    meta = make_meta(conn, day)
    df = conn.execute(
        "SELECT trade_date, kind, markdown, generated_at FROM brief WHERE kind = ? AND (? IS NULL OR trade_date = ?) "
        "ORDER BY trade_date DESC LIMIT 1",
        [kind, day, day],
    ).pl()
    if df.is_empty():
        raise HTTPException(status_code=404, detail="当日暂无简报")
    return Envelope(meta=meta, data=rows(df)[0])


@router.get("/briefs", response_model=Envelope)
def briefs(
    limit: int = Query(default=30, ge=1, le=250),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    df = conn.execute(
        "SELECT trade_date, kind, generated_at FROM brief ORDER BY trade_date DESC, kind LIMIT ?",
        [limit],
    ).pl()
    return Envelope(meta=Meta(trade_date=None, status=DataStatus.CONFIRMED), data=rows(df))
