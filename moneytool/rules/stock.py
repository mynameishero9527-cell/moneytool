"""需求 8.1–8.4、8.8、8.9 个股角色与动作规则。每条规则是纯函数：输入一行，输出带证据的结果。

输入行由 `compute/roles.py` 组装（个股当日特征 ⋈ 依据板块当日特征与阶段 ⋈ 板块内排名），关键列：

- 相对板块：`rs_5d_sector` / `rs_3d_sector`（个股 − 板块收益）、`sector_ret_5d`、`sector_breadth`
- 板块内排名（0–1，越小越靠前；涨停日 / 连板 / 新纳入 / 复牌首日不参与，为 null）：
  `ratio_top`（近 5 日净流入占成交额）、`rs_top`（近 5 日相对强度）
- `dd_vs_sector`：个股近 5 日最大回撤 − 板块成分中位数（> 0 表示回撤小于板块）
- 阶段布尔：`stage_entry`（启动 / 扩散前半段）、`stage_exit`（退潮 / 分歧背离）、`stage_climax`、
  `stage_start_fresh`（迁入启动当日或次日）、`parent_ebb`（所属一级或主导行业退潮）
"""

from __future__ import annotations

from moneytool.params import Params
from moneytool.rules.base import Evidence, FeatureRow, RuleResult, all_hit, any_hit, compare

AVOID_TYPES = ("follow_down", "bleed", "stall", "diverge_out", "pulse_giveback")
SELL_TYPES = ("cycle_ebb", "diverge_out", "strength_broken", "climax_distribution")
BUY_POINTS = ("start_confirm", "pullback_hold", "breakout")
SELL_POINTS = ("climax", "diverge", "broken", "ebb")

AVOID_LABEL_ZH = {
    "follow_down": "跟跌不跟涨",
    "bleed": "持续失血",
    "stall": "放量滞涨",
    "diverge_out": "背离出货",
    "pulse_giveback": "脉冲回吐",
}
SELL_LABEL_ZH = {
    "cycle_ebb": "周期退潮",
    "diverge_out": "背离出货",
    "strength_broken": "强势破坏",
    "climax_distribution": "高潮派发",
}
POINT_LABEL_ZH = {
    "start_confirm": "启动确认",
    "pullback_hold": "回踩不破",
    "breakout": "放量突破",
    "low_base": "低位企稳",
    "climax": "阶段性高潮",
    "diverge": "背离",
    "broken": "结构破坏",
    "ebb": "周期退潮",
    "expire": "跟踪期满",
}


# ---------- 8.1 强势 ----------


def rule_strong(row: FeatureRow, p: Params) -> RuleResult:
    r = p.role
    return all_hit(
        compare("stock.strong.rs", row, "rs_5d_sector", ">", 0.0, "近 5 日相对板块强度"),
        compare(
            "stock.strong.ratio", row, "ratio_top", "<=", r.strong_ratio_top, "资金比例板块排名"
        ),
        compare("stock.strong.retention", row, "retention_5d", ">", 0.0, "资金留存"),
        compare(
            "stock.strong.not_one_day",
            row,
            "inflow_days_5d",
            ">=",
            float(r.min_inflow_days_of_5),
            "近 5 日净流入天数",
        ),
    )


def rule_core(row: FeatureRow, p: Params) -> RuleResult:
    top = p.role.core_top
    return all_hit(
        compare("stock.core.rs_top", row, "rs_top", "<=", top, "相对强度板块排名"),
        compare("stock.core.ratio_top", row, "ratio_top", "<=", top, "资金比例板块排名"),
        compare("stock.core.dd", row, "dd_vs_sector", ">", 0.0, "近 5 日最大回撤较板块中位数"),
    )


def rule_invalid(row: FeatureRow, p: Params) -> RuleResult:
    return any_hit(
        compare("stock.invalid.rs_3d", row, "rs_3d_sector", "<", 0.0, "近 3 日相对强度"),
        compare(
            "stock.invalid.ratio",
            row,
            "ratio_top",
            ">",
            p.role.invalid_ratio_top,
            "资金比例板块排名",
        ),
        compare("stock.invalid.retention", row, "retention_5d", "<", 0.0, "资金留存"),
    )


