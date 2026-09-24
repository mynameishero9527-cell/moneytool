"""需求 6.1 个股派生量。公式与列名见 capital-flow-indicators skill。

输入 `df` 列（flow_daily ⋈ bar_daily，每股每日一行，停牌日无行）：
code, trade_date, net_main, net_super, net_large, net_medium, net_small,
open, high, low, close, pre_close, amount, turnover, pct_chg, adj_factor, limit_up, float_mv
"""

from __future__ import annotations

import polars as pl

from moneytool.compute import quant as q
from moneytool.params import Params

REQUIRED_INPUT = (
    "code",
    "trade_date",
    "net_main",
    "net_super",
    "net_large",
    "net_medium",
    "net_small",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "amount",
    "turnover",
    "pct_chg",
    "adj_factor",
    "limit_up",
)


def _check(df: pl.DataFrame) -> None:
    missing = [c for c in REQUIRED_INPUT if c not in df.columns]
    if missing:
        raise ValueError(f"feature 输入缺列: {missing}")


def _role_inputs(df: pl.DataFrame, p: Params) -> pl.DataFrame:
    """需求 8.1–8.11 角色、名单、排除标签与指数用到的派生量。"""
    w = p.windows
    r = w.min_valid_ratio
    over = "code"
    ex = p.exclude
    df = df.with_columns(
        turnover_sum_20d=q.rolling_sum("turnover", w.cycle, r).over(over),
        _amt_close=pl.col("amount") * pl.col("close_adj"),
        _low_turn_big=(
            (pl.col("turnover") < ex.control_low_turnover)
            & (pl.col("pct_chg").abs() >= ex.control_big_move)
        ).cast(pl.Int32),
        _new_low_60=(pl.col("low_adj") <= pl.col("low_60d").shift(1).over(over)).cast(pl.Int32),
        _outflow_day=(pl.col("net_main") < 0).cast(pl.Int32),
        _abs_ratio=pl.col("main_ratio").abs(),
        amount_ma_20d_prev=pl.col("amount_ma_20d").shift(1).over(over),
        main_ratio_5d=q.safe_div(pl.col("net_main_5d"), pl.col("amount_5d")),
        amount_5d_vs_60d=q.safe_div(pl.col("amount_5d") / w.persist, pl.col("amount_ma_60d")),
        amplitude_20_60=q.safe_div(pl.col("amplitude_20d"), pl.col("amplitude_60d")),
        amplitude_5_60=q.safe_div(pl.col("amplitude_5d"), pl.col("amplitude_60d")),
    )
    df = df.with_columns(
        vwap_20d=q.safe_div(
            q.rolling_sum("_amt_close", w.cycle, r), q.rolling_sum("amount", w.cycle, r)
        ).over(over),
        low_turn_big_move_60d=q.rolling_sum("_low_turn_big", w.long, r).over(over),
        new_low_60d_10d=pl.col("_new_low_60").fill_null(0).rolling_sum(10).over(over),
        outflow_days_5d=q.rolling_sum("_outflow_day", w.persist, r).over(over),
        main_ratio_abs_mean_20d=q.rolling_mean("_abs_ratio", w.cycle, r).over(over),
        drawdown_20d_pct_20d=q.rolling_pct_rank("drawdown_20d", w.cycle, r).over(over),
    )
    # 缩量回落日：下跌，且主力净流出占成交额比例不超过自身近 20 日均值（需求 8.8 回踩不破）
    df = df.with_columns(
        vwap_gap_20d=q.safe_div(pl.col("close_adj"), pl.col("vwap_20d")) - 1,
        _soft_pullback=(pl.col("pct_chg") < 0)
        & (-pl.col("main_ratio") <= pl.col("main_ratio_abs_mean_20d"))
        & (pl.col("close_adj") >= pl.col("ma_10")),
    )
    df = df.with_columns(
        pullback_hold=(
            (
                pl.col("_soft_pullback").shift(1).over(over)
                | pl.col("_soft_pullback").shift(2).over(over)
                | pl.col("_soft_pullback").shift(3).over(over)
            )
            & (pl.col("net_main") > 0)
            & (pl.col("close_adj") >= pl.col("ma_10"))
        ).fill_null(False),
    )
    if "cal_idx" in df.columns:
        df = df.with_columns(
            is_resumed=((pl.col("cal_idx") - pl.col("cal_idx").shift(1).over(over)) > 1).fill_null(
                False
            )
        )
    else:
        df = df.with_columns(is_resumed=pl.lit(False))
    return df


