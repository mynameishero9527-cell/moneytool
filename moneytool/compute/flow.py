"""资金流整理：分段差分、收盘对账、盘中形态（需求 7.2、11.2；架构 3.1）。"""

from __future__ import annotations

import polars as pl

from moneytool.params import Params
from moneytool.types import INTRADAY_SEGMENTS, Segment

FLOW_COLS = ("net_main", "net_super", "net_large", "net_medium", "net_small")
SEGMENT_ORDER = {s.value: i for i, s in enumerate(INTRADAY_SEGMENTS)}


def segment_diff(snapshots: pl.DataFrame, id_col: str = "subject_id") -> pl.DataFrame:
    """今日累计快照 → 分段净额。本段 = 本次累计 − 上一个成功分段的累计；上一段缺失则跨段并标 spans_missing。

    `snapshots` 列：id_col, trade_date, segment, net_*（竞价段不参与差分，单独展示）。
    """
    df = snapshots.filter(pl.col("segment") != Segment.AUCTION.value)
    if df.is_empty():
        return df.with_columns(spans_missing=pl.lit(False))
    df = df.with_columns(
        _ord=pl.col("segment").replace_strict(SEGMENT_ORDER, default=None).cast(pl.Int32)
    )
    df = df.sort(id_col, "trade_date", "_ord")
    over = [id_col, "trade_date"]
    exprs = [(pl.col(c) - pl.col(c).shift(1).over(over).fill_null(0.0)).alias(c) for c in FLOW_COLS]
    prev_ord = pl.col("_ord").shift(1).over(over)
    spans = (
        pl.when(prev_ord.is_null())
        .then(pl.col("_ord") > 0)
        .otherwise(pl.col("_ord") - prev_ord > 1)
    )
    return df.with_columns(*exprs, spans_missing=spans.fill_null(False)).drop("_ord")


def reconcile(
    segments: pl.DataFrame, daily: pl.DataFrame, id_col: str = "subject_id"
) -> pl.DataFrame:
    """分段之和 vs 日频正式值：偏差率 = |Σ分段 − 日频| / max(|日频|, 1)。返回 (id, trade_date, segment_sum, daily, diff_ratio)。"""
    s = segments.group_by([id_col, "trade_date"]).agg(segment_sum=pl.col("net_main").sum())
    d = daily.select(id_col, "trade_date", pl.col("net_main").alias("daily_net_main"))
    out = s.join(d, on=[id_col, "trade_date"], how="inner")
    return out.with_columns(
        diff_ratio=(pl.col("segment_sum") - pl.col("daily_net_main")).abs()
        / pl.max_horizontal(pl.col("daily_net_main").abs(), pl.lit(1.0))
    )


def intraday_pattern(segments: pl.DataFrame, p: Params, id_col: str = "subject_id") -> pl.DataFrame:
    """需求 7.2 更新形态：早盘加强 / 冲高回吐 / 尾盘异动 / 全天稳步 / 无形态。按 (id, trade_date) 一行。

    只在 5 段齐全时判定；缺段返回 null。
    """
    wide = segments.filter(pl.col("segment").is_in(list(SEGMENT_ORDER))).pivot(
        on="segment", index=[id_col, "trade_date"], values="net_main", aggregate_function="first"
    )
    for seg in SEGMENT_ORDER:
        if seg not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(seg))
    s1, s2, s3, s4, s5 = (pl.col(s.value) for s in INTRADAY_SEGMENTS)
    morning = s1 + s2
    afternoon = s3 + s4 + s5
    total = morning + afternoon
    complete = pl.all_horizontal([pl.col(s).is_not_null() for s in SEGMENT_ORDER])
    tail_share = pl.when(total.abs() > 0).then(s5 / total).otherwise(None)
    same_sign = pl.all_horizontal([(pl.col(s).sign() == total.sign()) for s in SEGMENT_ORDER])
    max_seg_share = pl.max_horizontal([pl.col(s).abs() for s in SEGMENT_ORDER]) / pl.max_horizontal(
        total.abs(), pl.lit(1.0)
    )
    pattern = (
        pl.when(~complete)
        .then(None)
        .when(
            (morning > 0) & (afternoon < 0) & (-afternoon >= p.qualifiers.majority_ratio * morning)
        )
        .then(pl.lit("冲高回吐"))
        .when((total > 0) & (tail_share > p.qualifiers.tail_share))
        .then(pl.lit("尾盘异动"))
        .when(
            (s1 > 0)
            & (s2 > 0)
            & (afternoon >= 0)
            & (s1 + s2 >= (total * p.qualifiers.majority_ratio))
        )
        .then(pl.lit("早盘加强"))
        .when(same_sign & (max_seg_share < p.qualifiers.majority_ratio))
        .then(pl.lit("全天稳步"))
        .otherwise(pl.lit("无明显形态"))
    )
    return wide.with_columns(pattern=pattern).select(id_col, "trade_date", "pattern")
