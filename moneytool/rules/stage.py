"""需求 7.1 六阶段规则。输入板块当日特征行（列名见 capital-flow-indicators skill 板块结构一节）。

板块特征行额外约定的列：
- `inflow_streak`     连续净流入（正）/ 净流出（负）天数
- `main_slope_3d/5d`  主力净流入占比的 OLS 斜率
- `main_dev_5d`       相对近 5 日均值偏离
- `main_multiple_20d` 当日 net_main / 近 20 日均值（均值 > 0 时）
- `pct_chg_pct_20d`   当日涨跌幅在自身近 20 日分位
- `main_ratio_pct_20d`
- `turnover_vs_20d`   板块换手 / 近 20 日均值
- `breadth`, `breadth_chg_1d`, `concentration_top5`, `price_flow_agree`
- `ret_3d`, `ret_5d`, `net_main_5d`, `sector_rs_5d`, `decline_dull` (= ret_3d − ret_5d)
"""

from __future__ import annotations

from moneytool.params import Params
from moneytool.rules.base import Evidence, FeatureRow, RuleResult, all_hit, any_hit, compare
from moneytool.types import STAGE_PRIORITY, SpreadHalf, Stage


def rule_stage_freeze(row: FeatureRow, p: Params) -> RuleResult:
    """冰点：持续净流出、流出放缓、跌幅钝化、上涨家数占比仍低。"""
    q = p.stage.freeze
    return all_hit(
        compare(
            "stage.freeze.outflow_persist",
            row,
            "inflow_streak",
            "<=",
            -q.outflow_streak_min,
            "连续净流出天数",
        ),
        compare(
            "stage.freeze.outflow_slowing",
            row,
            "main_slope_3d",
            ">",
            0.0,
            "3 日斜率转正 = 流出放缓",
        ),
        compare(
            "stage.freeze.decline_dull", row, "decline_dull", ">=", 0.0, "近 3 日跌幅小于近 5 日"
        ),
        compare("stage.freeze.breadth_low", row, "breadth", "<", q.breadth_max, "上涨家数占比"),
    )


def rule_stage_start(row: FeatureRow, p: Params) -> RuleResult:
    """启动：净流入由负转正或重新高于 5 日均值，连续天数尚短，涨幅不大，内部开始扩散。"""
    q = p.stage.start
    turn = any_hit(
        compare("stage.start.inflow_positive", row, "inflow_streak", ">=", 1.0, "净流入转正"),
        compare("stage.start.above_5d_mean", row, "main_dev_5d", ">", 0.0, "高于近 5 日均值"),
    )
    rest = all_hit(
        compare("stage.start.net_main_positive", row, "sector_net_main", ">", 0.0),
        compare(
            "stage.start.streak_short",
            row,
            "inflow_streak",
            "<=",
            float(q.inflow_days_max),
            "连续天数尚短",
        ),
        compare("stage.start.ret_modest", row, "ret_5d", "<=", q.ret_5d_max, "近 5 日涨幅"),
        compare(
            "stage.start.breadth_spreading", row, "breadth", ">=", q.breadth_min, "上涨家数占比"
        ),
    )
    return RuleResult(turn.hit and rest.hit, turn.evidence + rest.evidence)


def rule_stage_spread(row: FeatureRow, p: Params) -> RuleResult:
    """扩散加速：斜率向上、价资同向、扩散提升、资金不只堆在 1 只票上。"""
    q = p.stage.spread
    return all_hit(
        compare("stage.spread.slope_up", row, "main_slope_5d", ">", q.slope_5d_min, "5 日斜率"),
        compare(
            "stage.spread.price_flow_agree", row, "price_flow_agree", "is_true", None, "价资同向"
        ),
        compare("stage.spread.breadth", row, "breadth", ">=", q.breadth_min, "上涨家数占比"),
        compare(
            "stage.spread.not_concentrated",
            row,
            "concentration_top5",
            "<=",
            q.concentration_max,
            "前 5 只占比",
        ),
    )


def spread_half(row: FeatureRow, p: Params) -> tuple[SpreadHalf, tuple[Evidence, ...]]:
    """扩散加速前后半段：涨幅或净流入占比或换手进入近 20 日极端即后半段。"""
    ex = p.qualifiers.extreme_pct
    ev = (
        compare(
            "stage.spread.half.pct_extreme", row, "pct_chg_pct_20d", ">=", ex, "涨跌幅 20 日分位"
        ),
        compare(
            "stage.spread.half.ratio_extreme",
            row,
            "main_ratio_pct_20d",
            ">=",
            ex,
            "净流入占比 20 日分位",
        ),
        compare(
            "stage.spread.half.turnover_extreme",
            row,
            "turnover_pct_20d",
            ">=",
            ex,
            "换手 20 日分位",
        ),
    )
    return (SpreadHalf.SECOND if any(e.hit for e in ev) else SpreadHalf.FIRST), ev


