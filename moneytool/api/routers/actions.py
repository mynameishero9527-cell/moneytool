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

LIST_TYPES = ("buy", "buy_invalid", "sell", "hold_watch", "point", "lowbase")
LIST_PATTERN = "^(" + "|".join(LIST_TYPES) + ")$"
STAGE_ORDER = "CASE st.stage WHEN 'start' THEN 0 WHEN 'spread' THEN 1 WHEN 'climax' THEN 2 WHEN 'freeze' THEN 3 WHEN 'diverge' THEN 4 ELSE 5 END"


@contextmanager
def write_conn(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    ctx = getattr(request.app.state, "ctx", None)
    lock = write_lock(ctx.settings.lock_path, owner="api", timeout=10) if ctx else nullcontext()
    with lock, request.app.state.db.write() as conn:
        yield conn


@router.get("/actions", response_model=Envelope)
def actions(
    list_type: str = Query(pattern=LIST_PATTERN),
    trade_date: dt.date | None = None,
    segment: str | None = Query(default=None),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    check_segment(segment)
    day = resolve_trade_date(conn, trade_date)
    meta = make_meta(conn, day, segment)
    if day is None:
        return Envelope(meta=meta, data=[])
    confirmed = segment is None or segment == "close"
    table = "action_list_confirmed" if confirmed else "action_list_intraday"
    stage_table = "sector_stage_confirmed" if confirmed else "sector_stage_intraday"
    seg_clause = "" if confirmed else " AND a.segment = ?"
    stage_seg = "" if confirmed else " AND st.segment = a.segment"
    args = [] if confirmed else [segment]
    # 默认排序：板块阶段 → 资金留存（不设全市场名次）；高风险排组内最后
    df = conn.execute(
        f"""
        SELECT a.*, s.name, sec.name AS sector_name, sec.level AS sector_level,
               st.stage AS sector_stage, st.half AS sector_half, st.days_in_stage,
               p.score, p.score_tier, r.total AS risk_total,
               TRY_CAST(json_extract(fd.features, '$.retention_5d') AS DOUBLE) AS retention_5d,
               TRY_CAST(json_extract(fd.features, '$.close') AS DOUBLE) AS close,
               TRY_CAST(json_extract(fd.features, '$.pct_chg') AS DOUBLE) AS pct_chg,
               TRY_CAST(json_extract(fd.features, '$.net_main') AS DOUBLE) AS net_main,
               TRY_CAST(json_extract(fd.features, '$.main_ratio') AS DOUBLE) AS main_ratio
        FROM {table} a
        LEFT JOIN security s ON s.code = a.code
        LEFT JOIN sector sec ON sec.sector_id = a.sector_id
        LEFT JOIN {stage_table} st ON st.sector_id = a.sector_id AND st.trade_date = a.trade_date
             AND st.param_version = a.param_version{stage_seg}
        LEFT JOIN stock_profile p ON p.code = a.code AND p.trade_date = a.trade_date
             AND p.segment = 'close' AND p.param_version = a.param_version
        LEFT JOIN index_daily r ON r.subject_type = 'stock' AND r.subject_id = a.code AND r.trade_date = a.trade_date
             AND r.index_name = 'stock_risk' AND r.segment = ? AND r.param_version = a.param_version
        LEFT JOIN feature_daily fd ON fd.subject_type = 'stock' AND fd.subject_id = a.code
             AND fd.trade_date = a.trade_date AND fd.segment = ? AND fd.param_version = a.param_version
        WHERE a.trade_date = ? AND a.list_type = ? AND a.param_version = ?{seg_clause}
        ORDER BY {STAGE_ORDER}, CAST(a.tags AS VARCHAR) LIKE '%高风险%', retention_5d DESC NULLS LAST, a.code
        """,
        [segment or "close", segment or "close", day, list_type, meta.param_version, *args],
    ).pl()
    return Envelope(meta=meta, data=rows(df, ("tags", "evidence", "invalidation")))


@router.get("/tracking", response_model=Envelope)
def tracking(
    active_only: bool = True, conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn)
) -> Envelope:
    """跟踪中：入选日与价格、至今收益及相对全 A 等权 / 依据板块的超额、当前角色与动作标签（需求 8.3）。"""
    day = resolve_trade_date(conn, None)
    meta = make_meta(conn, day)
    if day is None:
        return Envelope(meta=meta, data=[])
    df = conn.execute(
        f"""
        WITH t AS (
            SELECT * FROM tracking WHERE param_version = ? {"AND exited_date IS NULL" if active_only else ""}
        ),
        eqw AS (
            SELECT t.list_type, t.code, t.sector_id, t.entered_date,
                   exp(sum(ln(1 + m.eqw_ret))) - 1 AS eqw_since
            FROM t JOIN market_daily m ON m.segment = 'close' AND m.param_version = t.param_version
                 AND m.trade_date > t.entered_date AND m.trade_date <= coalesce(t.exited_date, ?)
            GROUP BY ALL
        ),
        sec_ret AS (
            SELECT t.list_type, t.code, t.sector_id, t.entered_date,
                   exp(sum(ln(1 + TRY_CAST(json_extract(f.features, '$.sector_pct_chg') AS DOUBLE)))) - 1 AS sector_since
            FROM t JOIN feature_daily f ON f.subject_type = 'sector' AND f.subject_id = t.sector_id
                 AND f.segment = 'close' AND f.param_version = t.param_version
                 AND f.trade_date > t.entered_date AND f.trade_date <= coalesce(t.exited_date, ?)
            GROUP BY ALL
        )
        SELECT t.*, s.name, sec.name AS sector_name, b.close AS last_close,
               b.close / NULLIF(t.entered_price, 0) - 1 AS ret_since,
               b.close / NULLIF(t.entered_price, 0) - 1 - eqw.eqw_since AS excess_vs_eqw,
               b.close / NULLIF(t.entered_price, 0) - 1 - sec_ret.sector_since AS excess_vs_sector,
               r.role AS current_role,
               (SELECT list(DISTINCT a.list_type) FROM action_list_confirmed a WHERE a.code = t.code
                 AND a.trade_date = ? AND a.param_version = t.param_version) AS current_actions
        FROM t
        LEFT JOIN security s ON s.code = t.code
        LEFT JOIN sector sec ON sec.sector_id = t.sector_id
        LEFT JOIN bar_daily b ON b.code = t.code AND b.trade_date = coalesce(t.exited_date, ?)
        LEFT JOIN eqw ON eqw.list_type = t.list_type AND eqw.code = t.code AND eqw.sector_id = t.sector_id
             AND eqw.entered_date = t.entered_date
        LEFT JOIN sec_ret ON sec_ret.list_type = t.list_type AND sec_ret.code = t.code AND sec_ret.sector_id = t.sector_id
             AND sec_ret.entered_date = t.entered_date
        LEFT JOIN stock_role_confirmed r ON r.code = t.code AND r.sector_id = t.sector_id
             AND r.trade_date = ? AND r.param_version = t.param_version
        ORDER BY t.entered_date DESC, t.code
        """,
        [meta.param_version, day, day, day, day, day],
    ).pl()
    return Envelope(meta=meta, data=rows(df))


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
