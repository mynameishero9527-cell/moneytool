"""需求 7.5 板块强势归因：三类倾向各自 0–3 分，只给证据与倾向度，不改阶段、角色与评分。

- 概念炒作：三条均可由现有数据计算。
- 周期性（季节）：需要板块 5 年同月历史，计算链只装载近 320 个交易日，标「历史不足」不计分。
- 外部联动：外部资产映射与行情尚未接入，标「无对应外部资产」不计分（需求：不算 0 分）。
"""

from __future__ import annotations

from typing import Any

import polars as pl

from moneytool.params import Params
from moneytool.rules.base import compare
from moneytool.storage.repo import to_json
from moneytool.types import SectorLevel, Stage

ACTIVE = (Stage.START.value, Stage.SPREAD.value)


def hype_rules(row: dict[str, Any], p: Params) -> list[Any]:
    a = p.attribution
    return [
        compare(
            "attr.hype.concept_only",
            row,
            "concept_without_industry",
            "is_true",
            None,
            "概念板块且主导行业不在启动以上",
        ),
        compare(
            "attr.hype.limit_heat",
            row,
            "limit_up_ratio_pct_60d",
            ">=",
            a.hype_limit_pct,
            "涨停占比 60 日分位",
        ),
        compare(
            "attr.hype.concentration",
            row,
            "concentration_top5",
            ">",
            a.hype_concentration_min,
            "前 5 只资金集中度",
        ),
        compare(
            "attr.hype.turnover",
            row,
            "turnover_vs_60d",
            ">=",
            a.hype_turnover_multiple,
            "加权换手 / 近 60 日",
        ),
        compare(
            "attr.hype.small_cap",
            row,
            "small_cap_share",
            ">=",
            a.hype_small_share_min,
            "小、微体量占净流入",
        ),
    ]


def compute_attribution(sector_today: pl.DataFrame, ctx: pl.DataFrame, p: Params) -> pl.DataFrame:
    """返回 sector_id, hype_score, seasonal_score, external_score, attribution (JSON)。"""
    df = sector_today.join(
        ctx.select("sector_id", "level", "upper_stage"), on="sector_id", how="left"
    ).with_columns(
        concept_without_industry=(pl.col("level") == SectorLevel.CONCEPT.value)
        & ~pl.col("upper_stage").is_in(list(ACTIVE)).fill_null(False)
    )
    ids: list[str] = []
    scores: list[int] = []
    payload: list[str] = []
    for row in df.iter_rows(named=True):
        ev = hype_rules(row, p)
        score = int(ev[0].hit) + int(ev[1].hit and ev[2].hit) + int(ev[3].hit and ev[4].hit)
        summary = "概念炒作倾向" if score >= p.attribution.min_score else "行业自身驱动或暂无法归因"
        ids.append(row["sector_id"])
        scores.append(score)
        payload.append(
            to_json(
                {
                    "hype": {"score": score, "items": [e.to_dict() for e in ev]},
                    "seasonal": {"score": None, "note": "历史不足（需 5 年同月数据）"},
                    "external": {"score": None, "note": "无对应外部资产"},
                    "summary": summary,
                }
            )
        )
    return pl.DataFrame(
        {
            "sector_id": ids,
            "hype_score": scores,
            "seasonal_score": [None] * len(ids),
            "external_score": [None] * len(ids),
            "attribution": payload,
        },
        schema={
            "sector_id": pl.Utf8,
            "hype_score": pl.Int32,
            "seasonal_score": pl.Int32,
            "external_score": pl.Int32,
            "attribution": pl.Utf8,
        },
    )