# ---------- 8.2 规避 ----------


def avoid_rules(row: FeatureRow, p: Params) -> dict[str, RuleResult]:
    r = p.role
    return {
        "follow_down": all_hit(
            compare(
                "stock.avoid.follow_down.sector_up",
                row,
                "sector_ret_5d",
                ">",
                0.0,
                "板块近 5 日涨幅",
            ),
            compare("stock.avoid.follow_down.rs", row, "rs_5d_sector", "<", 0.0, "相对板块强度"),
            compare(
                "stock.avoid.follow_down.ratio",
                row,
                "ratio_top",
                ">=",
                1 - r.avoid_bottom,
                "资金比例板块排名（后段）",
            ),
        ),
        "bleed": all_hit(
            compare(
                "stock.avoid.bleed.days",
                row,
                "outflow_days_5d",
                ">=",
                float(r.bleed_outflow_days),
                "近 5 日净流出天数",
            ),
            compare("stock.avoid.bleed.retention", row, "retention_5d", "<", 0.0, "资金留存"),
        ),
        "stall": all_hit(
            compare(
                "stock.avoid.stall.turnover",
                row,
                "turnover_vs_20d",
                ">",
                1.0,
                "换手 / 近 20 日均值",
            ),
            compare(
                "stock.avoid.stall.flat",
                row,
                "ret_5d",
                "<=",
                p.qualifiers.flat_abs_pct,
                "近 5 日涨幅（接近 0 或为负）",
            ),
            compare("stock.avoid.stall.outflow", row, "net_main_5d", "<", 0.0, "近 5 日主力净额"),
        ),
        "diverge_out": all_hit(
            compare(
                "stock.avoid.diverge.price",
                row,
                "price_strong",
                "is_true",
                None,
                "5 日上涨或创 20 日新高",
            ),
            compare("stock.avoid.diverge.outflow", row, "net_main_5d", "<", 0.0, "近 5 日主力净额"),
            compare(
                "stock.avoid.diverge.side",
                row,
                "ratio_top",
                ">=",
                r.diverge_ratio_side,
                "流出占比板块排名（偏高一侧）",
            ),
        ),
        "pulse_giveback": all_hit(
            compare(
                "stock.avoid.pulse_giveback",
                row,
                "pulse_giveback",
                "is_true",
                None,
                "脉冲后 2 日回吐",
            )
        ),
    }


# ---------- 8.3 买入关注 ----------


def buy_conditions(row: FeatureRow, p: Params) -> dict[str, Evidence]:
    """八条准入（风控单列，由调用方决定新增 / 保留）。键为条件名，失效条件文案取自这里。"""
    return {
        "stage": compare(
            "stock.buy.stage", row, "stage_entry", "is_true", None, "依据板块处于启动 / 扩散前半段"
        ),
        "parent": compare(
            "stock.buy.parent", row, "parent_ebb", "is_false", None, "所属一级 / 主导行业不处于退潮"
        ),
        "role": compare(
            "stock.buy.role", row, "role_ok", "is_true", None, "核心，或跟随中留存靠前一档"
        ),
        "retention": compare("stock.buy.retention", row, "retention_5d", ">", 0.0, "资金留存"),
        "no_giveback": compare(
            "stock.buy.no_giveback", row, "pulse_giveback", "is_false", None, "无脉冲回吐"
        ),
        "position": compare(
            "stock.buy.position",
            row,
            "ret_5d",
            "<=",
            _float(row.get("pos_limit")),
            "近 5 日涨幅不超过板块涨幅倍数",
        ),
        "drawdown": compare(
            "stock.buy.drawdown",
            row,
            "drawdown_20d_pct_20d",
            ">",
            p.buy.drawdown_extreme_pct,
            "距 20 日高点回撤不在自身最深一档",
        ),
        "tradable": compare(
            "stock.buy.tradable", row, "buy_tradable", "is_true", None, "通过不可交易过滤"
        ),
        "not_excluded": compare(
            "stock.buy.not_excluded",
            row,
            "excluded",
            "is_false",
            None,
            "无控盘风险 / 历史暴涨暴跌标签",
        ),
        "limit_outflow": compare(
            "stock.buy.limit_outflow",
            row,
            "limit_outflow",
            "is_false",
            None,
            "非涨停但主力大幅净流出",
        ),
    }


