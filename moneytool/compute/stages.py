"""需求 7.1 阶段编排：六规则全评估 → 优先级 → 防抖 → 迁移路径 → 前后半段 → 输出行。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import polars as pl

from moneytool.params import Params
from moneytool.rules.stage import (
    evaluate_all_stages,
    pick_candidate,
    rule_sector_pulse,
    spread_half,
)
from moneytool.storage.repo import to_json
from moneytool.types import ALLOWED_TRANSITIONS, SpreadHalf, Stage


@dataclass(frozen=True)
class PrevStage:
    """昨日 confirmed 的关键字段。"""

    stage: Stage
    candidate: Stage | None
    days_in_stage: int
    entered_from: Stage | None


def _prev_map(prev: pl.DataFrame | None) -> dict[str, PrevStage]:
    if prev is None or prev.is_empty():
        return {}
    out: dict[str, PrevStage] = {}
    for row in prev.iter_rows(named=True):
        out[row["sector_id"]] = PrevStage(
            stage=Stage(row["stage"]),
            candidate=Stage(row["candidate"]) if row.get("candidate") else None,
            days_in_stage=int(row.get("days_in_stage") or 1),
            entered_from=Stage(row["entered_from"]) if row.get("entered_from") else None,
        )
    return out


def decide_stage(
    candidate: Stage | None, prev: PrevStage | None, p: Params
) -> tuple[Stage, Stage | None, bool, int, Stage | None]:
    """防抖与迁移路径。返回 (最终阶段, 疑似迁移目标, 异常迁移, 停留天数, 从哪个阶段进入)。

    - 无历史：候选即阶段；候选为空取冰点。
    - 候选 == 昨日：停留 +1。
    - 候选为退潮：立即切换。
    - 其他：昨日候选也是同一新阶段 → 切换；否则保持昨日并标疑似。
    """
    if prev is None:
        stage = candidate or Stage.FREEZE
        return stage, None, False, 1, None
    if candidate is None or candidate == prev.stage:
        return prev.stage, None, False, prev.days_in_stage + 1, prev.entered_from
    # debounce_days 当前只支持 2：昨日候选即今日候选则确认（需求 7.1）。
    switch = candidate in (Stage.EBB, prev.candidate)
    if not switch:
        return prev.stage, candidate, False, prev.days_in_stage + 1, prev.entered_from
    abnormal = (prev.stage, candidate) not in ALLOWED_TRANSITIONS
    return candidate, None, abnormal, 1, prev.stage


def compute_stages(
    sector_feat: pl.DataFrame,
    prev_confirmed: pl.DataFrame | None,
    p: Params,
    *,
    trade_date: dt.date,
    intraday: bool = False,
) -> pl.DataFrame:
    """对一日全部板块判阶段。`sector_feat` 为该日的板块特征行（compute_sector_features 输出的当日切片）。

    盘中模式只输出候选作为盘中倾向（不防抖、不改 confirmed），`stage` 列为候选或昨日阶段。
    """
    prev_map = _prev_map(prev_confirmed)
    rows: list[dict[str, Any]] = []
    for row in sector_feat.iter_rows(named=True):
        results = evaluate_all_stages(row, p)
        candidate, suppressed = pick_candidate(results)
        prev = prev_map.get(row["sector_id"])
        if intraday:
            stage = candidate or (prev.stage if prev else Stage.FREEZE)
            suspected, abnormal, days, entered_from = (
                None,
                False,
                (prev.days_in_stage if prev else 1),
                (prev.entered_from if prev else None),
            )
        else:
            stage, suspected, abnormal, days, entered_from = decide_stage(candidate, prev, p)
        half_evidence: tuple[Any, ...] = ()
        half: SpreadHalf | None = None
        if stage == Stage.SPREAD:
            half, half_evidence = spread_half(row, p)
        pulse = rule_sector_pulse(row, p)
        evidence = {s.value: [e.to_dict() for e in r.evidence] for s, r in results.items()}
        evidence["half"] = [e.to_dict() for e in half_evidence]
        evidence["pulse"] = pulse.to_dicts()
        rows.append(
            {
                "sector_id": row["sector_id"],
                "trade_date": trade_date,
                "param_version": p.version,
                "stage": stage.value,
                "half": half.value if half else None,
                "candidate": candidate.value if candidate else None,
                "suspected_to": suspected.value if suspected else None,
                "abnormal_transition": abnormal,
                "carried_over": False,
                "days_in_stage": days,
                "entered_from": entered_from.value if entered_from else None,
                "suppressed": to_json([s.value for s in suppressed]),
                "pattern": None,
                "is_pulse": pulse.hit,
                "small_sample": bool(row.get("small_sample") or False),
                "attribution": None,
                "evidence": to_json(evidence),
            }
        )
    schema = {
        "sector_id": pl.Utf8,
        "trade_date": pl.Date,
        "param_version": pl.Utf8,
        "stage": pl.Utf8,
        "half": pl.Utf8,
        "candidate": pl.Utf8,
        "suspected_to": pl.Utf8,
        "abnormal_transition": pl.Boolean,
        "carried_over": pl.Boolean,
        "days_in_stage": pl.Int32,
        "entered_from": pl.Utf8,
        "suppressed": pl.Utf8,
        "pattern": pl.Utf8,
        "is_pulse": pl.Boolean,
        "small_sample": pl.Boolean,
        "attribution": pl.Utf8,
        "evidence": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema)


def carry_over_stages(prev_confirmed: pl.DataFrame, trade_date: dt.date, p: Params) -> pl.DataFrame:
    """降级（价格模式）：沿用昨日阶段并标 carried_over（架构 4.1.2）。"""
    if prev_confirmed.is_empty():
        return prev_confirmed
    return prev_confirmed.with_columns(
        trade_date=pl.lit(trade_date),
        param_version=pl.lit(p.version),
        carried_over=pl.lit(True),
        candidate=pl.lit(None, dtype=pl.Utf8),
        suspected_to=pl.lit(None, dtype=pl.Utf8),
        days_in_stage=(pl.col("days_in_stage") + 1).cast(pl.Int32),
        is_pulse=pl.lit(False),
    )
