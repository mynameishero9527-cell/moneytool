"""structlog JSON 日志，按日写 `logs/moneytool-YYYY-MM-DD.log`，保留 N 天。"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

import structlog


def setup_logging(logs_dir: Path | None, level: str = "INFO", retention_days: int = 30) -> None:
    """配置根 logger：stderr 人类可读 + 文件 JSON 行。测试时 `logs_dir=None` 只写 stderr。"""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        today = dt.date.today().isoformat()
        handlers.append(logging.FileHandler(logs_dir / f"moneytool-{today}.log", encoding="utf-8"))
        _prune_logs(logs_dir, retention_days)
    logging.basicConfig(level=level, format="%(message)s", handlers=handlers, force=True)
    # APScheduler 每次启动逐个打印「Adding job」，淹没有用信息；只保留警告以上
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def _prune_logs(logs_dir: Path, retention_days: int) -> None:
    cutoff = dt.date.today() - dt.timedelta(days=retention_days)
    for p in logs_dir.glob("moneytool-*.log"):
        try:
            day = dt.date.fromisoformat(p.stem.removeprefix("moneytool-"))
        except ValueError:
            continue
        if day < cutoff:
            p.unlink(missing_ok=True)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
