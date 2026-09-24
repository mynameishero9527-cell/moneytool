"""个股层：详情（证券信息、所属板块、派生量、角色、指数、提示、用户标记）与序列。"""

from __future__ import annotations

import datetime as dt

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

from moneytool.api.deps import get_ro_conn, make_meta, resolve_trade_date, rows
from moneytool.api.routers.sectors import features_of
from moneytool.api.schemas import Envelope

router = APIRouter(prefix="/stocks")


def _security(conn: duckdb.DuckDBPyConnection, code: str) -> dict[str, object]:
    sec = conn.execute("SELECT * FROM security WHERE code = ?", [code]).pl()
    if sec.is_empty():
        raise HTTPException(status_code=404, detail="证券不存在")
    return rows(sec)[0]


@router.get("/{code}", response_model=Envelope)
def detail(
    code: str,
    trade_date: dt.date | None = None,
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    day = resolve_trade_date(conn, trade_date)
    meta = make_meta(conn, day)
    security = _security(conn, code)
    if day is None:
        return Envelope(meta=meta, data={"security": security})
    sectors = conn.execute(
        """
        WITH latest AS (
            SELECT sector_id, max(snapshot_date) AS snapshot_date FROM sector_member_snapshot
            WHERE snapshot_date <= ? GROUP BY sector_id
        )
        SELECT sec.sector_id, sec.name, sec.level, st.stage, st.days_in_stage
        FROM sector_member_snapshot m JOIN latest USING (sector_id, snapshot_date)
        JOIN sector sec USING (sector_id)
        LEFT JOIN sector_stage_confirmed st ON st.sector_id = sec.sector_id AND st.trade_date = ?
             AND st.param_version = ?
        WHERE m.code = ? ORDER BY sec.level, sec.sector_id
        """,
        [day, day, meta.param_version, code],
    ).pl()
    roles = conn.execute(
        "SELECT r.sector_id, sec.name AS sector_name, sec.level, r.role, r.tags, r.score, r.score_components, "
        "r.evidence FROM stock_role_confirmed r LEFT JOIN sector sec USING (sector_id) "
        "WHERE r.code = ? AND r.trade_date = ? AND r.param_version = ? ORDER BY sec.level DESC, r.sector_id",
        [code, day, meta.param_version],
    ).pl()
    version = meta.param_version
    indices = conn.execute(
        "SELECT index_name, total, components, window_ok, degraded FROM index_daily "
        "WHERE subject_type = 'stock' AND subject_id = ? AND trade_date = ? AND segment = 'close' "
        "AND param_version = ? ORDER BY index_name",
        [code, day, version],
    ).pl()
    hold = conn.execute(
        "SELECT eval, changed_from, evidence FROM hold_eval WHERE code = ? AND trade_date = ? AND param_version = ?",
        [code, day, version],
    ).pl()
    hints = conn.execute(
        "SELECT template_id, tier, text, links FROM hint WHERE subject_type = 'stock' AND subject_id = ? "
        "AND trade_date = ? AND segment = 'close' AND param_version = ?",
        [code, day, version],
    ).pl()
    profile = conn.execute(
        "SELECT p.basis_sector, sec.name AS basis_sector_name, sec.level AS basis_level, p.identity, p.exclusions, "
        "p.tradable, p.score, p.score_tier, p.score_components FROM stock_profile p "
        "LEFT JOIN sector sec ON sec.sector_id = p.basis_sector "
        "WHERE p.code = ? AND p.trade_date = ? AND p.segment = 'close' AND p.param_version = ?",
        [code, day, version],
    ).pl()
    actions = conn.execute(
        "SELECT a.sector_id, sec.name AS sector_name, a.list_type, a.point_type, a.basis_level, a.tags, "
        "a.evidence, a.invalidation FROM action_list_confirmed a LEFT JOIN sector sec USING (sector_id) "
        "WHERE a.code = ? AND a.trade_date = ? AND a.param_version = ? ORDER BY a.list_type, a.sector_id",
        [code, day, version],
    ).pl()
    tracking = conn.execute(
        "SELECT t.*, sec.name AS sector_name FROM tracking t LEFT JOIN sector sec USING (sector_id) "
        "WHERE t.code = ? AND t.param_version = ? ORDER BY t.entered_date DESC LIMIT 20",
        [code, version],
    ).pl()
    marks = conn.execute(
        "SELECT marked_at, mark, note FROM user_mark WHERE code = ? ORDER BY marked_at DESC LIMIT 20",
        [code],
    ).pl()
    flow = conn.execute(
        "SELECT net_main, net_super, net_large, net_medium, net_small, main_ratio, close, pct_chg, reconciled, reconcile_diff "
        "FROM flow_daily WHERE code = ? AND trade_date = ?",
        [code, day],
    ).pl()
    bar = conn.execute(
        "SELECT open, high, low, close, pre_close, amount, turnover, pct_chg, limit_up, float_mv, is_suspended "
        "FROM bar_daily WHERE code = ? AND trade_date = ?",
        [code, day],
    ).pl()
    return Envelope(
        meta=meta,
        data={
            "security": security,
            "sectors": rows(sectors),
            "bar": rows(bar)[0] if not bar.is_empty() else None,
            "flow": rows(flow)[0] if not flow.is_empty() else None,
            "features": features_of(conn, "stock", code, day, meta.param_version),
            "roles": rows(roles, ("tags", "score_components", "evidence")),
            "indices": rows(indices, ("components",)),
            "hold_eval": rows(hold, ("evidence",))[0] if not hold.is_empty() else None,
            "hints": rows(hints, ("links",)),
            "profile": rows(profile, ("identity", "exclusions", "score_components"))[0]
            if not profile.is_empty()
            else None,
            "actions": rows(actions, ("tags", "evidence", "invalidation")),
            "tracking": rows(tracking),
            "marks": rows(marks),
        },
    )


@router.get("/{code}/history", response_model=Envelope)
def history(
    code: str,
    days: int = Query(default=120, ge=5, le=500),
    end: dt.date | None = None,
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """日线 + 资金流序列（个股主图）。资金缺失日 net_main 为 null，不填补。"""
    day = resolve_trade_date(conn, end)
    meta = make_meta(conn, day)
    _security(conn, code)
    if day is None:
        return Envelope(meta=meta, data=[])
    df = conn.execute(
        """
        SELECT b.trade_date, b.open, b.high, b.low, b.close, b.amount, b.turnover, b.pct_chg, b.limit_up,
               f.net_main, f.net_super, f.net_large, f.net_medium, f.net_small, f.main_ratio, f.reconciled
        FROM bar_daily b LEFT JOIN flow_daily f USING (code, trade_date)
        WHERE b.code = ? AND b.trade_date <= ? ORDER BY b.trade_date DESC LIMIT ?
        """,
        [code, day, days],
    ).pl()
    return Envelope(meta=meta, data=rows(df.sort("trade_date")))


@router.get("/{code}/intraday", response_model=Envelope)
def intraday(
    code: str,
    trade_date: dt.date | None = None,
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """分段资金（需求 6.4）：各分段增量与 spans_missing 标记。"""
    day = resolve_trade_date(conn, trade_date)
    _security(conn, code)
    if day is None:
        return Envelope(meta=make_meta(conn, None), data=[])
    df = conn.execute(
        "SELECT segment, net_main, net_super, net_large, net_medium, net_small, spans_missing "
        "FROM flow_intraday WHERE code = ? AND trade_date = ? ORDER BY segment",
        [code, day],
    ).pl()
    return Envelope(meta=make_meta(conn, day), data=rows(df))
