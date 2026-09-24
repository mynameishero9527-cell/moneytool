"""需求 7.7 / 8.11 持有结构评估与模板提示。

提示是固定模板拼句：每句对应一条证据（`links` 给出证据键），结论词只有三档，禁用词见 types.FORBIDDEN_WORDS。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import polars as pl

from moneytool.compute.indices import RETAIL_TIERS, RISK_TIERS, STOCK_RISK_ITEMS, tier_of
from moneytool.params import Params
from moneytool.rules.stock import POINT_LABEL_ZH, SELL_LABEL_ZH
from moneytool.storage.repo import to_json
from moneytool.types import STAGE_LABEL_ZH, Role, SpreadHalf, Stage

ROLE_ZH = {
    Role.CORE.value: "核心",
    Role.FOLLOW.value: "跟随",
    Role.AVOID.value: "规避",
    Role.OTHER.value: "一般成分",
}
HOLD_ZH = {"intact": "完好", "review": "需复核", "broken": "已破坏"}
SECTOR_TIER_ZH = {"focus": "可关注", "watch": "观察", "avoid": "不参与"}
MAX_SENTENCES = 4


def stage_zh(stage: str | None, half: str | None = None) -> str:
    if stage is None:
        return "阶段未知"
    label = STAGE_LABEL_ZH[Stage(stage)]
    if stage == Stage.SPREAD.value and half:
        label += "前半段" if half == SpreadHalf.FIRST.value else "后半段"
    return label


# ---------- 持有结构评估 ----------


def hold_eval(row: dict[str, Any], p: Params, gate: bool) -> tuple[str, list[str]]:
    """返回 (intact / review / broken, 触发项或证据)。`row` 含角色行 + 风险 / 散户压力总分 + 当日卖点。"""
    ix = p.indices
    sell_points = row.get("sell_points") or []
    if sell_points:
        return "broken", [POINT_LABEL_ZH.get(t, t) for t in sell_points]
    risk = row.get("risk_total")
    retail = row.get("retail_total")
    triggers: list[str] = []
    if row.get("strong_invalid") and (row.get("net_main") or 0) >= 0:
        triggers.append("强势失效但主力未净流出")
    if row.get("stage") in (Stage.CLIMAX.value, Stage.DIVERGE.value):
        triggers.append(f"板块进入{stage_zh(row.get('stage'))}")
    if risk is not None and ix.review_risk_min <= risk < ix.broken_risk_min:
        triggers.append(f"风险指数 {risk}")
    if retail is not None and retail >= ix.retail_review_min:
        triggers.append(f"近期承接筹码压力 {retail}")
    if gate:
        triggers.append("市场风控开启")
    strong = row.get("role") in (Role.CORE.value, Role.FOLLOW.value)
    active = row.get("stage") in (Stage.START.value, Stage.SPREAD.value)
    if not triggers and strong and active and (risk is None or risk < ix.review_risk_min):
        return "intact", [
            f"角色为{ROLE_ZH[row['role']]}",
            f"依据板块处于{stage_zh(row.get('stage'), row.get('half'))}",
        ]
    if not triggers:
        if not strong:
            triggers.append("角色不是核心或跟随")
        if not active:
            triggers.append(f"依据板块处于{stage_zh(row.get('stage'))}")
        if risk is not None and risk >= ix.broken_risk_min:
            triggers.append(f"风险指数 {risk}")
    return "review", triggers


def hold_text(eval_: str, items: list[str]) -> str:
    if eval_ == "intact":
        return "结构完好：" + "；".join(items[:2])
    if eval_ == "broken":
        return "结构已破坏：" + "、".join(items) + "，见卖出关注"
    return "需复核：" + "、".join(items)


# ---------- 板块提示 ----------


def sector_hint(
    row: dict[str, Any], p: Params, gate: bool, names: dict[str, str]
) -> dict[str, Any]:
    """`row`：sector_id, name, stage, half, days_in_stage, risk_total, sentiment_total, price_flow_agree,
    inflow_streak, breadth, concentration_top5, hype_score, upper_id, upper_stage。"""
    ix = p.indices
    name = row.get("name") or row["sector_id"]
    stage, half = row.get("stage"), row.get("half")
    risk = row.get("risk_total")
    entry = stage == Stage.START.value or (
        stage == Stage.SPREAD.value and half == SpreadHalf.FIRST.value
    )
    risk_tier = tier_of(risk, RISK_TIERS) or "未知"
    if (
        stage in (Stage.EBB.value, Stage.DIVERGE.value)
        or (risk is not None and risk >= ix.broken_risk_min)
        or gate
    ):
        tier = "avoid"
        if gate:
            source = "市场风控开启"
        elif risk is not None and risk >= ix.broken_risk_min:
            source = f"风险指数 {risk}（{risk_tier}）"
        else:
            source = f"阶段为{stage_zh(stage)}"
        first = f"{name} 处于{stage_zh(stage, half)}，{source}，本工具不产出买入。"
        first_links = ["stage", "risk", "gate"]
    elif entry and (risk is None or risk < ix.focus_risk_max):
        tier = "focus"
        streak = row.get("inflow_streak") or 0
        if row.get("price_flow_agree"):
            shape = "价资同向"
        elif streak > 0:
            shape = f"连续 {streak} 日净流入"
        else:
            shape = "流入尚未连续"
        first = f"{name} 处于{stage_zh(stage, half)}第 {row.get('days_in_stage') or 1} 日，资金{shape}，风险{risk_tier}。"
        first_links = ["stage", "flow", "risk"]
    else:
        tier = "watch"
        if not entry:
            conflict = "阶段不在启动或扩散前半段"
        else:
            conflict = f"风险指数 {risk}（{risk_tier}）偏高"
        first = f"{name} 处于{stage_zh(stage, half)}，{conflict}，暂不满足关注条件。"
        first_links = ["stage", "risk"]
    sentences: list[dict[str, Any]] = [{"text": first, "links": first_links}]
    hype = row.get("hype_score") or 0
    if hype >= p.attribution.min_score:
        sentences.append(
            {"text": f"资金结构带概念炒作倾向（{hype} 分）。", "links": ["attribution"]}
        )
    if row.get("upper_id"):
        up = names.get(row["upper_id"], row["upper_id"])
        sentences.append(
            {"text": f"所属 {up} 处于{stage_zh(row.get('upper_stage'))}。", "links": ["upper"]}
        )
    breadth, conc = row.get("breadth"), row.get("concentration_top5")
    if breadth is not None and conc is not None:
        sentences.append(
            {
                "text": f"上涨家数占比 {breadth:.0%}，前 5 只资金集中度 {conc:.0%}。",
                "links": ["breadth", "concentration"],
            }
        )
    sentences = sentences[:MAX_SENTENCES]
    return {"tier": tier, "tier_zh": SECTOR_TIER_ZH[tier], "sentences": sentences}


# ---------- 个股提示 ----------


def stock_hint(row: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """`row`：code, name, sector_id, stage, half, role, hold_eval, actions(list), points(list),
    risk_total, risk_items(dict), retail_total, control_risk, crash_risk, amount_tier_zh, indices(list)。"""
    stock = row.get("name") or row["code"]
    sector = names.get(row["sector_id"], row["sector_id"])
    role = ROLE_ZH.get(row.get("role") or Role.OTHER.value, "一般成分")
    hold = HOLD_ZH.get(row.get("hold_eval") or "review", "需复核")
    sentences: list[dict[str, Any]] = [
        {
            "text": f"{stock} 在{sector}（{stage_zh(row.get('stage'), row.get('half'))}）中为{role}，持有结构{hold}。",
            "links": ["role", "hold_eval"],
        }
    ]
    acts = row.get("actions") or []
    points = row.get("points") or []
    if acts or points:
        parts = []
        if "buy" in acts:
            parts.append("今日在买入关注")
        if "lowbase" in acts:
            parts.append("今日在低位企稳名单")
        sells = [SELL_LABEL_ZH.get(t, t) for t in row.get("sell_types") or []]
        if sells:
            parts.append("命中卖出关注（" + "、".join(sells) + "）")
        if points:
            parts.append("条件命中：" + "、".join(POINT_LABEL_ZH.get(t, t) for t in points))
        if parts:
            sentences.append({"text": "，".join(parts) + "。", "links": ["actions", "points"]})
    risk = row.get("risk_total")
    items = row.get("risk_items") or {}
    if risk is not None and items:
        top_key = max(items, key=lambda k: items[k] if items[k] is not None else -1)
        sentences.append(
            {
                "text": f"风险指数 {risk}（{tier_of(risk, RISK_TIERS)}），最高分项为{STOCK_RISK_ITEMS[top_key]}。",
                "links": ["stock_risk"],
            }
        )
    retail = row.get("retail_total")
    if retail is not None and retail >= 60:
        sentences.append(
            {
                "text": f"近期承接筹码压力 {retail}（{tier_of(retail, RETAIL_TIERS)}）。",
                "links": ["retail_pressure"],
            }
        )
    excl = [
        lbl
        for key, lbl in (("control_risk", "控盘风险"), ("crash_risk", "历史暴涨暴跌"))
        if row.get(key)
    ]
    if excl:
        sentences.append({"text": "带" + "、".join(excl) + "标签。", "links": ["exclusions"]})
    odd = []
    if row.get("amount_tier_zh") == "微":
        odd.append("资金体量为微")
    if row.get("indices") == ["指数外"]:
        odd.append("不属于主要指数")
    if odd:
        sentences.append({"text": "，".join(odd) + "。", "links": ["identity"]})
    return {"sentences": sentences[:MAX_SENTENCES]}


def hint_rows(
    items: list[tuple[str, str, str, dict[str, Any]]],
    day: dt.date,
    segment: str | None,
    version: str,
) -> pl.DataFrame:
    """(subject_type, subject_id, tier, payload) → hint 表行；一个主体一行，template_id 固定为 summary。"""
    schema = {
        "subject_type": pl.Utf8,
        "subject_id": pl.Utf8,
        "trade_date": pl.Date,
        "segment": pl.Utf8,
        "param_version": pl.Utf8,
        "template_id": pl.Utf8,
        "tier": pl.Utf8,
        "text": pl.Utf8,
        "links": pl.Utf8,
    }
    rows = [
        {
            "subject_type": st,
            "subject_id": sid,
            "trade_date": day,
            "segment": segment or "close",
            "param_version": version,
            "template_id": "summary",
            "tier": tier,
            "text": "".join(s["text"] for s in payload["sentences"]),
            "links": to_json(payload["sentences"]),
        }
        for st, sid, tier, payload in items
    ]
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
