"""板块聚合与板块派生量（capital-flow-indicators skill「板块结构」一节）。

输入：
- `stock`  个股特征表（compute_stock_features 输出），至少含 code, trade_date, net_*, amount, pct_chg,
           turnover, float_mv, is_one_word, is_limit_up, is_consecutive_limit
- `member` 成分表 (sector_id, code, trade_date)：每日生效成分，由存储层从快照展开
- `market` 市场日表 (trade_date, eqw_ret_5d)：算相对强度
"""

from __future__ import annotations

import polars as pl

from moneytool.compute import quant as q
from moneytool.params import Params

TIER_SMALL_INDEX = 3  # amount_tiers 之后的两档：小、微


def amount_tier(amount_ma_20d: pl.Expr, tiers: tuple[float, float, float, float]) -> pl.Expr:
    """需求 8.6 资金体量：巨量 / 大 / 中 / 小 / 微 → 0..4。"""
    huge, large, mid, small = tiers
    return (
        pl.when(amount_ma_20d >= huge)
        .then(0)
        .when(amount_ma_20d >= large)
        .then(1)
        .when(amount_ma_20d >= mid)
        .then(2)
        .when(amount_ma_20d >= small)
        .then(3)
        .otherwise(4)
        .cast(pl.Int32)
    )


def aggregate_sector_daily(stock: pl.DataFrame, member: pl.DataFrame, p: Params) -> pl.DataFrame:
    """成分股 → 板块日行。剔一字板后求和；breadth、涨停占比、集中度、小体量份额、加权换手。"""
    needed = {"code", "trade_date", "net_main", "amount", "pct_chg", "is_one_word", "is_limit_up"}
    missing = needed - set(stock.columns)
    if missing:
        raise ValueError(f"aggregate_sector_daily 缺列: {sorted(missing)}")
    joined = member.join(stock, on=["code", "trade_date"], how="inner")
    if "amount_ma_20d" in joined.columns:
        joined = joined.with_columns(
            amount_tier=amount_tier(pl.col("amount_ma_20d"), p.tags.amount_tiers)
        )
    else:
        joined = joined.with_columns(amount_tier=pl.lit(2, dtype=pl.Int32))
    if "float_mv" not in joined.columns:
        joined = joined.with_columns(float_mv=pl.lit(None, dtype=pl.Float64))
    if "turnover" not in joined.columns:
        joined = joined.with_columns(turnover=pl.lit(None, dtype=pl.Float64))
    if "net_small" not in joined.columns:
        joined = joined.with_columns(net_small=pl.lit(None, dtype=pl.Float64))
    for c in ("net_super", "net_large", "net_medium"):
        if c not in joined.columns:
            joined = joined.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))

    kept = joined.with_columns(
        _flow=pl.when(pl.col("is_one_word")).then(0.0).otherwise(pl.col("net_main")),
        _pos=pl.when(pl.col("is_one_word") | (pl.col("net_main") <= 0))
        .then(0.0)
        .otherwise(pl.col("net_main")),
    )
    kept = kept.with_columns(
        _pos_small_cap=pl.when(pl.col("amount_tier") >= TIER_SMALL_INDEX)
        .then(pl.col("_pos"))
        .otherwise(0.0),
        _rank_pos=pl.col("_pos")
        .rank(method="ordinal", descending=True)
        .over(["sector_id", "trade_date"]),
    ).with_columns(
        _top5=pl.when(pl.col("_rank_pos") <= 5).then(pl.col("_pos")).otherwise(0.0),
    )

    agg = kept.group_by(["sector_id", "trade_date"]).agg(
        sector_net_main=pl.col("_flow").sum(),
        sector_net_super=pl.when(pl.col("is_one_word"))
        .then(0.0)
        .otherwise(pl.col("net_super"))
        .sum(),
        sector_net_large=pl.when(pl.col("is_one_word"))
        .then(0.0)
        .otherwise(pl.col("net_large"))
        .sum(),
        sector_net_medium=pl.when(pl.col("is_one_word"))
        .then(0.0)
        .otherwise(pl.col("net_medium"))
        .sum(),
        sector_net_small=pl.when(pl.col("is_one_word"))
        .then(0.0)
        .otherwise(pl.col("net_small"))
        .sum(),
        sector_amount=pl.col("amount").sum(),
        member_count=pl.col("code").n_unique(),
        up_count=(pl.col("pct_chg") > 0).sum(),
        limit_up_count=pl.col("is_limit_up").sum(),
        consecutive_limit_count=pl.col("is_consecutive_limit").sum()
        if "is_consecutive_limit" in kept.columns
        else pl.lit(0),
        sector_pct_chg=pl.col("pct_chg").mean(),
        _pos_sum=pl.col("_pos").sum(),
        _top5_sum=pl.col("_top5").sum(),
        _pos_small_sum=pl.col("_pos_small_cap").sum(),
        _small_pos_count=(pl.col("net_small") > 0).sum(),
        _wturn=(pl.col("turnover") * pl.col("float_mv")).sum(),
        _wsum=pl.when(pl.col("turnover").is_not_null())
        .then(pl.col("float_mv"))
        .otherwise(None)
        .sum(),
    )
    return agg.with_columns(
        sector_main_ratio=q.safe_div(pl.col("sector_net_main"), pl.col("sector_amount")),
        breadth=q.safe_div(
            pl.col("up_count").cast(pl.Float64), pl.col("member_count").cast(pl.Float64)
        ),
        limit_up_ratio=q.safe_div(
            pl.col("limit_up_count").cast(pl.Float64), pl.col("member_count").cast(pl.Float64)
        ),
        concentration_top5=q.safe_div(pl.col("_top5_sum"), pl.col("_pos_sum")),
        small_cap_share=q.safe_div(pl.col("_pos_small_sum"), pl.col("_pos_sum")),
        small_positive_ratio=q.safe_div(
            pl.col("_small_pos_count").cast(pl.Float64), pl.col("member_count").cast(pl.Float64)
        ),
        sector_turnover=q.safe_div(pl.col("_wturn"), pl.col("_wsum")),
        small_sample=pl.col("member_count") < p.universe.min_members,
    ).drop([c for c in agg.columns if c.startswith("_")])


