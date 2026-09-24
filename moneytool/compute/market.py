"""市场层：全 A 等权（需求 6.1）、广度、涨停统计、主线 / 宽度 / 轮动（7.3.1）、风控开关输入（7.6）。"""

from __future__ import annotations

import datetime as dt
from typing import Any

import polars as pl

from moneytool.compute import quant as q
from moneytool.params import Params
from moneytool.rules.market import decide_gate, evaluate_gate_triggers
from moneytool.types import MarketRegime, Stage

ACTIVE_STAGES = (Stage.START.value, Stage.SPREAD.value)


def eligible_universe(stock: pl.DataFrame, security: pl.DataFrame, p: Params) -> pl.DataFrame:
    """全 A 等权口径：剔上市不满 20 日、ST、退市整理、北交所；停牌日无行天然剔除。

    `security` 列：code, list_date, is_st, is_delisting, exchange。
    """
    df = stock.join(
        security.select("code", "list_date", "is_st", "is_delisting", "exchange"),
        on="code",
        how="left",
    )
    cond = pl.lit(True)
    if p.universe.exclude_st:
        cond = cond & ~pl.col("is_st").fill_null(False)
    if p.universe.exclude_bj:
        cond = cond & (pl.col("exchange") != "BJ")
    cond = cond & ~pl.col("is_delisting").fill_null(False)
    cond = cond & (
        pl.col("list_date").is_null()
        | (
            (pl.col("trade_date") - pl.col("list_date")).dt.total_days()
            >= p.universe.min_listed_days * 7 // 5
        )
    )
    return df.filter(cond).drop("list_date", "is_st", "is_delisting", "exchange")


def compute_market_daily(eligible: pl.DataFrame, p: Params) -> pl.DataFrame:
    """每日市场行：等权收益、广度、涨停、成交额、主力净额，及 5 日累计与 250 日分位。"""
    if eligible.is_empty():
        raise ValueError("compute_market_daily 输入为空")
    w = p.windows
    r = w.min_valid_ratio
    daily = (
        eligible.group_by("trade_date")
        .agg(
            eqw_ret=pl.col("pct_chg").mean(),
            breadth_all=(pl.col("pct_chg") > 0).mean(),
            limit_up_count=pl.col("is_limit_up").sum().cast(pl.Int32),
            consecutive_limit_count=pl.col("is_consecutive_limit").sum().cast(pl.Int32)
            if "is_consecutive_limit" in eligible.columns
            else pl.lit(0, dtype=pl.Int32),
            stock_count=pl.len().cast(pl.Int32),
            amount_all=pl.col("amount").sum(),
            net_main_all=pl.col("net_main").sum(),
            net_small_all=pl.col("net_small").sum()
            if "net_small" in eligible.columns
            else pl.lit(None),
            small_positive_ratio=(pl.col("net_small") > 0).mean()
            if "net_small" in eligible.columns
            else pl.lit(None),
        )
        .sort("trade_date")
    )
    daily = daily.with_columns(
        limit_up_ratio_all=pl.col("limit_up_count") / pl.col("stock_count"),
        _growth=(1 + pl.col("eqw_ret").fill_null(0.0)).cum_prod(),
    )
    daily = daily.with_columns(
        eqw_ret_5d=pl.col("_growth") / pl.col("_growth").shift(w.persist) - 1,
        eqw_ret_20d=pl.col("_growth") / pl.col("_growth").shift(w.cycle) - 1,
        amount_ma_20d=q.rolling_mean("amount_all", w.cycle, r),
    )
    daily = daily.with_columns(
        eqw_ret_5d_pct_250d=q.rolling_pct_rank("eqw_ret_5d", w.year, r),
        amount_vs_20d=q.safe_div(pl.col("amount_all"), pl.col("amount_ma_20d")),
        breadth_dev=(pl.col("breadth_all") - 0.5).abs(),
    )
    daily = q.add_streak(
        daily.with_columns(_k=pl.lit(1)), "net_main_all", "_k", "market_flow_streak"
    ).drop("_k")
    return daily.with_columns(
        market_outflow_streak=pl.when(pl.col("market_flow_streak") < 0)
        .then(-pl.col("market_flow_streak"))
        .otherwise(0)
        .cast(pl.Int32)
    ).drop("_growth")


