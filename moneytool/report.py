"""需求 9.9 每日复盘简报。全部由已存档的结果按固定模板拼成 Markdown，不用生成式模型。

收盘简报顺序固定：市场三态与情绪压力、风控；主线与归因；阶段迁移；买入 / 卖出关注增减（行业、概念分列）；
持有结构评估变化；低位企稳新增；数据质量摘要。盘前简报：昨日主线与阶段、需复核的跟踪股、静默期合并的提醒。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import duckdb

from moneytool.compute.hints import HOLD_ZH, stage_zh
from moneytool.compute.market import pressure_tier
from moneytool.types import FORBIDDEN_WORDS

REGIME_ZH = {"clear": "主线清晰", "scattered": "分散", "none": "无主线"}
GROUP_ZH = {"L1": "行业", "L2": "行业", "concept": "概念"}


def _prev_day(conn: duckdb.DuckDBPyConnection, day: dt.date, version: str) -> dt.date | None:
    row = conn.execute(
        "SELECT max(trade_date) FROM market_daily WHERE trade_date < ? AND segment = 'close' AND param_version = ?",
        [day, version],
    ).fetchone()
    return row[0] if row and row[0] else None


def _list_keys(
    conn: duckdb.DuckDBPyConnection, day: dt.date | None, version: str, list_type: str
) -> dict[tuple[str, str], dict[str, Any]]:
    if day is None:
        return {}
    rows = conn.execute(
        "SELECT a.code, a.sector_id, a.basis_level, s.name, sec.name FROM action_list_confirmed a "
        "LEFT JOIN security s ON s.code = a.code LEFT JOIN sector sec ON sec.sector_id = a.sector_id "
        "WHERE a.trade_date = ? AND a.param_version = ? AND a.list_type = ?",
        [day, version, list_type],
    ).fetchall()
    return {
        (r[0], r[1]): {
            "code": r[0],
            "basis_level": r[2],
            "name": r[3] or r[0],
            "sector": r[4] or r[1],
        }
        for r in rows
    }


def _diff_lines(
    today: dict[tuple[str, str], dict[str, Any]], prev: dict[tuple[str, str], dict[str, Any]]
) -> list[str]:
    lines: list[str] = []
    for label, keys, src in (
        ("新增", today.keys() - prev.keys(), today),
        ("移出", prev.keys() - today.keys(), prev),
    ):
        for group in ("行业", "概念"):
            items = [
                src[k] for k in sorted(keys) if GROUP_ZH.get(src[k]["basis_level"], "行业") == group
            ]
            if items:
                body = "、".join(f"{i['name']}（{i['sector']}）" for i in items[:20])
                more = f" 等 {len(items)} 只" if len(items) > 20 else ""
                lines.append(f"- {label}·{group}：{body}{more}")
    return lines or ["- 无变化"]


def close_brief(conn: duckdb.DuckDBPyConnection, day: dt.date, version: str) -> str:
    m = conn.execute(
        "SELECT regime, market_pressure, risk_gate, risk_gate_reasons, mainline, data_status FROM market_daily "
        "WHERE trade_date = ? AND segment = 'close' AND param_version = ?",
        [day, version],
    ).fetchone()
    out = [f"# 收盘简报 {day.isoformat()}", ""]
    if m is None:
        return "\n".join([*out, "当日暂无收盘确认结果。"])
    regime, pressure, gate, reasons, mainline, status = m
    out += [
        "## 市场",
        f"- 市场宽度：{REGIME_ZH.get(regime, regime)}；情绪压力 {pressure if pressure is not None else '—'}"
        f"（{pressure_tier(pressure) or '历史不足'}）",
        "- 市场风控："
        + (
            "开启，暂停新增买入关注与低位企稳；原因：" + "、".join(_reasons(reasons))
            if gate
            else "关闭"
        ),
        f"- 数据状态：{status}",
        "",
        "## 主线板块",
    ]
    ml = json.loads(mainline) if mainline else []
    if not ml:
        out.append("- 无主线")
    for item in ml:
        att = conn.execute(
            "SELECT json_extract_string(attribution, '$.summary') FROM sector_stage_confirmed "
            "WHERE sector_id = ? AND trade_date = ? AND param_version = ?",
            [item.get("sector_id"), day, version],
        ).fetchone()
        out.append(
            f"- {item.get('name') or item.get('sector_id')}：{stage_zh(item.get('stage'))}"
            + (f"，归因：{att[0]}" if att and att[0] else "")
        )
    out += ["", "## 阶段迁移"]
    moves = conn.execute(
        """
        SELECT sec.name, sec.level, s.entered_from, s.stage, s.abnormal_transition, s.suspected_to
        FROM sector_stage_confirmed s JOIN sector sec USING (sector_id)
        WHERE s.trade_date = ? AND s.param_version = ?
          AND ((s.days_in_stage = 1 AND s.entered_from IS NOT NULL) OR s.suspected_to IS NOT NULL)
          AND NOT s.small_sample
        ORDER BY sec.level, sec.name
        """,
        [day, version],
    ).fetchall()
    if not moves:
        out.append("- 无确认迁移")
    for name, level, frm, to, abnormal, suspected in moves[:40]:
        if suspected:
            out.append(f"- {name}（{level}）：疑似 {stage_zh(to)} → {stage_zh(suspected)}")
        else:
            out.append(
                f"- {name}（{level}）：{stage_zh(frm)} → {stage_zh(to)}"
                + ("（异常迁移）" if abnormal else "")
            )
    if len(moves) > 40:
        out.append(f"- 另有 {len(moves) - 40} 条，见板块看板")
    prev = _prev_day(conn, day, version)
    for title, lt in (("买入关注", "buy"), ("卖出关注", "sell")):
        out += ["", f"## {title}"]
        out += _diff_lines(_list_keys(conn, day, version, lt), _list_keys(conn, prev, version, lt))
    out += ["", "## 持有结构评估变化"]
    changes = conn.execute(
        "SELECT h.code, s.name, h.changed_from, h.eval FROM hold_eval h LEFT JOIN security s USING (code) "
        "WHERE h.trade_date = ? AND h.param_version = ? AND h.changed_from IS NOT NULL ORDER BY h.code",
        [day, version],
    ).fetchall()
    out += [
        f"- {name or code}：{HOLD_ZH.get(frm, frm)} → {HOLD_ZH.get(to, to)}"
        for code, name, frm, to in changes
    ] or ["- 无变化"]
    out += ["", "## 低位企稳新增"]
    low_today = _list_keys(conn, day, version, "lowbase")
    low_prev = _list_keys(conn, prev, version, "lowbase")
    new_low = [low_today[k] for k in sorted(low_today.keys() - low_prev.keys())]
    out += [f"- {i['name']}（{i['sector']}）" for i in new_low] or ["- 无"]
    out += ["", "## 数据质量"]
    q = conn.execute(
        "SELECT status, count(*) FROM data_quality WHERE trade_date = ? GROUP BY 1 ORDER BY 1",
        [day],
    ).fetchall()
    unrec = conn.execute(
        "SELECT count(*) FILTER (WHERE NOT reconciled), count(*) FROM flow_daily WHERE trade_date = ?",
        [day],
    ).fetchone()
    out += [f"- {st}：{n} 条" for st, n in q] or ["- 无缺失记录"]
    if unrec and unrec[1]:
        out.append(f"- 未经对账资金流：{unrec[0]} / {unrec[1]}")
    out += ["", "> 本简报由规则结果按固定模板生成，描述资金结构，不构成收益承诺。"]
    return _clean("\n".join(out))


def premarket_brief(
    conn: duckdb.DuckDBPyConnection, day: dt.date, version: str, quiet_alerts: list[str]
) -> str:
    """当日 8:30：昨日确认的主线与阶段、跟踪中需复核、静默期合并的提醒。"""
    prev = _prev_day(conn, day + dt.timedelta(days=1), version)
    out = [f"# 盘前简报 {day.isoformat()}", ""]
    if prev is None:
        return "\n".join([*out, "暂无上一交易日收盘确认结果。"])
    m = conn.execute(
        "SELECT regime, mainline, risk_gate FROM market_daily WHERE trade_date = ? AND segment = 'close' "
        "AND param_version = ?",
        [prev, version],
    ).fetchone()
    out.append(f"## 昨日（{prev.isoformat()}）主线与阶段")
    if m:
        out.append(
            f"- 市场宽度：{REGIME_ZH.get(m[0], m[0])}；市场风控：{'开启' if m[2] else '关闭'}"
        )
        for item in json.loads(m[1]) if m[1] else []:
            out.append(
                f"- {item.get('name') or item.get('sector_id')}：{stage_zh(item.get('stage'))}"
            )
    out += ["", "## 需复核"]
    review = conn.execute(
        "SELECT h.code, s.name, json_extract_string(h.evidence, '$.text') FROM hold_eval h "
        "LEFT JOIN security s USING (code) WHERE h.trade_date = ? AND h.param_version = ? AND h.eval <> 'intact' "
        "ORDER BY h.eval, h.code",
        [prev, version],
    ).fetchall()
    out += [f"- {name or code}：{text}" for code, name, text in review] or ["- 无"]
    out += ["", "## 静默期提醒"]
    out += [f"- {t}" for t in quiet_alerts] or ["- 无"]
    return _clean("\n".join(out))


def _reasons(raw: str | None) -> list[str]:
    try:
        v = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


def _clean(text: str) -> str:
    for w in FORBIDDEN_WORDS:
        if w in text:
            raise ValueError(f"简报含禁用词：{w}")
    return text


def save_brief(
    conn: duckdb.DuckDBPyConnection, day: dt.date, kind: str, markdown: str, briefs_dir: Path | None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO brief (trade_date, kind, markdown, generated_at) VALUES (?, ?, ?, now())",
        [day, kind, markdown],
    )
    if briefs_dir is not None:
        briefs_dir.mkdir(parents=True, exist_ok=True)
        (briefs_dir / f"{day.isoformat()}-{kind}.md").write_text(markdown, encoding="utf-8")