def compute_stock_features(df: pl.DataFrame, p: Params) -> pl.DataFrame:
    """对全市场多日数据一次算出全部个股派生量。按 code 分组的窗口用 `over("code")`。"""
    _check(df)
    if df.is_empty():
        raise ValueError("feature 输入为空")
    w = p.windows
    r = w.min_valid_ratio
    df = df.sort("code", "trade_date")

    # 复权价：adj_factor 缺失时视为 1（不复权），只用于区间与位置指标。
    df = df.with_columns(
        adj=pl.col("adj_factor").fill_null(1.0),
    ).with_columns(
        close_adj=pl.col("close") * pl.col("adj"),
        high_adj=pl.col("high") * pl.col("adj"),
        low_adj=pl.col("low") * pl.col("adj"),
        open_adj=pl.col("open") * pl.col("adj"),
        main_ratio=q.safe_div(pl.col("net_main"), pl.col("amount")),
        small_ratio=q.safe_div(pl.col("net_small"), pl.col("amount")),
        is_limit_up=(pl.col("close") >= pl.col("limit_up")) & pl.col("limit_up").is_not_null(),
    )

    over = "code"
    df = df.with_columns(
        main_chg_1d=(pl.col("net_main") - pl.col("net_main").shift(1)).over(over),
        main_mean_5d=q.rolling_mean("net_main", w.persist, r).over(over),
        main_mean_prev5=q.rolling_mean("net_main", w.persist, r).shift(1).over(over),
        main_mean_20d=q.rolling_mean("net_main", w.cycle, r).over(over),
        net_main_5d=q.rolling_sum("net_main", w.persist, r).over(over),
        main_slope_3d=q.ols_slope("main_ratio", w.short, r).over(over),
        main_slope_5d=q.ols_slope("main_ratio", w.persist, r).over(over),
        main_ratio_pct_20d=q.rolling_pct_rank("main_ratio", w.cycle, r).over(over),
        pct_chg_pct_20d=q.rolling_pct_rank("pct_chg", w.cycle, r).over(over),
        turnover_ma_20d=q.rolling_mean("turnover", w.cycle, r).over(over),
        turnover_pct_20d=q.rolling_pct_rank("turnover", w.cycle, r).over(over),
        amount_ma_20d=q.rolling_mean("amount", w.cycle, r).over(over),
        amount_ma_60d=q.rolling_mean("amount", w.long, r).over(over),
        amount_5d=q.rolling_sum("amount", w.persist, r).over(over),
        ret_3d=(pl.col("close_adj") / pl.col("close_adj").shift(w.short) - 1).over(over),
        ret_5d=(pl.col("close_adj") / pl.col("close_adj").shift(w.persist) - 1).over(over),
        ret_20d=(pl.col("close_adj") / pl.col("close_adj").shift(w.cycle) - 1).over(over),
        ma_5=q.rolling_mean("close_adj", 5, r).over(over),
        ma_10=q.rolling_mean("close_adj", 10, r).over(over),
        high_20d=q.rolling_max("high_adj", w.cycle, r).over(over),
        high_250d=q.rolling_max("high_adj", w.year, r).over(over),
        low_20d=q.rolling_min("low_adj", w.cycle, r).over(over),
        low_60d=q.rolling_min("low_adj", w.long, r).over(over),
        low_250d=q.rolling_min("low_adj", w.year, r).over(over),
        max_dd_5d=q.max_drawdown("close_adj", w.persist, r).over(over),
        prev_close_adj=pl.col("close_adj").shift(1).over(over),
        super_share=q.safe_div(pl.col("net_super"), pl.col("net_main")),
    )

    # 依赖上一批列的派生
    df = df.with_columns(
        main_dev_5d=pl.when(pl.col("main_mean_5d") > 0)
        .then(pl.col("net_main") / pl.col("main_mean_5d") - 1)
        .otherwise(None),
        main_multiple_prev5=pl.when(pl.col("main_mean_prev5") > 0)
        .then(pl.col("net_main") / pl.col("main_mean_prev5"))
        .otherwise(None),
        main_multiple_20d=pl.when(pl.col("main_mean_20d") > 0)
        .then(pl.col("net_main") / pl.col("main_mean_20d"))
        .otherwise(None),
        price_flow_agree=(pl.col("pct_chg").sign() == pl.col("net_main").sign())
        & (pl.col("pct_chg") != 0)
        & (pl.col("net_main") != 0),
        turnover_vs_20d=q.safe_div(pl.col("turnover"), pl.col("turnover_ma_20d")),
        amount_ratio_20_60=q.safe_div(pl.col("amount_ma_20d"), pl.col("amount_ma_60d")),
        is_new_high_20d=(pl.col("close_adj") >= pl.col("high_20d").shift(1).over(over)),
        drawdown_250d=q.safe_div(pl.col("close_adj"), pl.col("high_250d")) - 1,
        drawdown_20d=q.safe_div(pl.col("close_adj"), pl.col("high_20d")) - 1,
        range_pos_250d=q.safe_div(
            pl.col("close_adj") - pl.col("low_250d"), pl.col("high_250d") - pl.col("low_250d")
        ),
        is_one_word=(
            pl.col("amount")
            < pl.col("amount_ma_20d").shift(1).over(over) * p.universe.one_word_amount_ratio
        )
        & pl.col("is_limit_up"),
        amplitude=q.safe_div(pl.col("high_adj") - pl.col("low_adj"), pl.col("prev_close_adj")),
        gap_abs=(q.safe_div(pl.col("open_adj"), pl.col("prev_close_adj")) - 1).abs(),
        super_share_mean_5d=q.rolling_mean("super_share", w.persist, r).over(over),
        # 资金留存：涨日流入之和 − 跌日流出之和（跌日净流入不减；涨日净流出不加）
        _inflow_up=pl.when((pl.col("pct_chg") > 0) & (pl.col("net_main") > 0))
        .then(pl.col("net_main"))
        .otherwise(0.0),
        _outflow_down=pl.when((pl.col("pct_chg") < 0) & (pl.col("net_main") < 0))
        .then(-pl.col("net_main"))
        .otherwise(0.0),
        _inflow_day=(pl.col("net_main") > 0).cast(pl.Int32),
        _small_pos_main_neg=((pl.col("net_small") > 0) & (pl.col("net_main") < 0)).cast(pl.Int32),
    )

    df = df.with_columns(
        retention_5d=(
            q.rolling_sum("_inflow_up", w.persist, r) - q.rolling_sum("_outflow_down", w.persist, r)
        ).over(over),
        inflow_days_5d=q.rolling_sum("_inflow_day", w.persist, r).over(over),
        inflow_days_10d=q.rolling_sum("_inflow_day", 10, r).over(over),
        small_pos_main_neg_10d=q.rolling_sum("_small_pos_main_neg", 10, r).over(over),
        amplitude_5d=q.rolling_mean("amplitude", w.persist, r).over(over),
        amplitude_20d=q.rolling_mean("amplitude", w.cycle, r).over(over),
        amplitude_60d=q.rolling_mean("amplitude", w.long, r).over(over),
        gap_abs_5d=q.rolling_mean("gap_abs", w.persist, r).over(over),
        gap_abs_60d=q.rolling_mean("gap_abs", w.long, r).over(over),
        super_share_chg_5d=pl.col("super_share") - pl.col("super_share_mean_5d"),
        # 脉冲：当日 > 显著倍数 × 前 5 日均值，且前 5 日均值 > 0
        is_pulse=(pl.col("main_multiple_prev5") > p.qualifiers.significant_multiple)
        & (pl.col("main_mean_prev5") > 0),
        # 连板：连续 ≥ 2 日涨停
        _limit_int=pl.col("is_limit_up").cast(pl.Int32),
    )
    df = q.add_streak(df, "net_main", "code", "inflow_streak")
    df = q.add_streak(df, "_limit_int", "code", "limit_streak")

    # 脉冲回吐：脉冲日后 2 个交易日净额之和 ≤ −大部分 × 脉冲日净额（在脉冲日后第 2 日成立）
    df = df.with_columns(
        _pulse_net=pl.when(pl.col("is_pulse")).then(pl.col("net_main")).otherwise(None),
    ).with_columns(
        pulse_giveback=(
            pl.col("_pulse_net").shift(2).over(over).is_not_null()
            & (
                (pl.col("net_main") + pl.col("net_main").shift(1).over(over))
                <= -p.qualifiers.majority_ratio * pl.col("_pulse_net").shift(2).over(over)
            )
        ).fill_null(False),
        is_consecutive_limit=pl.col("limit_streak") >= 2,
    )
    df = _role_inputs(df, p)

    return df.drop(
        [c for c in df.columns if c.startswith("_")]
        + ["adj", "prev_close_adj", "super_share_mean_5d"]
        + (["cal_idx"] if "cal_idx" in df.columns else [])
    )
