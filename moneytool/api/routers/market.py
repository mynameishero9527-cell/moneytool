"""市场层：总览、风控开关、主线与轮动、市场情绪压力、历史序列。"""

from __future__ import annotations

import datetime as dt

import duckdb
from fastapi import APIRouter, Depends, Query

from moneytool.api.deps import (
    check_segment,
    get_ro_conn,
    make_meta,
    resolve_trade_date,
    rows,
    scalar,
    stage_table,
)
from moneytool.api.schemas import Envelope
from moneytool.types import STAGE_PRIORITY

router = APIRouter(prefix="/market")

MARKET_JSON_COLS = ("mainline", "rotation", "pressure_components", "risk_gate_reasons", "evidence")


@router.get("/overview", response_model=Envelope)
def overview(
    trade_date: dt.date | None = None,
    segment: str | None = Query(default=None),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """总览页顶部：风控、主线、轮动、压力、各阶段一级行业数。"""
    check_segment(segment)
    day = resolve_trade_date(conn, trade_date)
    meta = make_meta(conn, day, segment)
    if day is None:
        return Envelope(meta=meta, data=None)
    market = conn.execute(
        "SELECT * FROM market_daily WHERE trade_date = ? AND segment = ? ORDER BY param_version DESC LIMIT 1",
        [day, segment or "close"],
    ).pl()
    if market.is_empty():
        return Envelope(meta=meta, data=None)
    table, seg_clause = stage_table(segment)
    args: list[object] = [day]
    if seg_clause:
        args.append(segment)
    stage_counts = conn.execute(
        f"""
        SELECT s.stage, sec.level, count(*) AS n
        FROM {table} s JOIN sector sec USING (sector_id)
        WHERE s.trade_date = ?{seg_clause} AND s.param_version = ?
        GROUP BY 1, 2
        """,
        [*args, meta.param_version],
    ).pl()
    counts: dict[str, dict[str, int]] = {}
    for r in stage_counts.iter_rows(named=True):
        counts.setdefault(str(r["level"]), {})[str(r["stage"])] = int(r["n"])
    for per_level in counts.values():
        for st in STAGE_PRIORITY:
            per_level.setdefault(st.value, 0)
    data = rows(market, MARKET_JSON_COLS)[0]
    data["stage_counts"] = counts
    data["captured_segments"] = [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT segment FROM market_daily WHERE trade_date = ? ORDER BY 1", [day]
        ).fetchall()
    ]
    return Envelope(meta=meta, data=data)


@router.get("/history", response_model=Envelope)
def history(
    days: int = Query(default=60, ge=1, le=500),
    end: dt.date | None = None,
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """收盘口径的市场序列：等权收益、宽度、压力、风控开关（画图用）。"""
    day = resolve_trade_date(conn, end)
    if day is None:
        return Envelope(meta=make_meta(conn, None), data=[])
    df = conn.execute(
        """
        SELECT trade_date, eqw_ret, eqw_ret_5d, breadth_all, limit_up_count, amount_all, net_main_all,
               regime, market_pressure, risk_gate, data_status, param_version
        FROM market_daily WHERE segment = 'close' AND trade_date <= ?
        QUALIFY row_number() OVER (PARTITION BY trade_date ORDER BY param_version DESC) = 1
        ORDER BY trade_date DESC LIMIT ?
        """,
        [day, days],
    ).pl()
    return Envelope(meta=make_meta(conn, day), data=rows(df.sort("trade_date")))


@router.get("/gate", response_model=Envelope)
def gate(
    trade_date: dt.date | None = None,
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """风控开关与触发依据（需求 7.6）。"""
    day = resolve_trade_date(conn, trade_date)
    meta = make_meta(conn, day)
    if day is None:
        return Envelope(meta=meta, data=None)
    df = conn.execute(
        "SELECT trade_date, risk_gate, risk_gate_reasons, evidence, market_pressure, pressure_components "
        "FROM market_daily WHERE trade_date = ? AND segment = 'close' ORDER BY param_version DESC LIMIT 1",
        [day],
    ).pl()
    if df.is_empty():
        return Envelope(meta=meta, data=None)
    data = rows(df, ("risk_gate_reasons", "evidence", "pressure_components"))[0]
    data["gate_days"] = int(
        scalar(
            conn,
            """
            SELECT count(*) FROM market_daily WHERE segment = 'close' AND risk_gate
            AND trade_date > coalesce(
                (SELECT max(trade_date) FROM market_daily WHERE segment = 'close' AND NOT risk_gate AND trade_date <= ?),
                DATE '1900-01-01')
            AND trade_date <= ?
            """,
            [day, day],
        )
        or 0
    )
    return Envelope(meta=meta, data=data)
