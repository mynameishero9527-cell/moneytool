"""行动名单、跟踪、用户标记、自选。

名单与角色由计算链 ⑤–⑦ 产出；本版接口先读表返回（空表即空名单），前端按 `meta.status` 展示。
写操作（标记 / 自选）走 `write_conn`：持文件锁 + 进程内写连接，保持「写只在一处」。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext

import duckdb
import polars as pl
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from moneytool.api.deps import check_segment, get_ro_conn, make_meta, resolve_trade_date, rows
from moneytool.api.schemas import Envelope, MarkRequest, Meta
from moneytool.lock import write_lock
from moneytool.storage.repo import upsert
from moneytool.types import DataStatus

router = APIRouter()

LIST_TYPES = ("buy", "sell", "point", "lowbase")


@contextmanager
def write_conn(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    ctx = getattr(request.app.state, "ctx", None)
    lock = write_lock(ctx.settings.lock_path, owner="api", timeout=10) if ctx else nullcontext()
    with lock, request.app.state.db.write() as conn:
        yield conn


@router.get("/actions", response_model=Envelope)
def actions(
    list_type: str = Query(pattern="^(buy|sell|point|lowbase)$"),
    trade_date: dt.date | None = None,
    segment: str | None = Query(default=None),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    check_segment(segment)
    day = resolve_trade_date(conn, trade_date)
    meta = make_meta(conn, day, segment)
    if day is None:
        return Envelope(meta=meta, data=[])
    if segment is None or segment == "close":
        table, seg_clause, args = "action_list_confirmed", "", []
    else:
        table, seg_clause, args = "action_list_intraday", " AND a.segment = ?", [segment]
    df = conn.execute(
        f"""
        SELECT a.*, s.name, sec.name AS sector_name, f.net_main, f.main_ratio, f.pct_chg, f.close
        FROM {table} a
        LEFT JOIN security s ON s.code = a.code
        LEFT JOIN sector sec ON sec.sector_id = a.sector_id
        LEFT JOIN flow_daily f ON f.code = a.code AND f.trade_date = a.trade_date
        WHERE a.trade_date = ? AND a.list_type = ?{seg_clause}
        ORDER BY a.sector_id, a.code
        """,
        [day, list_type, *args],
    ).pl()
    return Envelope(meta=meta, data=rows(df, ("tags", "evidence", "invalidation")))


@router.get("/tracking", response_model=Envelope)
def tracking(
    active_only: bool = True, conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn)
) -> Envelope:
    df = conn.execute(
        "SELECT t.*, s.name, sec.name AS sector_name FROM tracking t "
        "LEFT JOIN security s ON s.code = t.code LEFT JOIN sector sec ON sec.sector_id = t.sector_id "
        + ("WHERE t.exited_date IS NULL " if active_only else "")
        + "ORDER BY t.entered_date DESC"
    ).pl()
    return Envelope(meta=make_meta(conn, resolve_trade_date(conn, None)), data=rows(df))


@router.get("/marks", response_model=Envelope)
def marks(
    code: str | None = None, conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn)
) -> Envelope:
    df = conn.execute(
        "SELECT m.code, s.name, m.marked_at, m.mark, m.note FROM user_mark m LEFT JOIN security s USING (code) "
        + ("WHERE m.code = ? " if code else "")
        + "ORDER BY m.marked_at DESC LIMIT 200",
        [code] if code else [],
    ).pl()
    return Envelope(meta=Meta(trade_date=None, status=DataStatus.CONFIRMED), data=rows(df))


@router.post("/marks", response_model=Envelope, status_code=201)
def add_mark(body: MarkRequest, request: Request) -> Envelope:
    if body.mark not in ("bought", "sold", "ignored"):
        raise HTTPException(status_code=422, detail="mark 只能是 bought / sold / ignored")
    now = dt.datetime.now(dt.UTC)
    with write_conn(request) as conn:
        upsert(
            conn,
            "user_mark",
            pl.DataFrame(
                {"code": [body.code], "marked_at": [now], "mark": [body.mark], "note": [body.note]},
                schema_overrides={"note": pl.Utf8},
            ),
        )
    return Envelope(
        meta=Meta(trade_date=None, status=DataStatus.CONFIRMED),
        data={"code": body.code, "marked_at": now, "mark": body.mark, "note": body.note},
    )


@router.get("/watchlist", response_model=Envelope)
def watchlist(
    trade_date: dt.date | None = None, conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn)
) -> Envelope:
    day = resolve_trade_date(conn, trade_date)
    df = conn.execute(
        """
        SELECT w.code, w.group_id, w.added_at, s.name, f.net_main, f.main_ratio, f.pct_chg, f.close, h.eval AS hold_eval
        FROM watchlist w LEFT JOIN security s ON s.code = w.code
        LEFT JOIN flow_daily f ON f.code = w.code AND f.trade_date = ?
        LEFT JOIN hold_eval h ON h.code = w.code AND h.trade_date = ?
        ORDER BY w.group_id, w.added_at
        """,
        [day, day],
    ).pl()
    return Envelope(meta=make_meta(conn, day), data=rows(df))


@router.post("/watchlist/{code}", response_model=Envelope, status_code=201)
def add_watch(code: str, request: Request, group_id: str = "default") -> Envelope:
    with write_conn(request) as conn:
        exists = conn.execute("SELECT 1 FROM security WHERE code = ?", [code]).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="证券不存在")
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (code, group_id) VALUES (?, ?)", [code, group_id]
        )
    return Envelope(
        meta=Meta(trade_date=None, status=DataStatus.CONFIRMED),
        data={"code": code, "group_id": group_id},
    )


@router.delete("/watchlist/{code}", response_model=Envelope)
def remove_watch(code: str, request: Request, group_id: str = "default") -> Envelope:
    with write_conn(request) as conn:
        conn.execute("DELETE FROM watchlist WHERE code = ? AND group_id = ?", [code, group_id])
    return Envelope(
        meta=Meta(trade_date=None, status=DataStatus.CONFIRMED),
        data={"code": code, "group_id": group_id},
    )
