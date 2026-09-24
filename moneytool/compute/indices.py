"""需求 7.7 / 8.11 描述性指数：板块情绪、板块风险、个股风险、散户承接压力。

四个指数都只做展示，不进入名单准入（只有 7.6 风控开关影响新增）。每个分项 0–20，总分 0–100。
分位一律按主体自身近 N 日（`extra.today_own_percentiles`），窗口不足时分项为 null、`window_ok=False`。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import polars as pl

from moneytool.compute import quant as q
from moneytool.compute.extra import today_amount_peak, today_own_percentiles
from moneytool.params import Params
from moneytool.storage.repo import to_json
from moneytool.types import SpreadHalf, Stage

RISK_TIERS = ((80, "高"), (60, "中高"), (40, "中"), (20, "中低"), (0, "低"))
RETAIL_TIERS = ((80, "重"), (60, "较重"), (40, "中"), (20, "较轻"), (0, "轻"))
SENTIMENT_TIERS = ((80, "过热"), (60, "亢奋"), (40, "中性"), (20, "低迷"), (0, "冰点"))
MICRO_TIER = 4
FULL = 20

SECTOR_SENTIMENT_ITEMS = {
    "limit_heat": "涨停与连板",
    "turnover_crowd": "换手 / 近 60 日",
    "breadth_extreme": "上涨家数偏离",
    "retail_participation": "小单净流入成分占比",
    "concentration": "资金集中度",
}
SECTOR_RISK_ITEMS = {
    "stage": "阶段风险",
    "position": "位置风险",
    "divergence": "背离风险",
    "crowding": "拥挤风险",
    "attribution": "归因风险",
}
STOCK_RISK_ITEMS = {
    "position": "位置风险",
    "divergence": "背离风险",
    "volatility": "波动风险",
    "liquidity": "流动性风险",
    "structure": "结构风险",
}
RETAIL_ITEMS = {
    "small_absorb": "小单承接",
    "high_volume": "高位放量",
    "holders": "股东户数",
    "turnover": "换手压力",
    "price_position": "价格位置",
}


def tier_of(total: int | None, tiers: tuple[tuple[int, str], ...]) -> str | None:
    if total is None:
        return None
    for lo, name in tiers:
        if total >= lo:
            return name
    return tiers[-1][1]


def _pscore(col: str, p: Params) -> pl.Expr:
    return q.percentile_score(pl.col(col), p.indices.lo_pct, p.indices.hi_pct)


def _total(cols: list[str]) -> pl.Expr:
    return pl.sum_horizontal([pl.col(c) for c in cols], ignore_nulls=False).cast(pl.Int32)


# ---------- 板块 ----------


def sector_sentiment(sector_feat: pl.DataFrame, day: dt.date, p: Params) -> pl.DataFrame:
    """板块情绪（7.7）：结构同市场情绪压力，分项换成板块内口径，分位按板块自身近 250 日。"""
    w = p.indices.window
    df = sector_feat.with_columns(
        breadth_dev=(pl.col("breadth") - 0.5).abs(),
        consecutive_limit_f=pl.col("consecutive_limit_count").cast(pl.Float64),
    )
    pct = today_own_percentiles(
        df,
        "sector_id",
        day,
        {
            "_p_limit": ("limit_up_ratio", w),
            "_p_consec": ("consecutive_limit_f", w),
            "_p_turn": ("turnover_vs_60d", w),
            "_p_breadth": ("breadth_dev", w),
            "_p_retail": ("small_positive_ratio", w),
            "_p_conc": ("concentration_top5", w),
        },
        p.windows.min_valid_ratio,
    )
    out = pct.with_columns(
        _p_limit_heat=pl.mean_horizontal("_p_limit", "_p_consec"),
    ).with_columns(
        limit_heat=_pscore("_p_limit_heat", p),
        turnover_crowd=_pscore("_p_turn", p),
        breadth_extreme=_pscore("_p_breadth", p),
        retail_participation=_pscore("_p_retail", p),
        concentration=_pscore("_p_conc", p),
    )
    items = list(SECTOR_SENTIMENT_ITEMS)
    return out.select("sector_id", *items).with_columns(total=_total(items))


def sector_risk(
    sector_feat: pl.DataFrame,
    day: dt.date,
    stages: pl.DataFrame,
    sentiment: pl.DataFrame,
    attribution: pl.DataFrame,
    p: Params,
) -> pl.DataFrame:
    ix = p.indices
    sr = ix.stage_risk
    hist = sector_feat.sort("sector_id", "trade_date").with_columns(
        sector_amount_5d=q.rolling_sum(
            "sector_amount", p.windows.persist, p.windows.min_valid_ratio
        ).over("sector_id")
    )
    pct = today_own_percentiles(
        hist, "sector_id", day, {"_p_ret20": ("ret_20d", ix.window)}, p.windows.min_valid_ratio
    )
    today = hist.filter(pl.col("trade_date") == day).select(
        "sector_id", "ret_5d", "net_main_5d", "sector_amount_5d"
    )
    stage_score = (
        pl.when(pl.col("stage") == Stage.SPREAD.value)
        .then(
            pl.when(pl.col("half") == SpreadHalf.SECOND.value)
            .then(sr.get("spread_second", 0))
            .otherwise(sr.get("spread_first", 0))
        )
        .otherwise(pl.col("stage").replace_strict(sr, default=0, return_dtype=pl.Int32))
    )
    hype_map = {int(k): int(v) for k, v in ix.hype_risk.items()}
    df = (
        today.join(stages.select("sector_id", "stage", "half"), on="sector_id", how="left")
        .join(pct, on="sector_id", how="left")
        .join(
            sentiment.select("sector_id", pl.col("total").alias("_sentiment")),
            on="sector_id",
            how="left",
        )
        .join(
            attribution.select("sector_id", "hype_score", "seasonal_score", "external_score"),
            on="sector_id",
            how="left",
        )
        .with_columns(
            stage=stage_score.cast(pl.Int32),
            position=_pscore("_p_ret20", p),
            divergence=pl.when((pl.col("ret_5d") > 0) & (pl.col("net_main_5d") < 0))
            .then(
                (
                    q.safe_div(-pl.col("net_main_5d"), pl.col("sector_amount_5d"))
                    / ix.diverge_full_ratio
                    * FULL
                )
                .clip(0, FULL)
                .round(0)
            )
            .when(pl.col("ret_5d").is_null() | pl.col("net_main_5d").is_null())
            .then(None)
            .otherwise(0)
            .cast(pl.Int32),
            crowding=(pl.col("_sentiment") * FULL / 100).round(0).cast(pl.Int32),
            attribution=(
                pl.col("hype_score")
                .fill_null(0)
                .replace_strict(hype_map, default=0, return_dtype=pl.Int32)
                - (pl.col("seasonal_score").fill_null(0) >= p.attribution.min_score).cast(pl.Int32)
                * ix.basis_risk_reduce
                - (pl.col("external_score").fill_null(0) >= p.attribution.min_score).cast(pl.Int32)
                * ix.basis_risk_reduce
            )
            .clip(lower_bound=0)
            .cast(pl.Int32),
        )
    )
    items = list(SECTOR_RISK_ITEMS)
    return df.select("sector_id", *items).with_columns(total=_total(items))


# ---------- 个股 ----------


def stock_risk(
    stock_feat: pl.DataFrame,
    day: dt.date,
    inputs: pl.DataFrame,
    p: Params,
) -> pl.DataFrame:
    """个股风险（8.11）。`inputs`：code, diverge_out, overdraft, amount_tier, control_risk, crash_risk, basis_sector_risk。"""
    ix = p.indices
    pct = today_own_percentiles(
        stock_feat,
        "code",
        day,
        {"_p_ret20": ("ret_20d", ix.window), "_p_amp": ("amplitude_5_60", ix.window)},
        p.windows.min_valid_ratio,
    )
    today = stock_feat.filter(pl.col("trade_date") == day).select(
        "code", "drawdown_20d", "price_flow_agree", "amount_5d_vs_60d"
    )
    ratio = pl.col("amount_5d_vs_60d")
    liquidity = (
        pl.when(ratio.is_null())
        .then(None)
        .when(ratio < ix.liquidity_dry)
        .then(FULL)
        .when(ratio > ix.liquidity_hot)
        .then(10)
        .when(ratio <= 1)
        .then((FULL * (1 - ratio) / (1 - ix.liquidity_dry)).round(0))
        .otherwise((10 * (ratio - 1) / (ix.liquidity_hot - 1)).round(0))
    )
    df = (
        today.join(pct, on="code", how="left")
        .join(inputs, on="code", how="left")
        .with_columns(
            _pos=_pscore("_p_ret20", p),
        )
        .with_columns(
            position=pl.when(
                (pl.col("drawdown_20d") >= 0) & (pl.col("_p_ret20") >= p.qualifiers.extreme_pct)
            )
            .then(FULL)
            .when(pl.col("overdraft").fill_null(False))
            .then(pl.max_horizontal(pl.col("_pos").fill_null(0), pl.lit(ix.overdraft_position_min)))
            .otherwise(pl.col("_pos"))
            .cast(pl.Int32),
            divergence=pl.when(pl.col("diverge_out").fill_null(False))
            .then(FULL)
            .when(pl.col("price_flow_agree").is_null())
            .then(None)
            .when(~pl.col("price_flow_agree"))
            .then(10)
            .otherwise(0)
            .cast(pl.Int32),
            volatility=_pscore("_p_amp", p),
            liquidity=(
                liquidity
                + (pl.col("amount_tier") == MICRO_TIER).fill_null(False).cast(pl.Int32)
                * ix.micro_extra
            )
            .clip(0, FULL)
            .cast(pl.Int32),
            structure=(
                pl.col("control_risk").fill_null(False).cast(pl.Int32) * 10
                + pl.col("crash_risk").fill_null(False).cast(pl.Int32) * 10
                + (pl.col("basis_sector_risk").fill_null(0) >= ix.broken_risk_min).cast(pl.Int32)
                * ix.sector_risk_extra
            )
            .clip(0, FULL)
            .cast(pl.Int32),
        )
    )
    items = list(STOCK_RISK_ITEMS)
    return df.select("code", *items).with_columns(total=_total(items))


def retail_pressure(stock_feat: pl.DataFrame, day: dt.date, p: Params) -> pl.DataFrame:
    """散户承接压力（8.11）。股东户数未接入：该分项为 null，其余四项之和 × 5/4 折算并标降级。"""
    ix = p.indices
    w = p.windows
    pct = today_own_percentiles(
        stock_feat, "code", day, {"_p_turn": ("turnover_sum_20d", ix.window)}, w.min_valid_ratio
    )
    peak = today_amount_peak(stock_feat, day, w.cycle)
    today = stock_feat.filter(pl.col("trade_date") == day).select(
        "code", "small_pos_main_neg_10d", "vwap_gap_20d"
    )
    drop = pl.col("amount_peak_drop")
    gap = pl.col("vwap_gap_20d")
    df = (
        today.join(pct, on="code", how="left")
        .join(peak, on="code", how="left")
        .with_columns(
            small_absorb=(pl.col("small_pos_main_neg_10d") / 10 * FULL)
            .round(0)
            .clip(0, FULL)
            .cast(pl.Int32),
            high_volume=pl.when(drop.is_null())
            .then(None)
            .when(pl.col("amount_peak_days") > ix.retail_peak_days)
            .then(0)
            .otherwise((-drop / ix.retail_peak_drop * FULL).round(0).clip(0, FULL))
            .cast(pl.Int32),
            holders=pl.lit(None, dtype=pl.Int32),
            turnover=_pscore("_p_turn", p),
            price_position=pl.when(gap.is_null())
            .then(None)
            .otherwise((-gap / ix.retail_vwap_gap * FULL).round(0).clip(0, FULL))
            .cast(pl.Int32),
        )
    )
    known = ["small_absorb", "high_volume", "turnover", "price_position"]
    return df.select("code", *RETAIL_ITEMS).with_columns(
        total=(_total(known).cast(pl.Float64) * len(RETAIL_ITEMS) / len(known))
        .round(0)
        .clip(0, 100)
        .cast(pl.Int32)
    )


# ---------- 存档 ----------


def index_rows(
    df: pl.DataFrame,
    *,
    id_col: str,
    subject_type: str,
    index_name: str,
    items: dict[str, str],
    tiers: tuple[tuple[int, str], ...],
    day: dt.date,
    segment: str | None,
    version: str,
    degraded: bool = False,
    notes: dict[str, str] | None = None,
) -> pl.DataFrame:
    """转成 index_daily 行。components 带分项名、分值、满分与说明；null 分项标「数据缺失」。"""
    comps: list[str] = []
    window_ok: list[bool] = []
    notes = notes or {}
    for row in df.iter_rows(named=True):
        entries: list[dict[str, Any]] = []
        ok = True
        for key, name in items.items():
            v = row.get(key)
            if v is None and key not in notes:
                ok = False
            entries.append(
                {
                    "key": key,
                    "name": name,
                    "value": v,
                    "max": FULL,
                    "note": notes.get(key) or ("数据缺失" if v is None else ""),
                }
            )
        comps.append(to_json({"items": entries, "tier": tier_of(row.get("total"), tiers)}))
        window_ok.append(ok)
    n = df.height
    return pl.DataFrame(
        {
            "subject_type": [subject_type] * n,
            "subject_id": df[id_col].to_list(),
            "trade_date": [day] * n,
            "index_name": [index_name] * n,
            "segment": [segment or "close"] * n,
            "param_version": [version] * n,
            "total": df["total"].to_list(),
            "components": comps,
            "window_ok": window_ok,
            "degraded": [degraded] * n,
        },
        schema={
            "subject_type": pl.Utf8,
            "subject_id": pl.Utf8,
            "trade_date": pl.Date,
            "index_name": pl.Utf8,
            "segment": pl.Utf8,
            "param_version": pl.Utf8,
            "total": pl.Int32,
            "components": pl.Utf8,
            "window_ok": pl.Boolean,
            "degraded": pl.Boolean,
        },
    )
