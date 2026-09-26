"""回补节奏：东财熔断冷却、日线与资金流两路互不等待。"""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from moneytool.adapters.base import (
    CircuitBreaker,
    FetchContext,
    RateLimiter,
    RawCache,
    SourceBlockedError,
    looks_blocked,
)
from moneytool.adapters.eastmoney import EastmoneyAdapter
from moneytool.app import build_context, init_data_dir
from moneytool.config import SourceRateLimit
from moneytool.ingest.bars import mark_progress
from moneytool.scheduler import jobs
from moneytool.scheduler.jobs import JobRunner


def test_breaker_trips_after_threshold_and_escalates() -> None:
    now = [0.0]
    b = CircuitBreaker(threshold=2, cooldown=10, max_cooldown=25, clock=lambda: now[0])
    assert b.failed() == 0 and b.remaining() == 0
    assert b.failed() == 10 and b.remaining() == 10
    now[0] = 11
    assert b.remaining() == 0
    b.failed()
    assert b.failed() == 20
    now[0] = 40
    b.failed()
    assert b.failed() == 25  # 封顶
    b.succeeded()
    b.failed()
    assert b.failed() == 10  # 成功后复位


def test_looks_blocked() -> None:
    assert looks_blocked(ConnectionError("('Connection aborted.', RemoteDisconnected('x'))"))
    assert looks_blocked(RuntimeError("502 Bad Gateway"))
    assert not looks_blocked(ValueError("列名不符"))


class _Ak:
    def __init__(self) -> None:
        self.calls = 0

    def stock_market_fund_flow(self) -> pd.DataFrame:
        self.calls += 1
        raise ConnectionError("('Connection aborted.', RemoteDisconnected('closed'))")


def test_eastmoney_stops_hitting_network_while_cooling(tmp_path: Path) -> None:
    ctx = FetchContext(
        cache=RawCache(tmp_path),
        rate=SourceRateLimit(backoff_seconds=(0.0, 0.0, 0.0)),
        limiter=RateLimiter(0),
        per_stock_limiter=RateLimiter(0),
    )
    ak = _Ak()
    em = EastmoneyAdapter(ctx, ak=ak, breaker=CircuitBreaker(threshold=2, cooldown=60))
    with pytest.raises(SourceBlockedError, match="冷却中"):
        em.flow_market(dt.date(2026, 9, 24))
    assert ak.calls == 2  # 第 2 次断开即熔断，剩余重试不再触网
    with pytest.raises(SourceBlockedError):
        em.flow_market(dt.date(2026, 9, 24))
    assert ak.calls == 2


def test_lanes_do_not_wait_for_each_other(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    codes = ["000001.SZ", "000002.SZ"]
    with ctx.db.write() as conn:
        for c in codes:
            conn.execute(
                "INSERT INTO security (code, name, exchange, board, is_st, is_delisting) "
                "VALUES (?, ?, 'SZ', '主板', FALSE, FALSE)",
                [c, c],
            )
            conn.execute(
                "INSERT INTO backfill_tier (task, subject_id, covered_from) VALUES ('bars', ?, DATE '1900-01-01')",
                [c],
            )
    runner = JobRunner(ctx)
    monkeypatch.setattr(jobs, "LANE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(jobs, "LANE_IDLE_SECONDS", 0.05)
    release = threading.Event()
    bars_calls: list[int] = []
    flow_calls: list[int] = []
    catchups: list[int] = []

    def bars(day: Any, n: int) -> int:
        bars_calls.append(n)
        return 1 if len(bars_calls) <= 3 else 0

    def flow(day: Any, n: int) -> int:
        flow_calls.append(n)
        if len(flow_calls) == 1:
            release.wait(10)  # 模拟资金流一批很慢
            with ctx.db.write() as conn:
                for c in codes:
                    mark_progress(conn, "flow_daily", c, "done")
            return 1
        return 0

    monkeypatch.setattr(runner, "job_startup_check", lambda: None)
    monkeypatch.setattr(runner, "_concepts_if_empty", lambda: None)
    monkeypatch.setattr(runner, "job_catchup", lambda: catchups.append(1) or 0)
    monkeypatch.setattr(runner, "_backfill_bars_batch", bars)
    monkeypatch.setattr(runner, "_backfill_flow_batch", flow)
    t = threading.Thread(target=runner.backfill_loop, daemon=True)
    t.start()
    deadline = time.monotonic() + 8
    while len(bars_calls) < 4 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(bars_calls) >= 4 and len(flow_calls) == 1  # 日线跑完了，资金流第一批还没回来
    assert catchups == []  # 资金流未完成，不补算
    release.set()
    while not catchups and time.monotonic() < deadline:
        time.sleep(0.05)
    assert catchups
    runner.stop_event.set()
    t.join(5)
    ctx.close()


def test_stale_code_detection() -> None:
    from moneytool.app import PROCESS_CODE_STAMP, code_stamp
    from moneytool.diagnose import stale_code

    assert not stale_code(PROCESS_CODE_STAMP)
    assert stale_code(None)
    assert stale_code(code_stamp() - 60)