# ---------- 8.4 卖出关注 ----------


def sell_rules(row: FeatureRow, p: Params) -> dict[str, RuleResult]:
    stage = compare(
        "stock.sell.cycle.stage", row, "stage_exit", "is_true", None, "依据板块退潮 / 分歧背离"
    )
    parent = compare(
        "stock.sell.cycle.parent", row, "parent_ebb", "is_true", None, "所属一级 / 主导行业退潮"
    )
    rs = compare("stock.sell.cycle.rs_3d", row, "rs_3d_sector", "<", 0.0, "近 3 日相对强度")
    return {
        "cycle_ebb": RuleResult((stage.hit or parent.hit) and rs.hit, (stage, parent, rs)),
        "diverge_out": all_hit(
            compare("stock.sell.diverge", row, "avoid_diverge_out", "is_true", None, "背离出货成立")
        ),
        "strength_broken": all_hit(
            compare(
                "stock.sell.broken.invalid", row, "strong_invalid", "is_true", None, "强势失效"
            ),
            compare("stock.sell.broken.outflow", row, "net_main", "<", 0.0, "当日主力净额"),
        ),
        "climax_distribution": all_hit(
            compare(
                "stock.sell.climax.stage", row, "stage_climax", "is_true", None, "依据板块高潮拥挤"
            ),
            compare(
                "stock.sell.climax.volume",
                row,
                "turnover_vs_20d",
                ">=",
                p.qualifiers.volume_multiple,
                "换手 / 近 20 日均值（放量）",
            ),
            compare("stock.sell.climax.outflow", row, "net_main", "<", 0.0, "当日主力净额"),
            compare(
                "stock.sell.climax.ratio",
                row,
                "outflow_ratio",
                ">",
                _float(row.get("main_ratio_abs_mean_20d")),
                "净流出占比高于自身近 20 日常态",
            ),
        ),
    }


# ---------- 8.8 买点 / 卖点 ----------


def buy_point_rules(row: FeatureRow, p: Params) -> dict[str, RuleResult]:
    pt = p.point
    return {
        "start_confirm": all_hit(
            compare(
                "stock.point.start.fresh",
                row,
                "stage_start_fresh",
                "is_true",
                None,
                "板块迁入启动当日或次日",
            ),
            compare(
                "stock.point.start.agree", row, "price_flow_agree", "is_true", None, "价资同向"
            ),
            compare(
                "stock.point.start.ratio",
                row,
                "main_ratio_pct_20d",
                ">=",
                1 - pt.start_ratio_top,
                "净流入占比自身 20 日分位",
            ),
            compare("stock.point.start.ma5", row, "close_vs_ma5", ">", 0.0, "收盘价相对 5 日均价"),
        ),
        "pullback_hold": all_hit(
            compare(
                "stock.point.pullback.stage",
                row,
                "stage_entry",
                "is_true",
                None,
                "启动 / 扩散前半段",
            ),
            compare("stock.point.pullback.strong", row, "is_strong", "is_true", None, "强势股"),
            compare(
                "stock.point.pullback.hold",
                row,
                "pullback_hold",
                "is_true",
                None,
                "缩量回落未破 10 日均价后重新流入",
            ),
        ),
        "breakout": all_hit(
            compare(
                "stock.point.breakout.stage",
                row,
                "stage_spread_first",
                "is_true",
                None,
                "扩散前半段",
            ),
            compare(
                "stock.point.breakout.high", row, "is_new_high_20d", "is_true", None, "创 20 日新高"
            ),
            compare(
                "stock.point.breakout.amount",
                row,
                "amount_vs_ma20_prev",
                ">=",
                pt.breakout_amount_multiple,
                "成交额 / 近 20 日均值",
            ),
            compare("stock.point.breakout.inflow", row, "net_main", ">", 0.0, "当日主力净额"),
            compare(
                "stock.point.breakout.breadth",
                row,
                "sector_breadth",
                ">=",
                pt.breakout_breadth_min,
                "板块上涨家数占比",
            ),
        ),
    }