def rule_stage_climax(row: FeatureRow, p: Params) -> RuleResult:
    """高潮拥挤：单日净流入或涨幅显著高于自身近 20 日，换手抬升，集中度升高或扩散开始下降。"""
    q = p.stage.climax
    extreme = any_hit(
        compare(
            "stage.climax.inflow_extreme",
            row,
            "main_multiple_20d",
            ">=",
            p.qualifiers.significant_multiple,
            "净流入 / 近 20 日均值",
        ),
        compare(
            "stage.climax.pct_extreme",
            row,
            "pct_chg_pct_20d",
            ">=",
            p.qualifiers.extreme_pct,
            "涨幅 20 日分位",
        ),
    )
    turnover = compare(
        "stage.climax.turnover_up",
        row,
        "turnover_vs_20d",
        ">=",
        p.qualifiers.volume_multiple,
        "换手 / 近 20 日均值",
    )
    crowd = any_hit(
        compare(
            "stage.climax.concentrated",
            row,
            "concentration_top5",
            ">=",
            q.concentration_min,
            "前 5 只占比",
        ),
        compare(
            "stage.climax.breadth_drop",
            row,
            "breadth_chg_1d",
            "<=",
            -q.breadth_drop_pp,
            "上涨家数占比较昨日变化",
        ),
    )
    return RuleResult(
        extreme.hit and turnover.hit and crowd.hit, (*extreme.evidence, turnover, *crowd.evidence)
    )


def rule_stage_diverge(row: FeatureRow, p: Params) -> RuleResult:
    """分歧背离：价格还强但主力转净流出；或主力仍流入但板块不涨、扩散掉下来。"""
    q = p.stage.diverge
    price_strong_flow_out = all_hit(
        compare("stage.diverge.price_strong", row, "ret_5d", ">", q.ret_5d_min, "近 5 日涨幅"),
        compare("stage.diverge.flow_out_5d", row, "net_main_5d", "<", 0.0, "近 5 日主力净额"),
        compare("stage.diverge.flow_out_today", row, "sector_net_main", "<", 0.0, "当日主力净额"),
    )
    flow_in_price_flat = all_hit(
        compare("stage.diverge.flow_in", row, "sector_net_main", ">", 0.0, "当日主力净额"),
        compare(
            "stage.diverge.price_flat",
            row,
            "ret_3d",
            "<=",
            p.qualifiers.flat_abs_pct,
            "近 3 日涨幅",
        ),
        compare("stage.diverge.breadth_low", row, "breadth", "<=", q.breadth_max, "上涨家数占比"),
    )
    return RuleResult(
        price_strong_flow_out.hit or flow_in_price_flat.hit,
        price_strong_flow_out.evidence + flow_in_price_flat.evidence,
    )


def rule_stage_ebb(row: FeatureRow, p: Params) -> RuleResult:
    """退潮：连续净流出，相对强度转负。"""
    q = p.stage.ebb
    return all_hit(
        compare(
            "stage.ebb.outflow_streak",
            row,
            "inflow_streak",
            "<=",
            -q.outflow_streak_min,
            "连续净流出天数",
        ),
        compare(
            "stage.ebb.rs_negative", row, "sector_rs_5d", "<", q.rs_5d_max, "相对全 A 等权 5 日"
        ),
    )


STAGE_RULES: dict[Stage, object] = {
    Stage.FREEZE: rule_stage_freeze,
    Stage.START: rule_stage_start,
    Stage.SPREAD: rule_stage_spread,
    Stage.CLIMAX: rule_stage_climax,
    Stage.DIVERGE: rule_stage_diverge,
    Stage.EBB: rule_stage_ebb,
}


def evaluate_all_stages(row: FeatureRow, p: Params) -> dict[Stage, RuleResult]:
    """六个阶段全部评估（需求 7.1：被压下的候选也要进解释区）。"""
    return {
        Stage.FREEZE: rule_stage_freeze(row, p),
        Stage.START: rule_stage_start(row, p),
        Stage.SPREAD: rule_stage_spread(row, p),
        Stage.CLIMAX: rule_stage_climax(row, p),
        Stage.DIVERGE: rule_stage_diverge(row, p),
        Stage.EBB: rule_stage_ebb(row, p),
    }


def pick_candidate(results: dict[Stage, RuleResult]) -> tuple[Stage | None, list[Stage]]:
    """按优先级取第一个命中的阶段；其余命中的为被压下候选。"""
    hits = [s for s in STAGE_PRIORITY if results[s].hit]
    if not hits:
        return None, []
    return hits[0], hits[1:]


def rule_sector_pulse(row: FeatureRow, p: Params) -> RuleResult:
    """需求 7.2 一日脉冲：净流入远高于近 5 日均值（用前 5 日，不含当日）。"""
    return all_hit(
        compare(
            "stage.pulse.multiple",
            row,
            "main_multiple_prev5",
            ">",
            p.qualifiers.significant_multiple,
            "当日 / 前 5 日均值",
        ),
        compare(
            "stage.pulse.prev_mean_positive", row, "main_mean_prev5", ">", 0.0, "前 5 日均值为正"
        ),
    )
