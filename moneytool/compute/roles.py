"""计算链 ⑤：个股角色（需求 8.1 / 8.2）与结构分（8.7）。

依据板块：行业组用申万二级（小样本二级改用所属一级，个股页写明原因），概念组用成分数足够的概念。
板块内排名剔除涨停封板日、连板、新纳入（不满 N 个交易日）、复牌首日。
强势有滞后：昨日为核心 / 跟随的，只有命中失效条件才离开强势，并以「失效」标出。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import polars as pl

from moneytool.compute import quant as q
from moneytool.params import Params
from moneytool.rules.stock import (
    AVOID_LABEL_ZH,
    AVOID_TYPES,
    avoid_rules,
    rule_core,
    rule_invalid,
    rule_strong,
)
from moneytool.storage.repo import to_json
from moneytool.types import Role, SectorLevel, SpreadHalf, Stage

SCORE_TIERS = ((80, "高"), (60, "中上"), (40, "中"), (20, "中下"), (0, "低"))
STRONG_ROLES = (Role.CORE.value, Role.FOLLOW.value)
MICRO_TIER = 4
NORMAL_TIER_MAX = 2


def basis_pairs(
    members: pl.DataFrame,
    sector_meta: pl.DataFrame,
    sector_today: pl.DataFrame,
    p: Params,
) -> pl.DataFrame:
    """(sector_id, code, group, basis_level)：每只股票 1 个行业依据板块 + 若干概念依据板块。"""
    small = sector_today.select("sector_id", "small_sample", "member_count")
    m = members.select("sector_id", "code", "level").join(small, on="sector_id", how="left")
    l2 = m.filter(
        (pl.col("level") == SectorLevel.L2.value) & ~pl.col("small_sample").fill_null(True)
    )
    l2 = l2.unique("code", keep="first").select(
        "sector_id",
        "code",
        pl.lit("industry").alias("group"),
        pl.lit(SectorLevel.L2.value).alias("basis_level"),
    )
    l1 = (
        m.filter(pl.col("level") == SectorLevel.L1.value)
        .join(l2.select("code"), on="code", how="anti")
        .unique("code", keep="first")
        .select(
            "sector_id",
            "code",
            pl.lit("industry").alias("group"),
            pl.lit(SectorLevel.L1.value).alias("basis_level"),
        )
    )
    concept = m.filter(
        (pl.col("level") == SectorLevel.CONCEPT.value)
        & (pl.col("member_count").fill_null(0) >= p.market.concept_min_members)
    ).select(
        "sector_id",
        "code",
        pl.lit("concept").alias("group"),
        pl.lit(SectorLevel.CONCEPT.value).alias("basis_level"),
    )
    allowed = p.role.basis_levels
    frames = [f for f in (l2, l1, concept) if not f.is_empty()]
    if not frames:
        return pl.DataFrame(
            schema={"sector_id": pl.Utf8, "code": pl.Utf8, "group": pl.Utf8, "basis_level": pl.Utf8}
        )
    out = pl.concat(frames)
    # 行业组始终保留（无二级时回退一级）；概念组受参数控制
    return out.filter((pl.col("group") == "industry") | pl.col("basis_level").is_in(list(allowed)))


def sector_context(
    sector_today: pl.DataFrame, stages: pl.DataFrame, sector_meta: pl.DataFrame
) -> pl.DataFrame:
    """板块当日上下文：收益、广度、阶段，及所属一级 / 主导行业的阶段。"""
    st = stages.select("sector_id", "stage", "half", "days_in_stage", "attribution")
    ctx = (
        sector_today.select(
            "sector_id",
            pl.col("ret_5d").alias("sector_ret_5d"),
            pl.col("ret_3d").alias("sector_ret_3d"),
            pl.col("breadth").alias("sector_breadth"),
            "small_sample",
        )
        .join(st, on="sector_id", how="left")
        .join(
            sector_meta.select("sector_id", "level", "parent_id", "dominant_l1"),
            on="sector_id",
            how="left",
        )
        .with_columns(
            upper_id=pl.when(pl.col("level") == SectorLevel.L2.value)
            .then(pl.col("parent_id"))
            .when(pl.col("level") == SectorLevel.CONCEPT.value)
            .then(pl.col("dominant_l1"))
            .otherwise(None)
        )
    )
    upper = st.select(pl.col("sector_id").alias("upper_id"), pl.col("stage").alias("upper_stage"))
    return ctx.join(upper, on="upper_id", how="left")


def role_frame(
    pairs: pl.DataFrame,
    members: pl.DataFrame,
    stock_today: pl.DataFrame,
    ctx: pl.DataFrame,
    eqw_ret_5d: float | None,
    new_member_cutoff: dt.date | None,
    p: Params,
) -> pl.DataFrame:
    """在全部依据板块的成分内排名，再保留依据板块对（pairs）。"""
    sector_first = members.group_by("sector_id").agg(_sector_first=pl.col("first_seen").min())
    basis_ids = pairs.select("sector_id").unique()
    universe = (
        members.select("sector_id", "code", "first_seen")
        .join(basis_ids, on="sector_id", how="semi")
        .join(sector_first, on="sector_id", how="left")
        .join(stock_today, on="code", how="inner")
        .join(ctx, on="sector_id", how="left")
    )
    is_new = (
        (pl.col("first_seen") > pl.col("_sector_first"))
        & (pl.col("first_seen") > new_member_cutoff)
        if new_member_cutoff is not None
        else pl.lit(False)
    )
    df = universe.with_columns(
        is_new_member=is_new.fill_null(False),
        rs_5d_sector=pl.col("ret_5d") - pl.col("sector_ret_5d"),
        rs_3d_sector=pl.col("ret_3d") - pl.col("sector_ret_3d"),
        rs_5d_eqw=pl.col("ret_5d") - pl.lit(eqw_ret_5d, dtype=pl.Float64),
    )
    df = df.with_columns(
        _elig=~(
            pl.col("is_limit_up").fill_null(False)
            | pl.col("is_consecutive_limit").fill_null(False)
            | pl.col("is_new_member")
            | pl.col("is_resumed").fill_null(False)
        )
    )
    by = "sector_id"
    elig_ratio = pl.when(pl.col("_elig")).then(pl.col("main_ratio_5d"))
    elig_rs = pl.when(pl.col("_elig")).then(pl.col("rs_5d_sector"))
    df = df.with_columns(
        _n_ratio=elig_ratio.count().over(by),
        _n_rs=elig_rs.count().over(by),
        _rank_ratio=elig_ratio.rank(method="max", descending=True).over(by),
        _rank_rs=elig_rs.rank(method="max", descending=True).over(by),
        _rs_all_rank=pl.col("rs_5d_sector").rank(method="average").over(by),
        _rs_all_n=pl.col("rs_5d_sector").count().over(by),
        sector_dd=pl.col("max_dd_5d").median().over(by),
    )
    df = df.with_columns(
        ratio_top=q.safe_div(
            pl.col("_rank_ratio").cast(pl.Float64), pl.col("_n_ratio").cast(pl.Float64)
        ),
        rs_top=q.safe_div(pl.col("_rank_rs").cast(pl.Float64), pl.col("_n_rs").cast(pl.Float64)),
        rs_pct_sector=q.safe_div(
            pl.col("_rs_all_rank").cast(pl.Float64), pl.col("_rs_all_n").cast(pl.Float64)
        ),
        dd_vs_sector=pl.col("max_dd_5d") - pl.col("sector_dd"),
        price_strong=(pl.col("ret_5d") > 0) | pl.col("is_new_high_20d").fill_null(False),
        stage_entry=(pl.col("stage") == Stage.START.value)
        | ((pl.col("stage") == Stage.SPREAD.value) & (pl.col("half") == SpreadHalf.FIRST.value)),
        stage_spread_first=(pl.col("stage") == Stage.SPREAD.value)
        & (pl.col("half") == SpreadHalf.FIRST.value),
        stage_exit=pl.col("stage").is_in([Stage.EBB.value, Stage.DIVERGE.value]),
        stage_ebb=pl.col("stage") == Stage.EBB.value,
        stage_climax=pl.col("stage") == Stage.CLIMAX.value,
        stage_start_fresh=(pl.col("stage") == Stage.START.value) & (pl.col("days_in_stage") <= 2),
        parent_ebb=(pl.col("upper_stage") == Stage.EBB.value).fill_null(False),
        outflow_ratio=-pl.col("main_ratio"),
        close_vs_ma5=pl.col("close_adj") - pl.col("ma_5"),
        close_vs_ma10=pl.col("close_adj") - pl.col("ma_10"),
        amount_vs_ma20_prev=q.safe_div(pl.col("amount"), pl.col("amount_ma_20d_prev")),
    )
    df = df.drop([c for c in df.columns if c.startswith("_")])
    return df.join(pairs, on=["sector_id", "code"], how="inner")


def evaluate_roles(
    frame: pl.DataFrame,
    prev_roles: pl.DataFrame | None,
    p: Params,
    *,
    intraday: bool,
) -> pl.DataFrame:
    """逐行跑 8.1 / 8.2 规则。追加 role, is_strong, strong_invalid, avoid_types, tags, role_evidence。"""
    prev: dict[tuple[str, str], str] = {}
    if prev_roles is not None and not prev_roles.is_empty():
        prev = {(r["code"], r["sector_id"]): r["role"] for r in prev_roles.iter_rows(named=True)}
    roles: list[str] = []
    strong_flags: list[bool] = []
    invalid_flags: list[bool] = []
    diverge_flags: list[bool] = []
    avoid_lists: list[str] = []
    tags_out: list[str] = []
    evidence_out: list[str] = []
    for row in frame.iter_rows(named=True):
        tags: list[str] = []
        ev: dict[str, Any] = {}
        was_strong = prev.get((row["code"], row["sector_id"])) in STRONG_ROLES
        consecutive = bool(row.get("is_consecutive_limit"))
        if consecutive:
            tags.append("连板")
        if row.get("is_new_member"):
            tags.append("新纳入")
        if row.get("is_resumed"):
            tags.append("复牌首日")
        if row.get("is_limit_up") and not consecutive:
            tags.append("涨停日不参与排名")
        if row.get("is_pulse"):
            tags.append("脉冲")
        if row.get("price_flow_agree"):
            tags.append("价资同向")

        avoid = avoid_rules(row, p)
        hit_types = [t for t in AVOID_TYPES if avoid[t].hit]
        ev["avoid"] = {t: r.to_dicts() for t, r in avoid.items()}
        strong = rule_strong(row, p)
        ev["strong"] = strong.to_dicts()
        invalid = rule_invalid(row, p)
        is_invalid = False
        if consecutive:
            role = Role.OTHER.value
        elif hit_types:
            role = Role.AVOID.value
            tags.extend(AVOID_LABEL_ZH[t] for t in hit_types)
            is_invalid = was_strong
        elif (was_strong and not invalid.hit) or (not was_strong and strong.hit):
            core = rule_core(row, p)
            ev["core"] = core.to_dicts()
            role = Role.CORE.value if core.hit else Role.FOLLOW.value
        else:
            role = Role.OTHER.value
            is_invalid = was_strong
        if is_invalid:
            ev["invalid"] = invalid.to_dicts()
            tags.append("强势失效")
        if intraday:
            tags.append("盘中")
        roles.append(role)
        strong_flags.append(role in STRONG_ROLES)
        invalid_flags.append(is_invalid)
        diverge_flags.append("diverge_out" in hit_types)
        avoid_lists.append(to_json(hit_types))
        tags_out.append(to_json(tags))
        evidence_out.append(to_json(ev))
    return frame.with_columns(
        role=pl.Series(roles, dtype=pl.Utf8),
        is_strong=pl.Series(strong_flags, dtype=pl.Boolean),
        strong_invalid=pl.Series(invalid_flags, dtype=pl.Boolean),
        avoid_diverge_out=pl.Series(diverge_flags, dtype=pl.Boolean),
        avoid_types=pl.Series(avoid_lists, dtype=pl.Utf8),
        tags=pl.Series(tags_out, dtype=pl.Utf8),
        role_evidence=pl.Series(evidence_out, dtype=pl.Utf8),
    )


def score_tier(score: int | None) -> str | None:
    if score is None:
        return None
    for lo, name in SCORE_TIERS:
        if score >= lo:
            return name
    return SCORE_TIERS[-1][1]


def compute_scores(industry: pl.DataFrame, profiles: pl.DataFrame, p: Params) -> pl.DataFrame:
    """需求 8.7 结构分：按行业依据板块行（每股一行）。返回 code, score, score_tier, score_components。"""
    s = p.score
    clear = p.qualifiers.clear_pp
    df = industry.join(
        profiles.select("code", "amount_tier", "is_st"), on="code", how="left"
    ).with_columns(
        _eqw_pct=pl.col("rs_5d_eqw").rank(method="average") / pl.col("rs_5d_eqw").count(),
        _ret_pct=pl.col("retention_5d").rank(method="average") / pl.col("retention_5d").count(),
    )
    df = df.with_columns(
        c_sector=pl.when(pl.col("rs_5d_sector") <= -clear)
        .then(0)
        .otherwise(q.percentile_score(pl.col("rs_pct_sector"), s.sector_bottom, 1 - s.sector_top)),
        c_eqw=pl.when(pl.col("rs_5d_eqw") <= -clear)
        .then(0)
        .when((pl.col("rs_5d_eqw") > 0) & (pl.col("_eqw_pct") >= 1 - s.eqw_top))
        .then(20)
        .otherwise(q.percentile_score(pl.col("_eqw_pct"), 0.0, 1 - s.eqw_top)),
        c_retention=pl.when(
            (pl.col("retention_5d") > 0) & (pl.col("inflow_streak") >= s.streak_full)
        )
        .then(20)
        .when((pl.col("retention_5d") < 0) & (pl.col("inflow_streak") <= -s.streak_full))
        .then(0)
        .otherwise(q.percentile_score(pl.col("_ret_pct"), 0.0, 1.0)),
        c_agree=pl.when(pl.col("avoid_diverge_out") | pl.col("pulse_giveback").fill_null(False))
        .then(0)
        .when(pl.col("price_flow_agree").fill_null(False) & ~pl.col("is_pulse").fill_null(False))
        .then(20)
        .otherwise(10),
        c_volume=pl.when(
            (pl.col("amount_tier") == MICRO_TIER)
            | (pl.col("main_ratio_pct_20d") < s.normal_lo)
            | (pl.col("main_ratio_pct_20d") > s.normal_hi)
        )
        .then(0)
        .when(pl.col("amount_tier") <= NORMAL_TIER_MAX)
        .then(20)
        .otherwise(10),
        penalty=(
            pl.col("avoid_diverge_out").cast(pl.Int32)
            + pl.col("pulse_giveback").fill_null(False).cast(pl.Int32)
            + pl.col("is_st").fill_null(False).cast(pl.Int32)
        )
        * s.penalty,
    )
    comp_cols = ("c_sector", "c_eqw", "c_retention", "c_agree", "c_volume")
    df = df.with_columns(
        _raw=pl.sum_horizontal([pl.col(c) for c in comp_cols], ignore_nulls=False),
        _has_data=pl.col("net_main").is_not_null() & pl.col("ret_5d").is_not_null(),
    ).with_columns(
        score=pl.when(pl.col("_has_data") & pl.col("_raw").is_not_null())
        .then((pl.col("_raw") - pl.col("penalty")).clip(lower_bound=0))
        .otherwise(None)
        .cast(pl.Int32)
    )
    names = {
        "c_sector": "相对板块",
        "c_eqw": "相对全 A 等权",
        "c_retention": "资金留存",
        "c_agree": "价资一致",
        "c_volume": "体量匹配",
    }
    comps: list[str] = []
    tiers: list[str | None] = []
    for row in df.iter_rows(named=True):
        penalties = [
            label
            for key, label in (
                ("avoid_diverge_out", "背离出货"),
                ("pulse_giveback", "脉冲回吐"),
                ("is_st", "ST"),
            )
            if row.get(key)
        ]
        comps.append(
            to_json(
                {
                    "items": [
                        {"key": c, "name": names[c], "value": row.get(c), "max": 20}
                        for c in comp_cols
                    ],
                    "penalty": row.get("penalty"),
                    "penalty_reasons": penalties,
                }
            )
        )
        tiers.append(score_tier(row.get("score")))
    return df.select("code", "score").with_columns(
        score_tier=pl.Series(tiers, dtype=pl.Utf8), score_components=pl.Series(comps, dtype=pl.Utf8)
    )