def sell_point_rules(row: FeatureRow, p: Params) -> dict[str, RuleResult]:
    return {
        "climax": all_hit(
            compare(
                "stock.spoint.climax.stage",
                row,
                "stage_climax",
                "is_true",
                None,
                "依据板块高潮拥挤",
            ),
            compare(
                "stock.spoint.climax.turnover",
                row,
                "turnover_vs_20d",
                ">=",
                p.sell.climax_turnover_multiple,
                "换手 / 近 20 日均值",
            ),
            compare("stock.spoint.climax.super", row, "net_super", "<", 0.0, "超大单净额转负"),
        ),
        "diverge": all_hit(
            compare(
                "stock.spoint.diverge", row, "avoid_diverge_out", "is_true", None, "背离出货成立"
            )
        ),
        "broken": all_hit(
            compare(
                "stock.spoint.broken.invalid", row, "strong_invalid", "is_true", None, "强势失效"
            ),
            compare("stock.spoint.broken.outflow", row, "net_main", "<", 0.0, "当日主力净额"),
            compare(
                "stock.spoint.broken.ma10", row, "close_vs_ma10", "<", 0.0, "收盘价相对 10 日均价"
            ),
        ),
        "ebb": any_hit(
            compare(
                "stock.spoint.ebb.stage", row, "stage_ebb", "is_true", None, "依据板块确认退潮"
            ),
            compare(
                "stock.spoint.ebb.parent",
                row,
                "parent_ebb",
                "is_true",
                None,
                "所属一级 / 主导行业退潮",
            ),
        ),
    }


# ---------- 8.9 低位企稳 ----------


def lowbase_rule(row: FeatureRow, p: Params) -> RuleResult:
    lb = p.lowbase
    return all_hit(
        compare(
            "stock.lowbase.range",
            row,
            "range_pos_250d",
            "<=",
            lb.range_pos_max,
            "近 250 日区间位置",
        ),
        compare(
            "stock.lowbase.drawdown",
            row,
            "drawdown_250d",
            "<=",
            -lb.drawdown_min,
            "距 250 日高点回撤",
        ),
        compare(
            "stock.lowbase.no_new_low",
            row,
            "new_low_60d_10d",
            "==",
            0.0,
            "近 10 日创 60 日新低次数",
        ),
        compare(
            "stock.lowbase.amplitude",
            row,
            "amplitude_20_60",
            "<",
            lb.amplitude_ratio_max,
            "近 20 日振幅 / 近 60 日",
        ),
        compare(
            "stock.lowbase.amount_lo",
            row,
            "amount_ratio_20_60",
            ">=",
            lb.amount_ratio_lo,
            "成交额 20 / 60 日",
        ),
        compare(
            "stock.lowbase.amount_hi",
            row,
            "amount_ratio_20_60",
            "<=",
            lb.amount_ratio_hi,
            "成交额 20 / 60 日",
        ),
        compare("stock.lowbase.inflow_5d", row, "net_main_5d", ">", 0.0, "近 5 日主力净额"),
        compare("stock.lowbase.retention", row, "retention_5d", ">", 0.0, "资金留存"),
        compare(
            "stock.lowbase.inflow_days",
            row,
            "inflow_days_10d",
            ">=",
            float(lb.inflow_days_of_10),
            "近 10 日净流入天数",
        ),
        compare("stock.lowbase.rs_eqw", row, "rs_5d_eqw", ">", 0.0, "近 5 日相对全 A 等权"),
        compare("stock.lowbase.rs_sector", row, "rs_5d_sector", ">=", 0.0, "近 5 日相对板块"),
        compare(
            "stock.lowbase.stage", row, "stage_exit", "is_false", None, "板块不处于退潮 / 分歧背离"
        ),
        compare("stock.lowbase.day_ret", row, "pct_chg", "<", lb.max_day_ret, "当日涨幅"),
        compare(
            "stock.lowbase.clean", row, "lowbase_clean", "is_true", None, "无排除标签与特殊状态"
        ),
    )


def _float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
