"""只对「当日」一行需要的自身历史分位与峰值统计（需求 7.7 / 8.11）。

滚动分位对全历史逐日算代价高，而指数只展示当日值；这里按主体分组取最近 N 行，
精确计算当日值在自身窗口内的分位。样本不足 `min_valid_ratio` 时为 null。
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from moneytool.compute.quant import min_samples


def today_own_percentiles(
    df: pl.DataFrame,
    id_col: str,
    day: dt.date,
    specs: dict[str, tuple[str, int]],
    min_valid_ratio: float,
) -> pl.DataFrame:
    """`specs = {输出列: (源列, 窗口)}`。返回 (id_col, 输出列...)，只含当日有行的主体。"""
    hist = df.filter(pl.col("trade_date") <= day).sort(id_col, "trade_date")
    aggs: list[pl.Expr] = [pl.col("trade_date").last().alias("_last_date")]
    for out, (src, window) in specs.items():
        vals = pl.col(src).tail(window).drop_nulls()
        last = pl.col(src).last()
        aggs.append(
            pl.when(last.is_null() | (vals.len() < min_samples(window, min_valid_ratio)))
            .then(None)
            .otherwise((vals <= last).mean())
            .alias(out)
        )
    out_df = hist.group_by(id_col).agg(aggs)
    return out_df.filter(pl.col("_last_date") == day).drop("_last_date")


def today_amount_peak(df: pl.DataFrame, day: dt.date, window: int) -> pl.DataFrame:
    """近 `window` 日成交额峰值距今天数与峰值日以来的涨跌（需求 8.11 散户承接·高位放量）。"""
    hist = df.filter(pl.col("trade_date") <= day).sort("code", "trade_date")
    amt = pl.col("amount").tail(window)
    peak_at = amt.fill_null(0.0).arg_max()
    out = hist.group_by("code").agg(
        _last_date=pl.col("trade_date").last(),
        amount_peak_days=(amt.len() - 1 - peak_at).cast(pl.Int32),
        amount_peak_drop=pl.col("close_adj").last() / pl.col("close_adj").tail(window).get(peak_at)
        - 1,
    )
    return out.filter(pl.col("_last_date") == day).drop("_last_date")
