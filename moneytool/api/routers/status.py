"""数据状态、搜索、参数口径、管理接口。"""

from __future__ import annotations

import datetime as dt
from typing import Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from moneytool import __version__
from moneytool.api.deps import get_ro_conn, make_meta, resolve_trade_date, rows, scalar
from moneytool.api.schemas import Envelope, Meta, RecomputeRequest, StatusData
from moneytool.app import today_sh
from moneytool.ingest.flow import segments_captured
from moneytool.params import latest_params
from moneytool.storage.repo import is_trading_day, latest_confirmed_date, quality_for_date
from moneytool.types import DataStatus

router = APIRouter()


@router.get("/status", response_model=Envelope)
def status(conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn)) -> Envelope:
    today = today_sh()
    latest = latest_confirmed_date(conn)
    gate_row = conn.execute(
        "SELECT risk_gate, data_status, param_version FROM market_daily WHERE segment = 'close' ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    progress = conn.execute(
        "SELECT task, status, count(*) FROM backfill_progress GROUP BY task, status"
    ).fetchall()
    backfill: dict[str, dict[str, int]] = {}
    for task, st, n in progress:
        backfill.setdefault(str(task), {})[str(st)] = int(n)
    total_sec = int(scalar(conn, "SELECT count(*) FROM security WHERE NOT is_delisting") or 0)
    for task in ("bars", "flow_daily"):
        backfill.setdefault(task, {})["total"] = total_sec
    counts = {
        "security": int(scalar(conn, "SELECT count(*) FROM security") or 0),
        "sector": int(scalar(conn, "SELECT count(*) FROM sector") or 0),
        "bar_days": int(scalar(conn, "SELECT count(DISTINCT trade_date) FROM bar_daily") or 0),
        "flow_days": int(scalar(conn, "SELECT count(DISTINCT trade_date) FROM flow_daily") or 0),
        "confirmed_days": int(
            scalar(conn, "SELECT count(DISTINCT trade_date) FROM sector_stage_confirmed") or 0
        ),
    }
    data_status = str(gate_row[1]) if gate_row else DataStatus.MISSING.value
    data = StatusData(
        latest_confirmed=latest,
        today=today,
        is_trading_day=is_trading_day(conn, today),
        segments_captured=segments_captured(conn, today),
        risk_gate=bool(gate_row[0]) if gate_row else False,
        data_status=data_status,
        degraded_reason="当日资金流全部缺失，价格模式"
        if data_status == DataStatus.DEGRADED.value
        else None,
        backfill=backfill,
        quality=rows(quality_for_date(conn, today)),
        counts=counts,
        param_version=str(gate_row[2]) if gate_row else None,
        version=__version__,
    )
    meta = Meta(trade_date=latest, status=DataStatus(data_status) if latest else DataStatus.MISSING)
    return Envelope(meta=meta, data=data.model_dump())


@router.get("/quality", response_model=Envelope)
def quality(
    trade_date: dt.date | None = None, conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn)
) -> Envelope:
    day = resolve_trade_date(conn, trade_date)
    data = rows(quality_for_date(conn, day)) if day else []
    return Envelope(meta=make_meta(conn, day), data=data)


@router.get("/search", response_model=Envelope)
def search(
    q: str = Query(min_length=1, max_length=20),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """代码、名称、拼音首字母三路匹配（需求 10 节）。"""
    like = f"%{q.upper()}%"
    stocks = conn.execute(
        "SELECT code, name, 'stock' AS kind FROM security "
        "WHERE code LIKE ? OR name LIKE ? OR pinyin_initials LIKE ? ORDER BY code LIMIT 20",
        [like, f"%{q}%", like],
    ).pl()
    sectors = conn.execute(
        "SELECT sector_id AS code, name, level AS kind FROM sector WHERE name LIKE ? ORDER BY level, sector_id LIMIT 20",
        [f"%{q}%"],
    ).pl()
    return Envelope(
        meta=Meta(trade_date=None, status=DataStatus.CONFIRMED),
        data={"stocks": rows(stocks), "sectors": rows(sectors)},
    )


@router.get("/params", response_model=Envelope)
def params(request: Request, conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn)) -> Envelope:
    """当前参数版本与全部阈值（需求 9.8 口径页）。"""
    p = latest_params(request.app.state.params_dir)
    return Envelope(
        meta=Meta(trade_date=None, status=DataStatus.CONFIRMED, param_version=p.version),
        data=p.model_dump(mode="json"),
    )


@router.post("/admin/recompute", response_model=Envelope)
def admin_recompute(body: RecomputeRequest, request: Request) -> Envelope:
    """主进程内排队重算（架构 4.1.3：命令行在主进程运行时改走这里）。只接受回环地址。"""
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(status_code=403, detail="仅本机可用")
    runner = request.app.state.runner
    if runner is None:
        raise HTTPException(status_code=503, detail="调度器未启动")
    from moneytool.compute.pipeline import run  # noqa: PLC0415

    def work(conn: duckdb.DuckDBPyConnection) -> Any:
        res = run(
            conn, runner.ctx.params_for(body.trade_date), body.trade_date, segment=body.segment
        )
        return res.stage_rows

    out = runner.run_job("recompute", work, segment=body.segment or "")
    return Envelope(
        meta=Meta(trade_date=body.trade_date, segment=body.segment, status=DataStatus.CONFIRMED),
        data={"stage_rows": out},
    )
