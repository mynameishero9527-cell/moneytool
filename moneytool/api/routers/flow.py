"""资金周期：板块 × 周期（当天 / 5 / 20 / 40 / 60 / 120 / 250 日）、趋势倾向与历史统计、资金动向信号。"""

from __future__ import annotations

import datetime as dt
from typing import Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

from moneytool.api.deps import check_segment, get_ro_conn, make_meta, rows, scalar
from moneytool.api.schemas import Envelope
from moneytool.compute.horizons import HORIZON_HINT, HORIZONS, MARKET_ID, SIGNAL_ZH

router = APIRouter(prefix="/flow")

HORIZON_FIELDS = (
    "net_main",
    "amount",
    "main_ratio",
    "ret",
    "inflow_days_ratio",
    "flow_rank_pct",
    "ret_rank_pct",
)
TREND_FIELDS = (
    "score",
    "direction",
    "strength",
    "short_score",
    "mid_score",
    "long_score",
    "consistency",
    "accel",
)
NOTE = "趋势倾向由已发生的资金与价格统计合成，历史统计样本逐日重叠，仅供参考，不代表后续走势"


def _horizon_list() -> list[dict[str, Any]]:
    return [
        {"key": k, "days": n, "label": label, "hint": HORIZON_HINT[k]} for k, n, label in HORIZONS
    ]


def _resolve(
    conn: duckdb.DuckDBPyConnection, trade_date: dt.date | None, segment: str | None
) -> tuple[dt.date | None, str | None]:
    """缺省取最新一次结果：当日已收盘用收盘值，否则用最新盘中分段。返回 (日期, 分段；收盘为 None)。"""
    check_segment(segment)
    day = trade_date
    if day is None:
        latest = scalar(conn, "SELECT max(trade_date) FROM sector_trend")
        if latest is None:
            return None, None
        day = dt.date.fromisoformat(str(latest))
    if segment is not None:
        return day, None if segment == "close" else segment
    has_close = scalar(
        conn, "SELECT count(*) FROM sector_trend WHERE trade_date = ? AND segment = 'close'", [day]
    )
    if has_close:
        return day, None
    seg = scalar(
        conn,
        "SELECT max(segment) FROM sector_trend WHERE trade_date = ? AND segment <> 'close'",
        [day],
    )
    return day, None if seg is None else str(seg)


def _version(conn: duckdb.DuckDBPyConnection, day: dt.date, segment: str | None) -> str | None:
    v = scalar(
        conn,
        "SELECT max(param_version) FROM sector_trend WHERE trade_date = ? AND segment = ?",
        [day, segment or "close"],
    )
    return None if v is None else str(v)