def mainline_and_regime(
    l1_today: pl.DataFrame, l1_stage_history: pl.DataFrame, p: Params
) -> tuple[list[dict[str, Any]], MarketRegime, float | None]:
    """需求 7.3.1 主线板块与市场宽度三态。

    `l1_today` 列：sector_id, name, stage, market_share, sector_net_main
    `l1_stage_history` 列：sector_id, trade_date, stage（含今日与之前若干日，用于「连续 2 日以上」）
    返回 (主线列表, 三态, 前 3 份额合计)。
    """
    m = p.market
    if l1_today.is_empty():
        return [], MarketRegime.NONE, None
    active = l1_today.filter(pl.col("stage").is_in(ACTIVE_STAGES))
    total_positive = l1_today.filter(pl.col("sector_net_main") > 0)["sector_net_main"].sum()
    if total_positive is None or total_positive <= 0 or active.is_empty():
        return [], MarketRegime.NONE, None

    ranked = l1_today.filter(pl.col("market_share").is_not_null()).sort(
        "market_share", descending=True
    )
    top = ranked.head(m.mainline_top_n)
    top3_share = float(top["market_share"].sum()) if not top.is_empty() else 0.0

    mainline: list[dict[str, Any]] = []
    for row in top.iter_rows(named=True):
        if row["stage"] not in ACTIVE_STAGES:
            continue
        hist = (
            l1_stage_history.filter(pl.col("sector_id") == row["sector_id"])
            .sort("trade_date", descending=True)
            .head(m.mainline_min_days)
        )
        streak_ok = hist.height >= m.mainline_min_days and all(
            s in ACTIVE_STAGES for s in hist["stage"].to_list()
        )
        if streak_ok:
            mainline.append(
                {
                    "sector_id": row["sector_id"],
                    "name": row.get("name"),
                    "stage": row["stage"],
                    "market_share": row["market_share"],
                    "days": m.mainline_min_days,
                }
            )

    if mainline and top3_share >= m.clear_share_min:
        regime = MarketRegime.CLEAR
    elif top3_share < m.scattered_share_max:
        regime = MarketRegime.SCATTERED
    else:
        regime = MarketRegime.SCATTERED if not mainline else MarketRegime.CLEAR
    return mainline, regime, top3_share


def rotation_pairs(
    l1_today: pl.DataFrame, l1_yesterday: pl.DataFrame | None, p: Params
) -> list[dict[str, Any]]:
    """需求 7.3.1 轮动：同日或相邻 2 日一组进入退潮 / 分歧、另一组进入启动，且流出流入同一量级。

    `l1_*` 列：sector_id, name, stage, sector_net_main, entered_today (bool 今日是否迁入该阶段)。
    """
    m = p.market
    out_stages = (Stage.EBB.value, Stage.DIVERGE.value)
    frames = [l1_today] + ([l1_yesterday] if l1_yesterday is not None else [])
    both = (
        pl.concat([f for f in frames if not f.is_empty()], how="vertical_relaxed")
        if frames
        else l1_today
    )
    if both.is_empty() or "entered_today" not in both.columns:
        return []
    leaving = both.filter(
        pl.col("entered_today")
        & pl.col("stage").is_in(out_stages)
        & (pl.col("sector_net_main") < 0)
    )
    entering = both.filter(
        pl.col("entered_today")
        & (pl.col("stage") == Stage.START.value)
        & (pl.col("sector_net_main") > 0)
    )
    if leaving.is_empty() or entering.is_empty():
        return []
    out_total = -float(leaving["sector_net_main"].sum())
    in_total = float(entering["sector_net_main"].sum())
    if out_total <= 0 or in_total <= 0:
        return []
    ratio = in_total / out_total
    if not (m.rotation_ratio_lo <= ratio <= m.rotation_ratio_hi):
        return []
    return [
        {
            "from": leaving.select("sector_id", "name", "sector_net_main").to_dicts(),
            "to": entering.select("sector_id", "name", "sector_net_main").to_dicts(),
            "out_total": out_total,
            "in_total": in_total,
            "ratio": ratio,
        }
    ]


