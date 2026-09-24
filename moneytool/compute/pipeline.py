"""计算链编排（架构 4.1）：① 资金流整理 → ② 指标 → ③ 市场风控 → ④ 板块阶段 → ⑧ 存档。

本版实现到阶段与市场层；角色 / 名单 / 指数（⑤–⑦）由后续步骤接入同一入口。
入口：`run_confirmed(conn, params, trade_date)` 与 `run_intraday(conn, params, trade_date, segment)`。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

import duckdb
import polars as pl

from moneytool.compute.features import compute_stock_features
from moneytool.compute.market import (
    ACTIVE_STAGES,
    compute_market_daily,
    eligible_universe,
    mainline_and_regime,
    market_pressure,
    risk_gate_today,
    rotation_pairs,
)
from moneytool.compute.sector import (
    add_market_share,
    aggregate_sector_daily,
    compute_sector_features,
)
from moneytool.compute.stages import compute_stages
from moneytool.ingest.reference import members_as_of
from moneytool.logging import get_logger
from moneytool.params import Params
from moneytool.storage.repo import to_json, upsert
from moneytool.types import DataStatus, SectorLevel

log = get_logger(__name__)

HISTORY_DAYS = 320  # 覆盖 250 日窗口 + 缓冲


@dataclass
class PipelineResult:
    trade_date: dt.date
    segment: str | None
    data_status: DataStatus
    stock_rows: int = 0
    sector_rows: int = 0
    stage_rows: int = 0
    notes: list[str] = field(default_factory=list)


# ---------- 输入装载 ----------


def load_stock_inputs(
    conn: duckdb.DuckDBPyConnection, end: dt.date, days: int = HISTORY_DAYS
) -> pl.DataFrame:
    """flow_daily ⋈ bar_daily 近 N 个交易日。停牌日（bar 缺或 is_suspended）不出行。"""
    return conn.execute(
        """
        WITH cal AS (
            SELECT trade_date FROM trade_calendar WHERE is_open AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT ?
        )
        SELECT b.code, b.trade_date,
               f.net_main, f.net_super, f.net_large, f.net_medium, f.net_small,
               b.open, b.high, b.low, b.close, b.pre_close, b.amount, b.turnover, b.pct_chg,
               b.adj_factor, b.limit_up, b.float_mv
        FROM bar_daily b
        JOIN cal USING (trade_date)
        LEFT JOIN flow_daily f USING (code, trade_date)
        WHERE NOT b.is_suspended
        """,
        [end, days],
    ).pl()


def load_intraday_today(
    conn: duckdb.DuckDBPyConnection, day: dt.date, segment: str
) -> pl.DataFrame:
    """盘中：截至本段的分段累计 + 最新快照的价与涨跌幅；成交额由东财净占比反推。"""
    return conn.execute(
        """
        WITH seg AS (
            SELECT code, sum(net_main) net_main, sum(net_super) net_super, sum(net_large) net_large,
                   sum(net_medium) net_medium, sum(net_small) net_small
            FROM flow_intraday WHERE trade_date = ? GROUP BY code
        ),
        snap AS (
            SELECT subject_id AS code, close, pct_chg, net_main AS cum_main
            FROM flow_snapshot WHERE trade_date = ? AND segment = ? AND subject_type = 'stock'
        ),
        prev AS (
            SELECT code, close AS pre_close, adj_factor, limit_up, float_mv
            FROM bar_daily WHERE trade_date = (SELECT max(trade_date) FROM bar_daily WHERE trade_date < ?)
        )
        SELECT s.code, CAST(? AS DATE) AS trade_date,
               seg.net_main, seg.net_super, seg.net_large, seg.net_medium, seg.net_small,
               NULL::DOUBLE AS open, NULL::DOUBLE AS high, NULL::DOUBLE AS low, snap.close,
               prev.pre_close, NULL::DOUBLE AS amount, NULL::DOUBLE AS turnover, snap.pct_chg,
               prev.adj_factor, prev.pre_close * (prev.limit_up / NULLIF(prev.pre_close, 0)) AS limit_up, prev.float_mv
        FROM snap s
        JOIN seg USING (code)
        JOIN snap USING (code)
        LEFT JOIN prev USING (code)
        """,
        [day, day, segment, day, day],
    ).pl()


def load_security(conn: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return conn.execute(
        "SELECT code, name, exchange, board, list_date, is_st, is_delisting FROM security"
    ).pl()


def load_sector_meta(conn: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return conn.execute("SELECT sector_id, name, level, parent_id, dominant_l1 FROM sector").pl()


def load_prev_stage(
    conn: duckdb.DuckDBPyConnection, day: dt.date, version: str
) -> pl.DataFrame | None:
    row = conn.execute(
        "SELECT max(trade_date) FROM sector_stage_confirmed WHERE trade_date < ? AND param_version = ?",
        [day, version],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return conn.execute(
        "SELECT sector_id, stage, candidate, days_in_stage, entered_from FROM sector_stage_confirmed "
        "WHERE trade_date = ? AND param_version = ?",
        [row[0], version],
    ).pl()


def load_sw_index_returns(
    conn: duckdb.DuckDBPyConnection, end: dt.date, days: int
) -> pl.DataFrame | None:
    """申万指数日收益（若已同步到 external_asset_daily 以 `sw:` 前缀存放）。"""
    df = conn.execute(
        """
        SELECT asset_id AS sector_id, trade_date, ret AS pct_chg FROM external_asset_daily
        WHERE asset_id LIKE 'sw:%' AND trade_date <= ? ORDER BY trade_date DESC LIMIT ?
        """,
        [end, days * 200],
    ).pl()
    return None if df.is_empty() else df


def load_prev_market(
    conn: duckdb.DuckDBPyConnection, day: dt.date, version: str
) -> dict[str, Any] | None:
    df = conn.execute(
        "SELECT * FROM market_daily WHERE trade_date < ? AND segment = 'close' AND param_version = ? "
        "ORDER BY trade_date DESC LIMIT 1",
        [day, version],
    ).pl()
    return None if df.is_empty() else df.row(0, named=True)


# ---------- 主流程 ----------


def _prev_clear_streak(prev_market: dict[str, Any] | None) -> int:
    if not prev_market or not prev_market.get("evidence"):
        return 0
    try:
        return int(json.loads(prev_market["evidence"]).get("clear_streak", 0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0


def _features_json(
    df: pl.DataFrame, id_col: str, subject_type: str, day: dt.date, version: str
) -> pl.DataFrame:
    today = df.filter(pl.col("trade_date") == day)
    if today.is_empty():
        return pl.DataFrame(
            schema={
                "subject_type": pl.Utf8,
                "subject_id": pl.Utf8,
                "trade_date": pl.Date,
                "param_version": pl.Utf8,
                "features": pl.Utf8,
            }
        )
    payload = [
        to_json({k: v for k, v in row.items() if k not in (id_col, "trade_date")})
        for row in today.iter_rows(named=True)
    ]
    return pl.DataFrame(
        {
            "subject_type": [subject_type] * today.height,
            "subject_id": today[id_col].to_list(),
            "trade_date": [day] * today.height,
            "param_version": [version] * today.height,
            "features": payload,
        }
    )


def run(
    conn: duckdb.DuckDBPyConnection,
    p: Params,
    day: dt.date,
    *,
    segment: str | None = None,
) -> PipelineResult:
    """一次完整计算。`segment=None` 为收盘全量（写 confirmed），否则为盘中增量（写 intraday）。"""
    intraday = segment is not None
    result = PipelineResult(day, segment, DataStatus.INTRADAY if intraday else DataStatus.CONFIRMED)

    # ① 输入
    hist = load_stock_inputs(conn, day if not intraday else day - dt.timedelta(days=1))
    if intraday:
        today = load_intraday_today(conn, day, segment or "")
        if today.is_empty():
            result.data_status = DataStatus.MISSING
            result.notes.append("盘中无快照")
            return result
        # 东财净占比反推成交额；缺则昨日成交额近似（只影响盘中比例，收盘会被正式值覆盖）
        hist = pl.concat([hist, today.select(hist.columns)], how="vertical_relaxed")
    if hist.is_empty():
        result.data_status = DataStatus.MISSING
        result.notes.append("无日线 / 资金流历史")
        return result
    today_flow_missing = (
        hist.filter(pl.col("trade_date") == day)["net_main"].null_count()
        == hist.filter(pl.col("trade_date") == day).height
    )
    if today_flow_missing:
        result.data_status = DataStatus.DEGRADED
        result.notes.append("当日资金流全部缺失，价格模式")

    # ② 指标
    stock_feat = compute_stock_features(hist, p)
    result.stock_rows = stock_feat.filter(pl.col("trade_date") == day).height

    security = load_security(conn)
    eligible = eligible_universe(stock_feat, security, p)
    market = compute_market_daily(eligible, p)

    members = members_as_of(conn, day)
    if members.is_empty():
        result.notes.append("无成分快照，跳过板块")
        return _persist_market_only(conn, p, day, segment, market, result)
    # 成分只有当日生效版本；历史日按同一成分展开（架构 4.5：首次运行前的日期成分为近似）
    member_days = members.drop("trade_date").join(
        stock_feat.select("trade_date").unique(), how="cross"
    )
    sector_daily = aggregate_sector_daily(
        stock_feat, member_days.select("sector_id", "code", "trade_date"), p
    )
    sw_ret = load_sw_index_returns(conn, day, HISTORY_DAYS)
    sector_feat = compute_sector_features(sector_daily, market, p, index_ret=sw_ret)
    sector_meta = load_sector_meta(conn)
    sector_feat = add_market_share(sector_feat, sector_meta)
    result.sector_rows = sector_feat.filter(pl.col("trade_date") == day).height

    # ④ 阶段
    prev = load_prev_stage(conn, day, p.version)
    today_sector = sector_feat.filter(pl.col("trade_date") == day)
    stages = compute_stages(today_sector, prev, p, trade_date=day, intraday=intraday)
    if intraday:
        stages = stages.with_columns(segment=pl.lit(segment))
    result.stage_rows = stages.height

    # ③ 市场风控（需要 L1 阶段数，所以在阶段之后组装；规则本身只读市场行）
    l1_ids = set(sector_meta.filter(pl.col("level") == SectorLevel.L1.value)["sector_id"].to_list())
    l1_today = (
        stages.filter(pl.col("sector_id").is_in(list(l1_ids)))
        .select("sector_id", "stage", "days_in_stage", "entered_from")
        .join(
            today_sector.select("sector_id", "market_share", "sector_net_main"),
            on="sector_id",
            how="left",
        )
        .join(sector_meta.select("sector_id", "name"), on="sector_id", how="left")
        .with_columns(entered_today=pl.col("days_in_stage") == 1)
    )
    l1_history = conn.execute(
        "SELECT sector_id, trade_date, stage FROM sector_stage_confirmed WHERE param_version = ? AND trade_date < ?",
        [p.version, day],
    ).pl()
    l1_history = pl.concat(
        [l1_history, l1_today.select("sector_id", pl.lit(day).alias("trade_date"), "stage")],
        how="vertical_relaxed",
    )
    mainline, regime, top3 = mainline_and_regime(l1_today, l1_history, p)
    rotation = rotation_pairs(l1_today, None, p)

    top3_hist = conn.execute(
        "SELECT trade_date, mainline FROM market_daily WHERE segment = 'close' AND param_version = ? AND trade_date < ?",
        [p.version, day],
    ).pl()
    top3_frame = pl.DataFrame(
        {"trade_date": [day], "top3_share": [top3]},
        schema={"trade_date": pl.Date, "top3_share": pl.Float64},
    )
    if not top3_hist.is_empty():
        # 历史 top3 份额未单列存储时用 null（分位窗口不足会标历史不足）
        top3_frame = pl.concat(
            [
                top3_hist.select("trade_date", pl.lit(None, dtype=pl.Float64).alias("top3_share")),
                top3_frame,
            ]
        )
    pressure = market_pressure(market, top3_frame, p)
    today_market = pressure.filter(pl.col("trade_date") == day)
    market_row: dict[str, Any] = (
        today_market.row(0, named=True) if not today_market.is_empty() else {}
    )
    prev_market = load_prev_market(conn, day, p.version)
    prev_gate = bool(prev_market["risk_gate"]) if prev_market else False
    prev_clear = _prev_clear_streak(prev_market)
    l1_active = int(l1_today.filter(pl.col("stage").is_in(list(ACTIVE_STAGES))).height)
    gate, clear_streak, reasons, gate_evidence = risk_gate_today(
        market_row,
        l1_active,
        int(market_row.get("pressure_overheat_streak") or 0),
        prev_gate,
        prev_clear,
        p,
    )

    # ⑧ 存档
    stage_table = "sector_stage_intraday" if intraday else "sector_stage_confirmed"
    upsert(conn, stage_table, stages)
    upsert(conn, "feature_daily", _features_json(stock_feat, "code", "stock", day, p.version))
    upsert(
        conn, "feature_daily", _features_json(sector_feat, "sector_id", "sector", day, p.version)
    )
    market_out = pl.DataFrame(
        {
            "trade_date": [day],
            "segment": [segment or "close"],
            "param_version": [p.version],
            "eqw_ret": [market_row.get("eqw_ret")],
            "eqw_ret_5d": [market_row.get("eqw_ret_5d")],
            "breadth_all": [market_row.get("breadth_all")],
            "limit_up_count": [market_row.get("limit_up_count")],
            "limit_up_ratio_all": [market_row.get("limit_up_ratio_all")],
            "amount_all": [market_row.get("amount_all")],
            "net_main_all": [market_row.get("net_main_all")],
            "regime": [regime.value],
            "mainline": [to_json(mainline)],
            "rotation": [to_json(rotation)],
            "market_pressure": [market_row.get("market_pressure")],
            "pressure_components": [
                to_json(
                    {
                        k: market_row.get(k)
                        for k in (
                            "limit_heat",
                            "turnover_crowd",
                            "breadth_extreme",
                            "retail_participation",
                            "mainline_crowd",
                        )
                    }
                )
            ],
            "risk_gate": [gate],
            "risk_gate_reasons": [to_json(reasons)],
            "evidence": [
                to_json({"gate": gate_evidence, "clear_streak": clear_streak, "top3_share": top3})
            ],
            "data_status": [result.data_status.value],
        },
        schema_overrides={
            "trade_date": pl.Date,
            "limit_up_count": pl.Int32,
            "market_pressure": pl.Int32,
        },
    )
    upsert(conn, "market_daily", market_out)
    log.info(
        "pipeline_done",
        trade_date=day.isoformat(),
        segment=segment,
        stocks=result.stock_rows,
        sectors=result.sector_rows,
        gate=gate,
        regime=regime.value,
    )
    return result


def _persist_market_only(
    conn: duckdb.DuckDBPyConnection,
    p: Params,
    day: dt.date,
    segment: str | None,
    market: pl.DataFrame,
    result: PipelineResult,
) -> PipelineResult:
    today = market.filter(pl.col("trade_date") == day)
    row: dict[str, Any] = today.row(0, named=True) if not today.is_empty() else {}
    upsert(
        conn,
        "market_daily",
        pl.DataFrame(
            {
                "trade_date": [day],
                "segment": [segment or "close"],
                "param_version": [p.version],
                "eqw_ret": [row.get("eqw_ret")],
                "eqw_ret_5d": [row.get("eqw_ret_5d")],
                "breadth_all": [row.get("breadth_all")],
                "limit_up_count": [row.get("limit_up_count")],
                "amount_all": [row.get("amount_all")],
                "net_main_all": [row.get("net_main_all")],
                "regime": ["none"],
                "risk_gate": [False],
                "data_status": [result.data_status.value],
            },
            schema_overrides={"trade_date": pl.Date, "limit_up_count": pl.Int32},
        ),
    )
    return result
