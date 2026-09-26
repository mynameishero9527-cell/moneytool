"""主程序启停记录：启动、正常退出、启动 / 运行中崩溃都直接追加到当日日志文件（不依赖 logging 是否已配置），
doctor 据此区分“没启动”“正常关闭”“启动即崩溃”。"""

from __future__ import annotations

import datetime as dt
import json
import os
import traceback
from pathlib import Path
from typing import Any

STARTED = "app_started"
STOPPED = "app_stopped"
CRASHED = "app_crashed"
EVENTS = (STARTED, STOPPED, CRASHED)


def record(logs_dir: Path, event: str, level: str = "info", **fields: Any) -> None:
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.UTC)
        line = {
            "event": event,
            "level": level,
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "pid": os.getpid(),
            **fields,
        }
        path = logs_dir / f"moneytool-{dt.date.today().isoformat()}.log"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def record_crash(logs_dir: Path, exc: BaseException, stage: str) -> None:
    tb = "".join(traceback.format_exception(exc))
    record(logs_dir, CRASHED, "error", stage=stage, error=f"{type(exc).__name__}: {exc}", tb=tb)


def last_run(log_files: list[Path]) -> dict[str, Any] | None:
    """最近一次运行（按进程配对启动与结局）：{"started": 行或 None, "ended": 行或 None}。
    启动即崩溃的进程没有启动记录；同时开两个实例时各算各的。"""
    runs: dict[Any, dict[str, Any]] = {}
    latest: Any = None
    for path in log_files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            if '"app_' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("event") not in EVENTS:
                continue
            pid = obj.get("pid")
            if obj["event"] == STARTED or pid not in runs or runs[pid]["ended"] is not None:
                runs[pid] = {"started": None, "ended": None}
            runs[pid]["started" if obj["event"] == STARTED else "ended"] = obj
            latest = pid
    return runs.get(latest) if latest is not None else None


def local_time(ts: str) -> str:
    try:
        return (
            dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            .astimezone(dt.timezone(dt.timedelta(hours=8)))
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except ValueError:
        return ts[:19]
