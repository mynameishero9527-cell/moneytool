"""板块层：看板列表、详情（证据 / 历史 / 二级分布 / 成分）、对比、历史。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

from moneytool.api.deps import (
    check_segment,
    get_ro_conn,
    make_meta,
    resolve_trade_date,
    rows,
    stage_table,
)
from moneytool.api.schemas import Envelope
from moneytool.ingest.reference import SNAPSHOT_AS_OF
from moneytool.types import STAGE_PRIORITY, Stage

router = APIRouter(prefix="/sectors")

STAGE_JSON_COLS = ("suppressed", "attribution", "evidence")
# 看板列表只带少量派生量，避免每行都传整包 features
BOARD_FEATURES = (
    "sector_net_main",
    "sector_main_ratio",
    "main_mean_5d",
    "main_multiple_prev5",
    "main_slope_5d",
    "sector_pct_chg",
    "ret_5d",
    "ret_20d",
    "market_share",
    "breadth",
    "limit_up_count",
    "turnover_vs_20d",
    "amount_tier",
    "member_count",
    "small_sample",
)


def _stage_rows(
    conn: duckdb.DuckDBPyConnection,
    day: dt.date,
    segment: str | None,
    version: str | None,
    *,
    where: str = "",
    args: list[Any] | None = None,
) -> list[dict[str, Any]]:
    table, seg_clause = stage_table(segment)
    feat_seg = "s.segment" if seg_clause else "'close'"
    params: list[Any] = [day]
    if seg_clause:
        params.append(segment)
    if version is not None:
        params.append(version)
    params.extend(args or [])
    df = conn.execute(
        f"""
        SELECT s.*, sec.name, sec.level, sec.parent_id, sec.dominant_l1, f.features
        FROM {table} s
        JOIN sector sec USING (sector_id)
        LEFT JOIN feature_daily f ON f.subject_type = 'sector' AND f.subject_id = s.sector_id
             AND f.trade_date = s.trade_date AND f.segment = {feat_seg} AND f.param_version = s.param_version
        WHERE s.trade_date = ?{seg_clause}{" AND s.param_version = ?" if version else ""}{where}
        ORDER BY sec.level, s.sector_id
        """,
        params,
    ).pl()
    return rows(df, (*STAGE_JSON_COLS, "features"))


def _trim_features(items: list[dict[str, Any]]) -> None:
    for it in items:
        feats = it.pop("features", None) or {}
        it["metrics"] = {k: feats.get(k) for k in BOARD_FEATURES}


@router.get("", response_model=Envelope)
def list_sectors(
    trade_date: dt.date | None = None,
    segment: str | None = Query(default=None),
    level: str | None = Query(default=None, pattern="^(L1|L2|concept)$"),
    stage: str | None = Query(default=None),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """看板：按层级 / 阶段筛选，阶段按优先级排序，同阶段按主力净流入市场份额降序。"""
    check_segment(segment)
    if stage is not None and stage not in {s.value for s in Stage}:
        raise HTTPException(status_code=422, detail=f"未知阶段 {stage}")
    day = resolve_trade_date(conn, trade_date)
    meta = make_meta(conn, day, segment)
    if day is None:
        return Envelope(meta=meta, data=[])
    where = ""
    args: list[Any] = []
    if level:
        where += " AND sec.level = ?"
        args.append(level)
    if stage:
        where += " AND s.stage = ?"
        args.append(stage)
    items = _stage_rows(conn, day, segment, meta.param_version, where=where, args=args)
    _trim_features(items)
    order = {s.value: i for i, s in enumerate(STAGE_PRIORITY)}
    items.sort(
        key=lambda r: (
            order.get(str(r["stage"]), 99),
            -(r["metrics"].get("market_share") or 0.0),
        )
    )
    return Envelope(meta=meta, data=items)


@router.get("/compare", response_model=Envelope)
def compare(
    ids: str = Query(min_length=1, description="逗号分隔的 sector_id，最多 6 个"),
    trade_date: dt.date | None = None,
    days: int = Query(default=60, ge=5, le=250),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """多板块对比：各自阶段序列与 5 日主力占比、20 日收益（需求 9.4）。"""
    sector_ids = [s.strip() for s in ids.split(",") if s.strip()][:6]
    day = resolve_trade_date(conn, trade_date)
    meta = make_meta(conn, day)
    if day is None:
        return Envelope(meta=meta, data=[])
    placeholders = ", ".join("?" for _ in sector_ids)
    df = conn.execute(
        f"""
        SELECT s.sector_id, sec.name, s.trade_date, s.stage, s.days_in_stage, f.features
        FROM sector_stage_confirmed s
        JOIN sector sec USING (sector_id)
        LEFT JOIN feature_daily f ON f.subject_type = 'sector' AND f.subject_id = s.sector_id
             AND f.trade_date = s.trade_date AND f.segment = 'close' AND f.param_version = s.param_version
        WHERE s.sector_id IN ({placeholders}) AND s.trade_date <= ?
        QUALIFY row_number() OVER (PARTITION BY s.sector_id, s.trade_date ORDER BY s.param_version DESC) = 1
        ORDER BY s.sector_id, s.trade_date DESC
        """,
        [*sector_ids, day],
    ).pl()
    out: list[dict[str, Any]] = []
    for sid in sector_ids:
        part = df.filter(df["sector_id"] == sid).head(days).sort("trade_date")
        series = rows(part, ("features",))
        _trim_features(series)
        out.append(
            {
                "sector_id": sid,
                "name": series[-1]["name"] if series else None,
                "series": [
                    {
                        "trade_date": r["trade_date"],
                        "stage": r["stage"],
                        "days_in_stage": r["days_in_stage"],
                        **r["metrics"],
                    }
                    for r in series
                ],
            }
        )
    return Envelope(meta=meta, data=out)


@router.get("/{sector_id}", response_model=Envelope)
def detail(
    sector_id: str,
    trade_date: dt.date | None = None,
    segment: str | None = Query(default=None),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """板块详情：阶段与证据、全部派生量、二级分布、成分个股当日资金与价格、指数。"""
    check_segment(segment)
    day = resolve_trade_date(conn, trade_date)
    meta = make_meta(conn, day, segment)
    sec = conn.execute("SELECT * FROM sector WHERE sector_id = ?", [sector_id]).pl()
    if sec.is_empty():
        raise HTTPException(status_code=404, detail="板块不存在")
    if day is None:
        return Envelope(meta=meta, data={"sector": rows(sec)[0]})
    items = _stage_rows(
        conn, day, segment, meta.param_version, where=" AND s.sector_id = ?", args=[sector_id]
    )
    stage_row = items[0] if items else None
    features = stage_row.get("features") if stage_row else None
    if items:
        _trim_features(items)

    children = conn.execute(
        """
        SELECT s.sector_id, sec.name, s.stage, s.days_in_stage, f.features
        FROM sector_stage_confirmed s JOIN sector sec USING (sector_id)
        LEFT JOIN feature_daily f ON f.subject_type = 'sector' AND f.subject_id = s.sector_id
             AND f.trade_date = s.trade_date AND f.segment = 'close' AND f.param_version = s.param_version
        WHERE sec.parent_id = ? AND s.trade_date = ? AND s.param_version = ?
        ORDER BY s.sector_id
        """,
        [sector_id, day, meta.param_version],
    ).pl()
    child_rows = rows(children, ("features",))
    _trim_features(child_rows)

    members = conn.execute(
        f"""
        WITH latest AS (
            SELECT {SNAPSHOT_AS_OF} AS snapshot_date FROM sector_member_snapshot
            WHERE sector_id = ?
        )
        SELECT m.code, s.name, s.is_st, f.net_main, f.main_ratio, f.pct_chg, f.close, f.reconciled,
               b.amount, b.turnover, feat.features
        FROM sector_member_snapshot m
        JOIN latest USING (snapshot_date)
        LEFT JOIN security s ON s.code = m.code
        LEFT JOIN flow_daily f ON f.code = m.code AND f.trade_date = ?
        LEFT JOIN bar_daily b ON b.code = m.code AND b.trade_date = ?
        LEFT JOIN feature_daily feat ON feat.subject_type = 'stock' AND feat.subject_id = m.code
             AND feat.trade_date = ? AND feat.segment = ? AND feat.param_version = ?
        WHERE m.sector_id = ?
        ORDER BY f.net_main DESC NULLS LAST
        """,
        [day, sector_id, day, day, day, segment or "close", meta.param_version, sector_id],
    ).pl()
    member_rows = rows(members, ("features",))
    for m in member_rows:
        feats = m.pop("features", None) or {}
        m["metrics"] = {
            k: feats.get(k)
            for k in (
                "main_mean_5d",
                "inflow_days_5d",
                "ret_5d",
                "ret_20d",
                "is_limit_up",
                "is_one_word",
                "is_consecutive_limit",
                "retention_5d",
                "range_pos_250d",
                "super_share",
                "turnover_vs_20d",
            )
        }

    indices = conn.execute(
        "SELECT index_name, total, components, window_ok, degraded FROM index_daily "
        "WHERE subject_type = 'sector' AND subject_id = ? AND trade_date = ? AND segment = ? "
        "AND param_version = ? ORDER BY index_name",
        [sector_id, day, segment or "close", meta.param_version],
    ).pl()
    hints = conn.execute(
        "SELECT template_id, tier, text, links FROM hint WHERE subject_type = 'sector' AND subject_id = ? "
        "AND trade_date = ? AND segment = ? AND param_version = ?",
        [sector_id, day, segment or "close", meta.param_version],
    ).pl()
    roles = conn.execute(
        f"""
        SELECT r.code, s.name, r.role, r.tags, r.score FROM {"stock_role_confirmed" if segment in (None, "close") else "stock_role_intraday"} r
        LEFT JOIN security s USING (code)
        WHERE r.sector_id = ? AND r.trade_date = ? AND r.param_version = ?{"" if segment in (None, "close") else " AND r.segment = ?"}
        ORDER BY CASE r.role WHEN 'core' THEN 0 WHEN 'follow' THEN 1 WHEN 'avoid' THEN 2 ELSE 3 END, r.score DESC NULLS LAST
        """,
        [sector_id, day, meta.param_version, *([] if segment in (None, "close") else [segment])],
    ).pl()
    return Envelope(
        meta=meta,
        data={
            "sector": rows(sec)[0],
            "stage": stage_row,
            "features": features,
            "children": child_rows,
            "members": member_rows,
            "indices": rows(indices, ("components",)),
            "hints": rows(hints, ("links",)),
            "roles": rows(roles, ("tags",)),
        },
    )


@router.get("/{sector_id}/history", response_model=Envelope)
def sector_history(
    sector_id: str,
    days: int = Query(default=120, ge=5, le=500),
    end: dt.date | None = None,
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """阶段带 + 资金 / 价格序列（详情页主图）。"""
    day = resolve_trade_date(conn, end)
    meta = make_meta(conn, day)
    if day is None:
        return Envelope(meta=meta, data=[])
    df = conn.execute(
        """
        SELECT s.trade_date, s.stage, s.half, s.candidate, s.days_in_stage, s.is_pulse, s.abnormal_transition,
               s.small_sample, f.features
        FROM sector_stage_confirmed s
        LEFT JOIN feature_daily f ON f.subject_type = 'sector' AND f.subject_id = s.sector_id
             AND f.trade_date = s.trade_date AND f.segment = 'close' AND f.param_version = s.param_version
        WHERE s.sector_id = ? AND s.trade_date <= ?
        QUALIFY row_number() OVER (PARTITION BY s.trade_date ORDER BY s.param_version DESC) = 1
        ORDER BY s.trade_date DESC LIMIT ?
        """,
        [sector_id, day, days],
    ).pl()
    series = rows(df.sort("trade_date"), ("features",))
    for r in series:
        feats = r.pop("features", None) or {}
        for k in (
            "sector_net_main",
            "sector_main_ratio",
            "main_mean_5d",
            "sector_pct_chg",
            "ret_20d",
            "breadth",
            "market_share",
            "turnover_vs_20d",
        ):
            r[k] = feats.get(k)
    return Envelope(meta=meta, data=series)


def features_of(
    conn: duckdb.DuckDBPyConnection,
    subject_type: str,
    subject_id: str,
    day: dt.date,
    version: str | None,
    segment: str | None = None,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT features FROM feature_daily WHERE subject_type = ? AND subject_id = ? AND trade_date = ? AND segment = ? "
        + ("AND param_version = ? " if version else "")
        + "ORDER BY param_version DESC LIMIT 1",
        [subject_type, subject_id, day, segment or "close", *([version] if version else [])],
    ).fetchone()
    if row is None:
        return None
    parsed: dict[str, Any] = json.loads(str(row[0]))
    return parsed
