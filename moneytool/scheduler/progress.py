"""数据同步进度：回补线程在内存里记录当前阶段、每路当前批进度、速度与暂停原因；
接口 `/api/sync` 与控制台进度行都从这里取，再叠加库里的完成数。"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import duckdb

STAGE_LABEL = {
    "starting": "准备中",
    "reference": "同步参考数据",
    "backfill": "回补历史数据",
    "catchup": "计算历史结果",
    "deepening": "已可使用 · 后台补更早历史",
    "ready": "已就绪",
    "disabled": "未运行回补（--no-scheduler）",
}
LANE_TASK = {"bars": "bars", "flow": "flow_daily"}
LANE_LABEL = {"bars": "日线", "flow": "日频资金流"}
RATE_WINDOW_SECONDS = 600.0


class LaneProgress:
    """一路回补（日线 / 资金流）的实时状态。`state`：waiting 等待开始、running 拉取中、
    paused 暂停（数据源异常）、idle 已补完（等下次增量）。"""

    def __init__(self, name: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self.state = "waiting"
        self.note = ""
        self.resume_at: dt.datetime | None = None
        self.batch_total = 0
        self.batch_done = 0
        self._ticks: deque[float] = deque()

    def begin(self, n: int) -> None:
        with self._lock:
            self.state = "running"
            self.note = ""
            self.resume_at = None
            self.batch_total = n
            self.batch_done = 0

    def tick(self, _code: str | None = None) -> None:
        now = self._clock()
        with self._lock:
            self.batch_done += 1
            self._ticks.append(now)
            self._trim(now)

    def end(self) -> None:
        with self._lock:
            self.batch_total = 0
            self.batch_done = 0

    def pause(self, note: str, resume_at: dt.datetime | None) -> None:
        with self._lock:
            self.state = "paused"
            self.note = note
            self.resume_at = resume_at

    def idle(self) -> None:
        with self._lock:
            self.state = "idle"
            self.note = ""
            self.resume_at = None

    def _trim(self, now: float) -> None:
        while self._ticks and now - self._ticks[0] > RATE_WINDOW_SECONDS:
            self._ticks.popleft()

    def rate_per_minute(self) -> float | None:
        """最近 10 分钟每分钟处理的股票数；样本太少返回 None。"""
        now = self._clock()
        with self._lock:
            self._trim(now)
            if len(self._ticks) < 3:
                return None
            span = max(now - self._ticks[0], 30.0)  # 用到“现在”为止：卡住时速度随之下降
            return (len(self._ticks) - 1) / span * 60.0

    def snapshot(self) -> dict[str, Any]:
        rate = self.rate_per_minute()
        with self._lock:
            return {
                "state": self.state,
                "note": self.note,
                "resume_at": self.resume_at.isoformat() if self.resume_at else None,
                "batch_total": self.batch_total,
                "batch_done": self.batch_done,
                "rate_per_min": None if rate is None else round(rate, 1),
            }


class SyncProgress:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self.stage = "starting"
        self.catchup_total = 0
        self.catchup_done = 0
        self.usable = False
        self.bars_tiers: list[dict[str, str]] = []
        self.lanes = {name: LaneProgress(name, clock) for name in LANE_TASK}

    def set_usable(self, usable: bool) -> None:
        with self._lock:
            self.usable = usable

    def set_bars_tiers(self, tiers: list[dict[str, str]]) -> None:
        """日线分层定义（key / label / start），供概览按层统计。"""
        with self._lock:
            self.bars_tiers = tiers

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self.stage = stage

    def begin_catchup(self, n: int) -> str:
        """进入补算阶段，返回原阶段以便补算完恢复。"""
        with self._lock:
            prev = self.stage
            self.stage = "catchup"
            self.catchup_total = n
            self.catchup_done = 0
            return prev

    def catchup_tick(self) -> None:
        with self._lock:
            self.catchup_done += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stage, total, done = self.stage, self.catchup_total, self.catchup_done
            usable, tiers = self.usable, list(self.bars_tiers)
        return {
            "stage": stage,
            "usable": usable,
            "bars_tiers": tiers,
            "catchup": {"total": total, "done": done},
            "lanes": {name: lane.snapshot() for name, lane in self.lanes.items()},
        }


def task_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, dict[str, int]]:
    """各回补任务：total 在市证券数、done 已完成、failed 最近一次失败、pending 未开始。"""
    total_row = conn.execute("SELECT count(*) FROM security WHERE NOT is_delisting").fetchone()
    total = int(total_row[0]) if total_row else 0
    rows = conn.execute(
        "SELECT p.task, p.status, count(*) FROM backfill_progress p "
        "JOIN security s ON s.code = p.subject_id AND NOT s.is_delisting "
        "GROUP BY p.task, p.status"
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for task in LANE_TASK.values():
        by = {str(st): int(n) for t, st, n in rows if t == task}
        done, failed = by.get("done", 0), by.get("failed", 0)
        out[task] = {
            "total": total,
            "done": done,
            "failed": failed,
            "pending": max(0, total - done - failed),
        }
    return out


def _tier_rows(conn: duckdb.DuckDBPyConnection, defs: list[dict[str, str]]) -> list[dict[str, Any]]:
    from moneytool.ingest.bars import Tier, tier_counts  # noqa: PLC0415

    tiers = [Tier(d["key"], d["label"], dt.date.fromisoformat(d["start"])) for d in defs]
    return tier_counts(conn, tiers)


def sync_overview(conn: duckdb.DuckDBPyConnection, live: dict[str, Any] | None) -> dict[str, Any]:
    """合并库里的完成数与内存里的实时状态；`live` 为 None 表示本进程没跑回补（如 --no-scheduler）。
    日线分层回补时，日线一路的进度按当前层统计，各层完成情况另列在 `tiers`。"""
    counts = task_counts(conn)
    tiers = _tier_rows(conn, (live or {}).get("bars_tiers") or [])
    current = next((t for t in tiers if t["pending"] > 0), None)
    lanes: dict[str, Any] = {}
    for name, task in LANE_TASK.items():
        c = dict(counts[task])
        label = LANE_LABEL[name]
        if name == "bars" and tiers:
            layer = current or tiers[-1]
            c = {k: layer[k] for k in ("total", "done", "failed", "pending")}
            label = f"{label} · {layer['label']}"
        lv = (live or {}).get("lanes", {}).get(name, {})
        # 分层时连续失败的股票暂跳过（启动时重试），不计入剩余；资金流失败的会在后续批次重试
        remaining = c["pending"] if name == "bars" and tiers else c["total"] - c["done"]
        rate = lv.get("rate_per_min")
        eta = int(remaining / rate * 60) if rate and remaining > 0 else None
        lanes[name] = {
            "label": label,
            **c,
            "percent": round(c["done"] / c["total"] * 100, 1) if c["total"] else 0.0,
            "state": lv.get("state", "unknown"),
            "note": lv.get("note", ""),
            "resume_at": lv.get("resume_at"),
            "batch_total": lv.get("batch_total", 0),
            "batch_done": lv.get("batch_done", 0),
            "rate_per_min": rate,
            "eta_seconds": eta,
        }
    if tiers:
        for t in tiers:
            t["percent"] = round(t["done"] / t["total"] * 100, 1) if t["total"] else 0.0
        lanes["bars"]["tiers"] = tiers
    stage = (live or {}).get("stage", "unknown")
    complete = all(v["done"] + v["failed"] >= v["total"] > 0 for v in lanes.values()) and (
        current is None
    )
    etas = [v["eta_seconds"] for v in lanes.values() if v["eta_seconds"] is not None]
    return {
        "live": live is not None,
        "stage": stage,
        "stage_label": STAGE_LABEL.get(stage, "未知"),
        "usable": bool((live or {}).get("usable")) or stage == "ready",
        "complete": complete and stage in ("ready", "unknown"),
        "eta_seconds": max(etas) if etas else None,
        "catchup": (live or {}).get("catchup", {"total": 0, "done": 0}),
        "lanes": lanes,
    }


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "估算中"
    if seconds < 60:
        return "不到 1 分钟"
    h, m = divmod(seconds // 60, 60)
    return f"{h} 小时 {m} 分" if h else f"{m} 分钟"


def console_line(ov: dict[str, Any], now: dt.datetime) -> str:
    """控制台进度行，如 `[同步 14:20] 回补历史数据 · 日线 347/5222 6.6% 30只/分 剩余约 2 小时 43 分 | …`。"""
    parts = []
    for lane in ov["lanes"].values():
        text = f"{lane['label']} {lane['done']}/{lane['total']} {lane['percent']}%"
        if lane["state"] == "paused":
            text += f" 暂停：{lane['note'][:40]}"
        elif lane["state"] == "idle" or lane["done"] >= lane["total"]:
            text += " 已完成"
        elif lane["rate_per_min"]:
            text += (
                f" {lane['rate_per_min']:.0f}只/分 剩余约 {format_duration(lane['eta_seconds'])}"
            )
        parts.append(text)
    head = f"[同步 {now:%H:%M}] {ov['stage_label']}"
    if ov["stage"] == "catchup":
        head += f" {ov['catchup']['done']}/{ov['catchup']['total']} 天"
    return f"{head} · " + " | ".join(parts)