def _horizon_map(
    conn: duckdb.DuckDBPyConnection,
    day: dt.date,
    segment: str | None,
    version: str,
    ids: list[str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    where = ""
    args: list[Any] = [day, segment or "close", version]
    if ids is not None:
        where = f" AND sector_id IN ({', '.join('?' for _ in ids)})"
        args += ids
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows(
        conn.execute(
            f"SELECT sector_id, horizon, {', '.join(HORIZON_FIELDS)} FROM sector_horizon "
            f"WHERE trade_date = ? AND segment = ? AND param_version = ?{where}",
            args,
        ).pl()
    ):
        out.setdefault(r.pop("sector_id"), {})[r.pop("horizon")] = r
    return out


@router.get("/horizons", response_model=Envelope)
def horizons(
    trade_date: dt.date | None = None,
    segment: str | None = Query(default=None),
    level: str | None = Query(default="L1", pattern="^(L1|L2|concept|all)$"),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """板块 × 周期表：每个板块各周期的主力净流入、主力占比、涨跌与同级分位，附趋势倾向。"""
    day, seg = _resolve(conn, trade_date, segment)
    meta = make_meta(conn, day, seg)
    empty = {"horizons": _horizon_list(), "market": {}, "items": [], "note": NOTE}
    if day is None:
        return Envelope(meta=meta, data=empty)
    version = _version(conn, day, seg)
    if version is None:
        meta.reason = meta.reason or "该日尚无多周期结果（收盘计算后生成）"
        return Envelope(meta=meta, data=empty)
    meta.param_version = version
    where = "" if level in (None, "all") else " AND sec.level = ?"
    args: list[Any] = [day, seg or "close", version] + ([] if not where else [level])
    df = conn.execute(
        f"""
        SELECT t.sector_id, sec.name, sec.level,
               {", ".join(f"t.{c}" for c in TREND_FIELDS)},
               TRY_CAST(json_extract(f.features, '$.small_sample') AS BOOLEAN) AS small_sample,
               TRY_CAST(json_extract(f.features, '$.member_count') AS INTEGER) AS member_count
        FROM sector_trend t
        JOIN sector sec USING (sector_id)
        LEFT JOIN feature_daily f ON f.subject_type = 'sector' AND f.subject_id = t.sector_id
             AND f.trade_date = t.trade_date AND f.segment = t.segment AND f.param_version = t.param_version
        WHERE t.trade_date = ? AND t.segment = ? AND t.param_version = ?{where}
        ORDER BY t.score DESC NULLS LAST
        """,
        args,
    ).pl()
    hmap = _horizon_map(conn, day, seg, version)
    items = rows(df)
    for it in items:
        it["h"] = hmap.get(it["sector_id"], {})
    return Envelope(
        meta=meta,
        data={
            "horizons": _horizon_list(),
            "market": hmap.get(MARKET_ID, {}),
            "items": items,
            "note": NOTE,
        },
    )


@router.get("/signals", response_model=Envelope)
def signals(
    days: int = Query(default=5, ge=1, le=60),
    kind: str | None = Query(default=None),
    sector_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """资金动向流：近 N 个有结果的交易日的信号，同一天同一板块同类信号只留最新一次（收盘优先）。"""
    if kind is not None and kind not in SIGNAL_ZH:
        raise HTTPException(status_code=422, detail=f"未知信号 {kind}")
    latest = scalar(conn, "SELECT max(trade_date) FROM flow_signal")
    day = None if latest is None else dt.date.fromisoformat(str(latest))
    meta = make_meta(conn, day)
    if day is None:
        return Envelope(meta=meta, data={"items": [], "kinds": SIGNAL_ZH})
    where = ""
    args: list[Any] = [days]
    if kind:
        where += " AND s.kind = ?"
        args.append(kind)
    if sector_id:
        where += " AND s.sector_id = ?"
        args.append(sector_id)
    args.append(limit)
    df = conn.execute(
        f"""
        WITH d AS (SELECT DISTINCT trade_date FROM flow_signal ORDER BY trade_date DESC LIMIT ?),
        ranked AS (
            SELECT s.*, row_number() OVER (
                PARTITION BY s.trade_date, s.sector_id, s.kind
                ORDER BY s.segment = 'close' DESC, s.segment DESC, s.param_version DESC
            ) AS rn
            FROM flow_signal s JOIN d USING (trade_date)
            WHERE TRUE{where}
        )
        SELECT r.trade_date, r.segment, r.sector_id, sec.name, sec.level, r.kind, r.score, r.text,
               r.payload, r.created_at
        FROM ranked r LEFT JOIN sector sec USING (sector_id)
        WHERE r.rn = 1
        ORDER BY r.trade_date DESC, r.created_at DESC, abs(r.score) DESC
        LIMIT ?
        """,
        args,
    ).pl()
    items = rows(df, ("payload",))
    for it in items:
        it["kind_zh"] = SIGNAL_ZH.get(it["kind"], it["kind"])
    return Envelope(meta=meta, data={"items": items, "kinds": SIGNAL_ZH})


@router.get("/sectors/{sector_id}", response_model=Envelope)
def sector_detail(
    sector_id: str,
    trade_date: dt.date | None = None,
    segment: str | None = Query(default=None),
    days: int = Query(default=120, ge=10, le=500),
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """单板块多周期面板：各周期指标、趋势倾向与依据、同级同方向历史统计、倾向得分走势、近期信号。"""
    day, seg = _resolve(conn, trade_date, segment)
    meta = make_meta(conn, day, seg)
    base: dict[str, Any] = {
        "horizons": _horizon_list(),
        "h": {},
        "market": {},
        "trend": None,
        "history": [],
        "signals": [],
        "note": NOTE,
    }
    if day is None:
        return Envelope(meta=meta, data=base)
    version = _version(conn, day, seg)
    if version is None:
        return Envelope(meta=meta, data=base)
    meta.param_version = version
    hmap = _horizon_map(conn, day, seg, version, [sector_id, MARKET_ID])
    trend = rows(
        conn.execute(
            f"SELECT {', '.join(TREND_FIELDS)}, reasons, backtest FROM sector_trend "
            "WHERE sector_id = ? AND trade_date = ? AND segment = ? AND param_version = ?",
            [sector_id, day, seg or "close", version],
        ).pl(),
        ("reasons", "backtest"),
    )
    history = rows(
        conn.execute(
            """
            SELECT trade_date, score, short_score, mid_score, long_score, direction FROM sector_trend
            WHERE sector_id = ? AND segment = 'close' AND param_version = ? AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT ?
            """,
            [sector_id, version, day, days],
        ).pl()
    )
    history.reverse()
    sig = rows(
        conn.execute(
            """
            SELECT trade_date, segment, kind, score, text FROM flow_signal
            WHERE sector_id = ? AND param_version = ? AND trade_date <= ?
            QUALIFY row_number() OVER (PARTITION BY trade_date, kind ORDER BY segment = 'close' DESC, segment DESC) = 1
            ORDER BY trade_date DESC, created_at DESC LIMIT 20
            """,
            [sector_id, version, day],
        ).pl()
    )
    for s in sig:
        s["kind_zh"] = SIGNAL_ZH.get(s["kind"], s["kind"])
    base.update(
        h=hmap.get(sector_id, {}),
        market=hmap.get(MARKET_ID, {}),
        trend=trend[0] if trend else None,
        history=history,
        signals=sig,
    )
    return Envelope(meta=meta, data=base)


@router.get("/backtest", response_model=Envelope)
def backtest(
    trade_date: dt.date | None = None,
    conn: duckdb.DuckDBPyConnection = Depends(get_ro_conn),
) -> Envelope:
    """趋势倾向历史统计：各级别、各方向在其后 5 / 20 日的上涨占比、平均涨跌与相对同级超额。"""
    day = trade_date
    if day is None:
        latest = scalar(conn, "SELECT max(trade_date) FROM trend_backtest")
        day = None if latest is None else dt.date.fromisoformat(str(latest))
    meta = make_meta(conn, day)
    if day is None:
        return Envelope(meta=meta, data={"items": [], "note": NOTE})
    df = conn.execute(
        """
        SELECT * FROM trend_backtest WHERE trade_date = ?
          AND param_version = (SELECT max(param_version) FROM trend_backtest WHERE trade_date = ?)
        ORDER BY level, fwd_days, direction
        """,
        [day, day],
    ).pl()
    return Envelope(meta=meta, data={"items": rows(df), "note": NOTE})