def risk_gate_today(
    market_row: dict[str, Any],
    l1_active_count: int,
    pressure_overheat_streak: int,
    prev_gate: bool,
    prev_clear_streak: int,
    p: Params,
) -> tuple[bool, int, list[str], list[dict[str, Any]]]:
    """需求 7.6 风控开关：组装规则输入行 → 触发规则 → 状态机。返回 (开关, 无触发连续天数, 原因, 证据)。"""
    row = {
        **market_row,
        "l1_active_count": l1_active_count,
        "pressure_overheat_streak": pressure_overheat_streak,
    }
    triggers = evaluate_gate_triggers(row, p)
    gate, clear_streak, reasons = decide_gate(triggers, prev_gate, prev_clear_streak, p)
    evidence = [e.to_dict() for r in triggers.values() for e in r.evidence]
    return gate, clear_streak, reasons, evidence


def market_pressure(daily: pl.DataFrame, l1_top3_share: pl.DataFrame, p: Params) -> pl.DataFrame:
    """需求 7.6 市场情绪压力指数：五分项各 0–20，按近 250 日分位。历史不足 → null。

    `l1_top3_share (trade_date, top3_share)` 来自主线计算。
    """
    w = p.indices.window
    r = p.windows.min_valid_ratio
    lo, hi = p.indices.lo_pct, p.indices.hi_pct
    df = daily.join(l1_top3_share, on="trade_date", how="left").sort("trade_date")
    df = df.with_columns(
        _limit=q.rolling_pct_rank("limit_up_ratio_all", w, r),
        _limit2=q.rolling_pct_rank("consecutive_limit_count", w, r),
        _amt=q.rolling_pct_rank("amount_vs_20d", w, r),
        _breadth=q.rolling_pct_rank("breadth_dev", w, r),
        _retail=q.rolling_pct_rank("small_positive_ratio", w, r),
        _top3=q.rolling_pct_rank("top3_share", w, r),
    )
    df = df.with_columns(
        limit_heat=q.percentile_score((pl.col("_limit") + pl.col("_limit2")) / 2, lo, hi),
        turnover_crowd=q.percentile_score(pl.col("_amt"), lo, hi),
        breadth_extreme=q.percentile_score(pl.col("_breadth"), lo, hi),
        retail_participation=q.percentile_score(pl.col("_retail"), lo, hi),
        mainline_crowd=q.percentile_score(pl.col("_top3"), lo, hi),
    )
    components = [
        "limit_heat",
        "turnover_crowd",
        "breadth_extreme",
        "retail_participation",
        "mainline_crowd",
    ]
    total = pl.sum_horizontal([pl.col(c) for c in components])
    any_null = pl.any_horizontal([pl.col(c).is_null() for c in components])
    df = df.with_columns(
        market_pressure=pl.when(any_null).then(None).otherwise(total).cast(pl.Int32)
    )
    df = df.with_columns(
        _hot=(pl.col("market_pressure") >= p.market.pressure_overheat).cast(pl.Int32).fill_null(0)
    )
    df = q.add_streak(df.with_columns(_k=pl.lit(1)), "_hot", "_k", "pressure_overheat_streak").drop(
        "_k"
    )
    return df.drop([c for c in df.columns if c.startswith("_")])


def pressure_tier(total: int | None) -> str | None:
    if total is None:
        return None
    if total < 20:
        return "冰点"
    if total < 40:
        return "低迷"
    if total < 60:
        return "中性"
    if total < 80:
        return "亢奋"
    return "过热"


def as_date(value: Any) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))