def compute_sector_features(
    daily: pl.DataFrame, market: pl.DataFrame, p: Params, index_ret: pl.DataFrame | None = None
) -> pl.DataFrame:
    """板块日行 → 时序派生量。`index_ret (sector_id, trade_date, pct_chg)` 给出时覆盖等权收益（申万指数）。"""
    if daily.is_empty():
        raise ValueError("compute_sector_features 输入为空")
    w = p.windows
    r = w.min_valid_ratio
    over = "sector_id"
    df = daily.sort("sector_id", "trade_date")
    if index_ret is not None and not index_ret.is_empty():
        df = (
            df.join(
                index_ret.rename({"pct_chg": "_idx_pct"}),
                on=["sector_id", "trade_date"],
                how="left",
            )
            .with_columns(sector_pct_chg=pl.coalesce(pl.col("_idx_pct"), pl.col("sector_pct_chg")))
            .drop("_idx_pct")
        )

    df = df.with_columns(
        _growth=(1 + pl.col("sector_pct_chg").fill_null(0.0)).cum_prod().over(over)
    )
    df = df.with_columns(
        ret_3d=(pl.col("_growth") / pl.col("_growth").shift(w.short) - 1).over(over),
        ret_5d=(pl.col("_growth") / pl.col("_growth").shift(w.persist) - 1).over(over),
        ret_20d=(pl.col("_growth") / pl.col("_growth").shift(w.cycle) - 1).over(over),
        main_chg_1d=(pl.col("sector_net_main") - pl.col("sector_net_main").shift(1)).over(over),
        main_mean_5d=q.rolling_mean("sector_net_main", w.persist, r).over(over),
        main_mean_prev5=q.rolling_mean("sector_net_main", w.persist, r).shift(1).over(over),
        main_mean_20d=q.rolling_mean("sector_net_main", w.cycle, r).over(over),
        net_main_5d=q.rolling_sum("sector_net_main", w.persist, r).over(over),
        main_slope_3d=q.ols_slope("sector_main_ratio", w.short, r).over(over),
        main_slope_5d=q.ols_slope("sector_main_ratio", w.persist, r).over(over),
        main_ratio_pct_20d=q.rolling_pct_rank("sector_main_ratio", w.cycle, r).over(over),
        pct_chg_pct_20d=q.rolling_pct_rank("sector_pct_chg", w.cycle, r).over(over),
        turnover_pct_20d=q.rolling_pct_rank("sector_turnover", w.cycle, r).over(over),
        turnover_ma_20d=q.rolling_mean("sector_turnover", w.cycle, r).over(over),
        turnover_ma_60d=q.rolling_mean("sector_turnover", w.long, r).over(over),
        amount_ma_20d=q.rolling_mean("sector_amount", w.cycle, r).over(over),
        amount_ma_60d=q.rolling_mean("sector_amount", w.long, r).over(over),
        breadth_chg_1d=(pl.col("breadth") - pl.col("breadth").shift(1)).over(over),
        limit_up_ratio_pct_60d=q.rolling_pct_rank("limit_up_ratio", w.long, r).over(over),
        ret_20d_pct_250d=pl.lit(None, dtype=pl.Float64),
    )
    df = df.with_columns(
        ret_20d_pct_250d=q.rolling_pct_rank("ret_20d", w.year, r).over(over),
        main_dev_5d=pl.when(pl.col("main_mean_5d") > 0)
        .then(pl.col("sector_net_main") / pl.col("main_mean_5d") - 1)
        .otherwise(None),
        main_multiple_prev5=pl.when(pl.col("main_mean_prev5") > 0)
        .then(pl.col("sector_net_main") / pl.col("main_mean_prev5"))
        .otherwise(None),
        main_multiple_20d=pl.when(pl.col("main_mean_20d") > 0)
        .then(pl.col("sector_net_main") / pl.col("main_mean_20d"))
        .otherwise(None),
        turnover_vs_20d=q.safe_div(pl.col("sector_turnover"), pl.col("turnover_ma_20d")),
        turnover_vs_60d=q.safe_div(pl.col("sector_turnover"), pl.col("turnover_ma_60d")),
        amount_ratio_20_60=q.safe_div(pl.col("amount_ma_20d"), pl.col("amount_ma_60d")),
        price_flow_agree=(pl.col("sector_pct_chg").sign() == pl.col("sector_net_main").sign())
        & (pl.col("sector_pct_chg") != 0)
        & (pl.col("sector_net_main") != 0),
        decline_dull=pl.col("ret_3d") - pl.col("ret_5d"),
    )
    df = q.add_streak(df, "sector_net_main", "sector_id", "inflow_streak")

    df = df.join(
        market.select("trade_date", "eqw_ret_5d"), on="trade_date", how="left"
    ).with_columns(sector_rs_5d=pl.col("ret_5d") - pl.col("eqw_ret_5d"))
    return df.drop("_growth")


def add_market_share(sector_feat: pl.DataFrame, sector_meta: pl.DataFrame) -> pl.DataFrame:
    """市场流入份额：板块净流入 / 全部一级正净流入之和（一级、二级、概念分母都用一级）。"""
    df = sector_feat.join(sector_meta.select("sector_id", "level"), on="sector_id", how="left")
    denom = (
        df.filter(pl.col("level") == "L1")
        .group_by("trade_date")
        .agg(_l1_pos_sum=pl.col("sector_net_main").clip(lower_bound=0.0).sum())
    )
    df = df.join(denom, on="trade_date", how="left")
    return df.with_columns(
        market_share=pl.when(pl.col("_l1_pos_sum") > 0)
        .then(pl.col("sector_net_main") / pl.col("_l1_pos_sum"))
        .otherwise(None)
    ).drop("_l1_pos_sum")
