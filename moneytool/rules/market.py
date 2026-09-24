"""需求 7.6 市场风控开关规则。输入市场当日特征行。

列：`eqw_ret_5d_pct_250d`（5 日收益在近 250 日分位，越低越深）、`l1_active_count`（启动以上一级数）、
`market_outflow_streak`（全市场连续净流出天数，正数）、`pressure_overheat_streak`（情绪压力 ≥ 过热连续天数）。
"""

from __future__ import annotations

from moneytool.params import Params
from moneytool.rules.base import FeatureRow, RuleResult, all_hit, compare


def rule_gate_eqw_crash(row: FeatureRow, p: Params) -> RuleResult:
    """全 A 等权近 5 日跌幅进入近 250 日最深 5%。"""
    return all_hit(
        compare(
            "market.gate.eqw_drawdown",
            row,
            "eqw_ret_5d_pct_250d",
            "<=",
            p.market.gate.eqw_drawdown_5d_pct,
            "5 日收益 250 日分位",
        )
    )


def rule_gate_no_leader_outflow(row: FeatureRow, p: Params) -> RuleResult:
    """启动以上一级不足 2 个，且全市场连续 3 日主力净流出。"""
    g = p.market.gate
    return all_hit(
        compare(
            "market.gate.few_active_l1",
            row,
            "l1_active_count",
            "<",
            float(g.min_l1_active),
            "启动以上一级数",
        ),
        compare(
            "market.gate.market_outflow",
            row,
            "market_outflow_streak",
            ">=",
            float(g.outflow_days),
            "连续净流出天数",
        ),
    )


def rule_gate_overheat(row: FeatureRow, p: Params) -> RuleResult:
    """市场情绪压力指数连续 2 日 ≥ 80。"""
    return all_hit(
        compare(
            "market.gate.overheat",
            row,
            "pressure_overheat_streak",
            ">=",
            float(p.market.gate.overheat_days),
            "情绪压力过热连续天数",
        )
    )


def evaluate_gate_triggers(row: FeatureRow, p: Params) -> dict[str, RuleResult]:
    return {
        "eqw_crash": rule_gate_eqw_crash(row, p),
        "no_leader_outflow": rule_gate_no_leader_outflow(row, p),
        "overheat": rule_gate_overheat(row, p),
    }


def decide_gate(
    triggers: dict[str, RuleResult], prev_gate: bool, prev_clear_streak: int, p: Params
) -> tuple[bool, int, list[str]]:
    """开关状态机：任一触发即开；全部不成立连续 `release_days` 日才关。

    返回 (今日开关, 今日连续无触发天数, 触发原因)。
    """
    reasons = [name for name, r in triggers.items() if r.hit]
    if reasons:
        return True, 0, reasons
    clear_streak = prev_clear_streak + 1
    if prev_gate and clear_streak < p.market.gate.release_days:
        return True, clear_streak, ["release_pending"]
    return False, clear_streak, []
