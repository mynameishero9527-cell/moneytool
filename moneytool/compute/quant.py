"""Polars 表达式级统计助手（quant-algorithms skill）。全部按「有行的交易日」计数，调用方先剔停牌。"""

from __future__ import annotations

import polars as pl


def min_samples(window: int, ratio: float) -> int:
    """窗口内有效样本下限：不足 60%（默认）→ null。"""
    return max(1, round(window * ratio))


def rolling_mean(col: str, window: int, ratio: float) -> pl.Expr:
    return pl.col(col).rolling_mean(window_size=window, min_samples=min_samples(window, ratio))


def rolling_sum(col: str, window: int, ratio: float) -> pl.Expr:
    return pl.col(col).rolling_sum(window_size=window, min_samples=min_samples(window, ratio))


def rolling_max(col: str, window: int, ratio: float) -> pl.Expr:
    return pl.col(col).rolling_max(window_size=window, min_samples=min_samples(window, ratio))


def rolling_min(col: str, window: int, ratio: float) -> pl.Expr:
    return pl.col(col).rolling_min(window_size=window, min_samples=min_samples(window, ratio))


def ols_slope(col: str, window: int, ratio: float) -> pl.Expr:
    """最近 N 个值对 0..N-1 做 OLS 的斜率：Σ((i-ī)(y-ȳ)) / Σ(i-ī)² = Σ((i-ī)·y) / Σ(i-ī)²。

    用带权 rolling_sum 一次算完，不写 Python 循环。窗口内有 null 时该处为 null（保守）。
    """
    centered = [i - (window - 1) / 2 for i in range(window)]
    denom = sum(w * w for w in centered)
    # Polars 带权 rolling 不支持 null；窗口短（≤ 5），用 shift 展开。任一值为 null → 结果 null。
    terms = [pl.col(col).shift(window - 1 - i) * w for i, w in enumerate(centered) if w != 0]
    return pl.sum_horizontal(terms, ignore_nulls=False) / denom


def rolling_pct_rank(col: str, window: int, ratio: float, grid: int = 20) -> pl.Expr:
    """当日值在自身近 N 日中的分位（0–1），用 `grid` 个滚动分位数近似：分位 = 超过的网格分位数个数 / grid。

    精确 rolling rank 在 Polars 里没有向量化实现；`grid=20` 时误差 ≤ 0.05，够判「极端 ≥ 0.9」与 0–20 分映射。
    """
    ms = min_samples(window, ratio)
    exceeded = [
        (
            pl.col(col)
            >= pl.col(col).rolling_quantile(
                q, window_size=window, min_samples=ms, interpolation="linear"
            )
        )
        .cast(pl.Int32)
        .fill_null(0)
        for q in (k / grid for k in range(1, grid + 1))
    ]
    total = exceeded[0]
    for e in exceeded[1:]:
        total = total + e
    valid_count = (
        pl.col(col).is_not_null().cast(pl.Int32).rolling_sum(window_size=window, min_samples=1)
    )
    valid = valid_count >= ms
    return (
        pl.when(valid & pl.col(col).is_not_null())
        .then(total.cast(pl.Float64) / grid)
        .otherwise(None)
    )


def add_streak(df: pl.DataFrame, col: str, key: str, out: str) -> pl.DataFrame:
    """按 `key` 分组、按行序计算 `col` 的连续同号天数：正号为正、负号为负，遇 0 或 null 重置。

    调用前需按 (key, trade_date) 排序。
    """
    sign = pl.col(col).sign().fill_null(0).cast(pl.Int32)
    df = df.with_columns(_sign=sign)
    df = df.with_columns(
        _run=(pl.col("_sign") != pl.col("_sign").shift(1).fill_null(0))
        .cast(pl.Int32)
        .cum_sum()
        .over(key)
    )
    df = df.with_columns(_len=pl.int_range(pl.len()).over([key, "_run"]) + 1)
    return df.with_columns(
        pl.when(pl.col("_sign") == 0)
        .then(0)
        .otherwise(pl.col("_len") * pl.col("_sign"))
        .cast(pl.Int32)
        .alias(out)
    ).drop("_sign", "_run", "_len")


def safe_div(num: pl.Expr, den: pl.Expr) -> pl.Expr:
    """分母 0 或 null → null，不算 0。"""
    return pl.when(den.is_null() | (den == 0)).then(None).otherwise(num / den)


def percentile_score(pct: pl.Expr, lo: float, hi: float) -> pl.Expr:
    """分位 → 0–20 分（需求 7.6 分项）：q ≤ lo → 0，q ≥ hi → 20，中间线性取整。"""
    scaled = ((pct - lo) / (hi - lo) * 20).clip(0, 20).round(0)
    return pl.when(pct.is_null()).then(None).otherwise(scaled).cast(pl.Int32)


def max_drawdown(col: str, window: int, ratio: float) -> pl.Expr:
    """窗口内从高点到之后低点的最大回撤（近似：当前值 / 窗口内最高 − 1 的最小值）。"""
    running = pl.col(col) / pl.col(col).rolling_max(window_size=window, min_samples=1) - 1
    return running.rolling_min(window_size=window, min_samples=min_samples(window, ratio))
