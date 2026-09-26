"""需求 9.6 提醒：事件收集 → 去重 → 分组合并 → 频次上限 → 静默 → 推送（Webhook）。

事件仅限：风控开关开启 / 关闭；自选板块确认阶段迁移；自选股新进入买入关注 / 卖出关注；自选股强势失效；
跟踪中的股票命中卖出关注；板块资金动向（资金异动、趋势倾向转向、多周期共振），自选板块全部推送，
其余只推每类前 3 名一级行业。竞价段不触发；盘中事件标「盘中」，收盘时未被确认的补发「已撤销」。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

import duckdb
import httpx

from moneytool.compute.hints import stage_zh
from moneytool.compute.horizons import SIGNAL_ZH
from moneytool.config import NotifyConfig
from moneytool.logging import get_logger
from moneytool.types import FORBIDDEN_WORDS, Segment

log = get_logger(__name__)

DAILY_CAP = 30
GROUP_MIN = 3
PRIORITY_EVENTS = ("gate_on", "gate_off")
EVENT_ZH = {
    "gate_on": "市场风控开启",
    "gate_off": "市场风控关闭",
    "sector_transition": "阶段迁移",
    "watch_buy_new": "新进入买入关注",
    "watch_sell_new": "新进入卖出关注",
    "watch_strong_invalid": "强势失效",
    "tracking_sell": "跟踪中命中卖出关注",
    **{f"flow_{k}": v for k, v in SIGNAL_ZH.items()},
}
FLOW_TOP_L1 = 3


@dataclass
class Event:
    subject_id: str
    event: str
    text: str
    sector_id: str | None = None
    payload: dict[str, Any] | None = None


def _names(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT code, name FROM security UNION ALL SELECT sector_id, name FROM sector"
    ).fetchall()
    return {str(a): str(b) for a, b in rows}


def collect_events(
    conn: duckdb.DuckDBPyConnection, day: dt.date, segment: str | None, version: str
) -> list[Event]:
    """对比「本次结果」与「上一交易日收盘确认」。`segment=None` 为收盘确认。"""
    if segment == Segment.AUCTION.value:
        return []
    intraday = segment is not None
    names = _names(conn)
    watch = {r[0] for r in conn.execute("SELECT DISTINCT code FROM watchlist").fetchall()}
    table = "action_list_intraday" if intraday else "action_list_confirmed"
    seg_clause = " AND segment = ?" if intraday else ""
    seg_args = [segment] if intraday else []
    prev_day_row = conn.execute(
        "SELECT max(trade_date) FROM action_list_confirmed WHERE trade_date < ? AND param_version = ?",
        [day, version],
    ).fetchone()
    prev_day = prev_day_row[0] if prev_day_row else None
    suffix = "（盘中）" if intraday else ""
    events: list[Event] = []

    def keys(
        tbl: str, d: dt.date | None, lt: str, extra: str = "", args: list[Any] | None = None
    ) -> set[tuple[str, str]]:
        if d is None:
            return set()
        rows = conn.execute(
            f"SELECT code, sector_id FROM {tbl} WHERE trade_date = ? AND param_version = ? AND list_type = ?{extra}",
            [d, version, lt, *(args or [])],
        ).fetchall()
        return {(str(a), str(b)) for a, b in rows}

    for lt, ev in (("buy", "watch_buy_new"), ("sell", "watch_sell_new")):
        now = keys(table, day, lt, seg_clause, seg_args)
        before = keys("action_list_confirmed", prev_day, lt)
        for code, sector in sorted(now - before):
            if code in watch:
                events.append(
                    Event(
                        code,
                        ev,
                        f"{names.get(code, code)} {EVENT_ZH[ev]}（{names.get(sector, sector)}）{suffix}",
                        sector,
                    )
                )
    tracked = {
        (str(a), str(b))
        for a, b in conn.execute(
            "SELECT code, sector_id FROM tracking WHERE param_version = ? AND entered_date < ? "
            "AND (exited_date IS NULL OR exited_date >= ?)",
            [version, day, day],
        ).fetchall()
    }
    for code, sector in sorted(keys(table, day, "sell", seg_clause, seg_args) & tracked):
        events.append(
            Event(
                code,
                "tracking_sell",
                f"{names.get(code, code)} {EVENT_ZH['tracking_sell']}（{names.get(sector, sector)}），结构已变化，优先复核{suffix}",
                sector,
            )
        )
    role_table = "stock_role_intraday" if intraday else "stock_role_confirmed"
    rows = conn.execute(
        f"SELECT code, sector_id FROM {role_table} WHERE trade_date = ? AND param_version = ?{seg_clause} "
        "AND CAST(tags AS VARCHAR) LIKE '%强势失效%'",
        [day, version, *seg_args],
    ).fetchall()
    for code, sector in rows:
        if code in watch:
            events.append(
                Event(
                    str(code),
                    "watch_strong_invalid",
                    f"{names.get(code, code)} {EVENT_ZH['watch_strong_invalid']}（{names.get(sector, sector)}）{suffix}",
                    str(sector),
                )
            )
    if not intraday:
        for sid, frm, to in conn.execute(
            "SELECT sector_id, entered_from, stage FROM sector_stage_confirmed WHERE trade_date = ? "
            "AND param_version = ? AND days_in_stage = 1 AND entered_from IS NOT NULL",
            [day, version],
        ).fetchall():
            if sid in watch:
                events.append(
                    Event(
                        str(sid),
                        "sector_transition",
                        f"{names.get(sid, sid)} 阶段迁移：{stage_zh(frm)} → {stage_zh(to)}",
                    )
                )
        gates = conn.execute(
            "SELECT trade_date, risk_gate, risk_gate_reasons FROM market_daily WHERE segment = 'close' "
            "AND param_version = ? AND trade_date <= ? ORDER BY trade_date DESC LIMIT 2",
            [version, day],
        ).fetchall()
        if len(gates) == 2 and gates[0][0] == day and bool(gates[0][1]) != bool(gates[1][1]):
            on = bool(gates[0][1])
            reasons = "、".join(json.loads(gates[0][2] or "[]")) if on else ""
            text = (
                "市场风控开启：暂停新增买入关注与低位企稳"
                + (f"，原因：{reasons}" if reasons else "")
                if on
                else "市场风控关闭"
            )
            events.append(Event("market", "gate_on" if on else "gate_off", text))
    events += _flow_events(conn, day, segment, version, watch, suffix)
    return events


def _flow_events(
    conn: duckdb.DuckDBPyConnection,
    day: dt.date,
    segment: str | None,
    version: str,
    watch: set[str],
    suffix: str,
) -> list[Event]:
    """资金动向信号：自选板块全部推送；其余只推每类信号强度前几名的一级行业。"""
    rows = conn.execute(
        "SELECT sector_id, kind, score, text, json_extract_string(payload, '$.level') FROM flow_signal "
        "WHERE trade_date = ? AND segment = ? AND param_version = ? ORDER BY kind, abs(score) DESC",
        [day, segment or "close", version],
    ).fetchall()
    taken: dict[str, int] = {}
    out: list[Event] = []
    for raw_id, kind, score, text, level in rows:
        sid = str(raw_id)
        if sid not in watch:
            if level != "L1" or taken.get(kind, 0) >= FLOW_TOP_L1:
                continue
            taken[kind] = taken.get(kind, 0) + 1
        out.append(Event(sid, f"flow_{kind}", f"{text}{suffix}", payload={"score": score}))
    return out


def _group(events: list[Event]) -> list[Event]:
    """同一板块 ≥ 3 只股票触发同类事件 → 合并为一条。"""
    buckets: dict[tuple[str, str], list[Event]] = {}
    out: list[Event] = []
    for e in events:
        if e.sector_id and e.event not in PRIORITY_EVENTS and e.event != "sector_transition":
            buckets.setdefault((e.sector_id, e.event), []).append(e)
        else:
            out.append(e)
    for (sector, ev), items in buckets.items():
        if len(items) >= GROUP_MIN:
            head = items[0].text.split("（")[-1].rstrip("）")
            out.append(
                Event(
                    f"group:{sector}",
                    ev,
                    f"{head} {len(items)} 只{EVENT_ZH[ev]}",
                    sector,
                    {"items": [i.subject_id for i in items]},
                )
            )
        else:
            out.extend(items)
    return out


def in_quiet_hours(now: dt.datetime, cfg: NotifyConfig) -> bool:
    start = dt.time.fromisoformat(cfg.quiet_start)
    end = dt.time.fromisoformat(cfg.quiet_end)
    t = now.time()
    return t >= start or t < end if start > end else start <= t < end


def dispatch(
    conn: duckdb.DuckDBPyConnection,
    day: dt.date,
    segment: str | None,
    version: str,
    cfg: NotifyConfig,
    now: dt.datetime,
    trading_day: bool,
) -> list[str]:
    """收集并记录本次事件，返回本次推送的文本。静默期或非交易日只记录（进盘前简报）。"""
    events = _group(collect_events(conn, day, segment, version))
    seg = segment or "close"
    if segment is None:
        events += _revoked(conn, day, version)
    sent_today = conn.execute(
        "SELECT count(*) FROM alert_log WHERE trade_date = ? AND event NOT IN ('gate_on', 'gate_off')",
        [day],
    ).fetchone()
    budget = DAILY_CAP - int(sent_today[0] if sent_today else 0)
    fresh: list[Event] = []
    overflow = 0
    for e in events:
        dup = conn.execute(
            "SELECT 1 FROM alert_log WHERE trade_date = ? AND subject_id = ? AND event = ?",
            [day, e.subject_id, e.event],
        ).fetchone()
        if dup:
            continue
        if e.event not in PRIORITY_EVENTS:
            if budget <= 0:
                overflow += 1
                continue
            budget -= 1
        fresh.append(e)
    if overflow:
        fresh.append(Event("overflow", "overflow", f"还有 {overflow} 条变化，进入行动清单查看"))
    quiet = in_quiet_hours(now, cfg) or not trading_day
    texts = [e.text for e in fresh]
    for t in texts:
        if any(w in t for w in FORBIDDEN_WORDS):
            raise ValueError(f"提醒含禁用词：{t}")
    pushed = False
    if texts and not quiet:
        pushed = push(cfg, texts)
    for e in fresh:
        conn.execute(
            "INSERT OR REPLACE INTO alert_log (trade_date, subject_id, event, segment, payload, pushed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                day,
                e.subject_id,
                e.event,
                seg,
                json.dumps({"text": e.text, **(e.payload or {})}, ensure_ascii=False),
                now if pushed else None,
            ],
        )
    return texts if pushed else []


def _revoked(conn: duckdb.DuckDBPyConnection, day: dt.date, version: str) -> list[Event]:
    """盘中已提醒、收盘未确认的名单事件补发「已撤销」。"""
    rows = conn.execute(
        """
        SELECT a.subject_id, a.event, json_extract_string(a.payload, '$.text') FROM alert_log a
        WHERE a.trade_date = ? AND a.segment <> 'close' AND a.event IN ('watch_buy_new', 'watch_sell_new', 'tracking_sell')
          AND NOT EXISTS (
            SELECT 1 FROM action_list_confirmed c WHERE c.trade_date = a.trade_date AND c.code = a.subject_id
               AND c.param_version = ? AND c.list_type = CASE a.event WHEN 'watch_buy_new' THEN 'buy' ELSE 'sell' END)
        """,
        [day, version],
    ).fetchall()
    return [
        Event(str(s), f"{e}_revoked", f"已撤销：{str(t or '').replace('（盘中）', '')}")
        for s, e, t in rows
    ]


def push(cfg: NotifyConfig, texts: list[str]) -> bool:
    if not cfg.webhook_url:
        log.info("alerts", count=len(texts), texts=texts[:5])
        return True
    try:
        r = httpx.post(
            cfg.webhook_url,
            json={"msgtype": "text", "text": {"content": "\n".join(texts)}},
            timeout=10.0,
        )
        r.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("webhook_failed", error=str(exc))
        return False


def pending_quiet(conn: duckdb.DuckDBPyConnection, since: dt.date) -> list[str]:
    """静默期未推送的提醒，合并进盘前简报。"""
    rows = conn.execute(
        "SELECT json_extract_string(payload, '$.text') FROM alert_log WHERE pushed_at IS NULL AND trade_date >= ? "
        "ORDER BY created_at",
        [since],
    ).fetchall()
    return [str(r[0]) for r in rows if r[0]]
