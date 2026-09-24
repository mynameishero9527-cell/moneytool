from __future__ import annotations

import datetime as dt
import json
from typing import Any

import polars as pl
import pytest

from moneytool.compute.stages import PrevStage, compute_stages, decide_stage
from moneytool.params import Params
from moneytool.rules.stage import evaluate_all_stages, pick_candidate
from moneytool.types import Stage

D = dt.date(2026, 3, 2)


def sector_row(**over: Any) -> dict[str, Any]:
    """一个「什么都不命中」的基线板块行，再按需覆盖。"""
    base: dict[str, Any] = {
        "sector_id": "sw:801010",
        "sector_net_main": 1.0,
        "inflow_streak": 0,
        "main_slope_3d": 0.0,
        "main_slope_5d": 0.0,
        "main_dev_5d": 0.0,
        "main_multiple_20d": 1.0,
        "main_multiple_prev5": 1.0,
        "main_mean_prev5": 1.0,
        "pct_chg_pct_20d": 0.5,
        "main_ratio_pct_20d": 0.5,
        "turnover_pct_20d": 0.5,
        "turnover_vs_20d": 1.0,
        "breadth": 0.42,
        "breadth_chg_1d": 0.0,
        "concentration_top5": 0.7,
        "price_flow_agree": False,
        "ret_3d": 0.02,
        "ret_5d": 0.0,
        "net_main_5d": 1.0,
        "sector_rs_5d": 0.0,
        "decline_dull": -0.01,
        "small_sample": False,
    }
    base.update(over)
    return base


def test_start_hits_and_freeze_not(params: Params) -> None:
    row = sector_row(inflow_streak=2, sector_net_main=5.0, ret_5d=0.03, breadth=0.5)
    results = evaluate_all_stages(row, params)
    assert results[Stage.START].hit
    assert not results[Stage.FREEZE].hit
    cand, suppressed = pick_candidate(results)
    assert cand == Stage.START
    assert suppressed == []


def test_priority_ebb_over_start(params: Params) -> None:
    """验收 15：同一天命中多个阶段时按优先级取，且被压下的阶段列在证据里。"""
    # 同时满足启动（流入转正、扩散）与退潮（连续净流出、相对强度负）是矛盾的，用分歧背离 vs 启动构造：
    row = sector_row(
        inflow_streak=1,
        sector_net_main=5.0,
        ret_5d=0.03,
        breadth=0.45,
        # 分歧背离第二种：主力流入但 3 日不涨、扩散低
        ret_3d=0.0,
    )
    results = evaluate_all_stages(row, params)
    assert results[Stage.START].hit
    assert results[Stage.DIVERGE].hit
    cand, suppressed = pick_candidate(results)
    assert cand == Stage.DIVERGE
    assert suppressed == [Stage.START]


def test_debounce_requires_two_days(params: Params) -> None:
    """验收 16：候选与昨日不同，第 1 日保持并标疑似，第 2 日切换。"""
    prev = PrevStage(stage=Stage.FREEZE, candidate=None, days_in_stage=5, entered_from=Stage.EBB)
    stage, suspected, abnormal, days, entered_from = decide_stage(Stage.START, prev, params)
    assert stage == Stage.FREEZE
    assert suspected == Stage.START
    assert days == 6
    prev2 = PrevStage(
        stage=Stage.FREEZE, candidate=Stage.START, days_in_stage=6, entered_from=Stage.EBB
    )
    stage, suspected, abnormal, days, entered_from = decide_stage(Stage.START, prev2, params)
    assert stage == Stage.START
    assert suspected is None
    assert abnormal is False
    assert days == 1
    assert entered_from == Stage.FREEZE


def test_ebb_switches_immediately(params: Params) -> None:
    prev = PrevStage(
        stage=Stage.SPREAD, candidate=Stage.SPREAD, days_in_stage=3, entered_from=Stage.START
    )
    stage, _suspected, abnormal, days, _ = decide_stage(Stage.EBB, prev, params)
    assert stage == Stage.EBB
    assert abnormal is False
    assert days == 1


def test_abnormal_transition_flag(params: Params) -> None:
    """验收 17：冰点直接到高潮拥挤按规则照切，但标异常迁移。"""
    prev = PrevStage(stage=Stage.FREEZE, candidate=Stage.CLIMAX, days_in_stage=2, entered_from=None)
    stage, _, abnormal, _, entered_from = decide_stage(Stage.CLIMAX, prev, params)
    assert stage == Stage.CLIMAX
    assert abnormal is True
    assert entered_from == Stage.FREEZE


def test_compute_stages_outputs_all_evidence(params: Params) -> None:
    feat = pl.DataFrame(
        [sector_row(inflow_streak=2, sector_net_main=5.0, ret_5d=0.03, breadth=0.5)]
    )
    out = compute_stages(feat, None, params, trade_date=D)
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["stage"] == "start"
    assert row["days_in_stage"] == 1
    ev = json.loads(row["evidence"])
    assert set(ev) >= {s.value for s in Stage} | {"half", "pulse"}
    # 未命中的规则也有证据（前端灰显而非隐藏）
    assert any(not e["hit"] for e in ev["ebb"])


def test_spread_half_second_when_extreme(params: Params) -> None:
    feat = pl.DataFrame(
        [
            sector_row(
                main_slope_5d=0.01,
                price_flow_agree=True,
                breadth=0.6,
                concentration_top5=0.3,
                pct_chg_pct_20d=0.95,
                inflow_streak=4,
            )
        ]
    )
    out = compute_stages(feat, None, params, trade_date=D)
    row = out.row(0, named=True)
    assert row["stage"] == "spread"
    assert row["half"] == "second"


@pytest.mark.acceptance
def test_acceptance_15_priority_lists_suppressed(params: Params) -> None:
    row = sector_row(inflow_streak=1, sector_net_main=5.0, ret_5d=0.03, breadth=0.45, ret_3d=0.0)
    out = compute_stages(pl.DataFrame([row]), None, params, trade_date=D)
    r = out.row(0, named=True)
    assert r["stage"] == "diverge"
    assert json.loads(r["suppressed"]) == ["start"]
