"""回补并发：东财资金流线程池 + 自适应限速、日线与资金流同时进行、Baostock 会话保护。"""

from __future__ import annotations

import datetime as dt
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from moneytool.adapters.baostock import BaostockAdapter, _GuardedSocket
from moneytool.adapters.base import (
    AdapterError,
    AdaptiveLimiter,
    CaptchaError,
    FetchContext,
    RateLimiter,
    RawCache,
)
from moneytool.app import build_context, init_data_dir
from moneytool.config import SourceRateLimit
from moneytool.ingest.bars import TASK as BARS_TASK
from moneytool.ingest.bars import mark_progress, pending_codes
from moneytool.ingest.flow import TASK_FLOW, fetch_flow, store_flow
from moneytool.scheduler.jobs import JobRunner
from moneytool.storage.conn import Database
from moneytool.storage.migrate import migrate
from tests.compute.test_pipeline import seed_market

DAY = dt.date(2026, 1, 9)


def _hist(code: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_date": [DAY],
            "close": [10.0],
            "pct_chg": [0.01],
            **{c: [1.0] for c in ("net_main", "net_super", "net_large", "net_medium", "net_small")},
            **{
                c: [0.1]
                for c in ("main_ratio", "super_ratio", "large_ratio", "medium_ratio", "small_ratio")
            },
        }
    )


class _SlowEm:
    """每次请求耗时 0.1 秒，记录同时在途的最大请求数。"""

    def __init__(self, fail: set[str] | None = None, captcha: str | None = None) -> None:
        self.fail = fail or set()
        self.captcha = captcha
        self.calls: list[str] = []
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def flow_daily_stock(self, code: str, day: dt.date, limiter: Any = None) -> pl.DataFrame:
        if limiter is not None:
            limiter.wait()
        with self._lock:
            self.calls.append(code)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.1)
            if code == self.captcha:
                raise CaptchaError("eastmoney", "flow_daily_stock", "验证")
            if code in self.fail:
                raise AdapterError("eastmoney", "flow_daily_stock", "boom")
            return _hist(code)
        finally:
            with self._lock:
                self.active -= 1


def test_fetch_flow_runs_in_parallel() -> None:
    em = _SlowEm()
    codes = [f"{i:06d}.SZ" for i in range(12)]
    started = time.monotonic()
    out = fetch_flow(SimpleNamespace(eastmoney=em), codes, DAY, workers=4)  # type: ignore[arg-type]
    elapsed = time.monotonic() - started
    assert [c for c, _ in out] == codes
    assert em.peak == 4
    assert elapsed < 0.8  # 串行需要 1.2 秒


def test_fetch_flow_stops_on_captcha_and_reports() -> None:
    em = _SlowEm(captcha="000002.SZ")
    reasons: list[str] = []
    codes = [f"{i:06d}.SZ" for i in range(10)]
    out = fetch_flow(
        SimpleNamespace(eastmoney=em),  # type: ignore[arg-type]
        codes,
        DAY,
        workers=1,
        on_stall=reasons.append,
    )
    assert len(em.calls) == 3
    assert len(out) == 3
    assert reasons and reasons[0].startswith("触发验证")


def test_store_flow_marks_failures_but_not_captcha() -> None:
    db = Database(":memory:")
    migrate(db.rw)
    seed_market(db, n_days=3)
    with db.write() as conn:
        codes = pending_codes(conn, TASK_FLOW)[:3]
        results = [
            (codes[0], _hist(codes[0])),
            (codes[1], AdapterError("eastmoney", "flow_daily_stock", "boom")),
            (codes[2], CaptchaError("eastmoney", "flow_daily_stock", "验证")),
        ]
        assert store_flow(conn, results, DAY, dt.date(2025, 1, 1)) == 1
        rows = dict(
            conn.execute(
                "SELECT subject_id, status FROM backfill_progress WHERE task = ?", [TASK_FLOW]
            ).fetchall()
        )
    assert rows == {codes[0]: "done", codes[1]: "failed"}


def test_adaptive_limiter_backs_off_and_recovers() -> None:
    lim = AdaptiveLimiter(1.0, 10.0)
    lim.failed()
    assert lim.min_interval == 2.0
    for _ in range(5):
        lim.failed()
    assert lim.min_interval == 10.0
    for _ in range(100):
        lim.succeeded()
    assert lim.min_interval == 1.0


def test_pending_codes_puts_failed_last() -> None:
    db = Database(":memory:")
    migrate(db.rw)
    seed_market(db, n_days=3)
    with db.write() as conn:
        codes = pending_codes(conn, BARS_TASK)
        mark_progress(conn, BARS_TASK, codes[0], "failed")
        again = pending_codes(conn, BARS_TASK)
    assert again[-1] == codes[0]
    assert again[:-1] == codes[1:]


def test_batch_fetches_bars_and_flow_concurrently(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    seed_market(ctx.db, n_days=3)
    with ctx.db.write() as conn:
        conn.execute("DELETE FROM backfill_progress")
    both = threading.Barrier(2, timeout=5)
    seen: dict[str, int] = {}

    def fake_bars(ad: Any, codes: list[str], **kw: Any) -> list[Any]:
        both.wait()
        seen["bars"] = len(codes)
        return []

    def fake_flow(ad: Any, codes: list[str], day: dt.date, **kw: Any) -> list[Any]:
        both.wait()
        seen["flow"] = len(codes)
        assert kw["workers"] == ctx.settings.backfill.flow_workers
        return []

    monkeypatch.setattr("moneytool.scheduler.jobs.fetch_bars", fake_bars)
    monkeypatch.setattr("moneytool.scheduler.jobs.fetch_flow", fake_flow)
    runner = JobRunner(ctx)
    assert runner.job_backfill_batch(5) == (0, 0)
    assert seen == {"bars": 5, "flow": 5}
    ctx.close()


def test_guarded_socket_times_out_and_detects_close() -> None:
    a, b = socket.socketpair()
    g = _GuardedSocket(a, timeout=0.2)
    with pytest.raises(TimeoutError):
        g.recv(10)
    b.close()
    with pytest.raises(ConnectionError):
        g.recv(10)
    g.close()


class _FlakyBs:
    """第一次查询返回网络错误，之后正常；记录登录次数与是否有并发查询。"""

    def __init__(self) -> None:
        self.logins = 0
        self.queries = 0
        self.inflight = 0
        self.overlap = False

    def login(self) -> Any:
        self.logins += 1
        return SimpleNamespace(error_code="0", error_msg="")

    def logout(self) -> None:
        pass

    def query_trade_dates(self, start_date: str, end_date: str) -> Any:
        self.queries += 1
        self.inflight += 1
        self.overlap |= self.inflight > 1
        time.sleep(0.02)
        self.inflight -= 1
        if self.queries == 1:
            return SimpleNamespace(error_code="10002007", error_msg="网络接收错误。")
        rows = [["2026-01-09", "1"]]
        it = iter(rows)
        state: dict[str, Any] = {}

        def nxt() -> bool:
            state["row"] = next(it, None)
            return state["row"] is not None

        return SimpleNamespace(
            error_code="0",
            error_msg="",
            fields=["calendar_date", "is_trading_day"],
            next=nxt,
            get_row_data=lambda: state["row"],
        )


def test_baostock_relogs_after_network_error_and_serializes(tmp_path: Path) -> None:
    rate = SourceRateLimit(min_interval_seconds=0, backoff_seconds=(0.0,))
    ctx = FetchContext(
        cache=RawCache(tmp_path),
        rate=rate,
        limiter=RateLimiter(0),
        per_stock_limiter=RateLimiter(0),
    )
    bs = _FlakyBs()
    ad = BaostockAdapter(ctx, bs=bs)
    df = ad.trade_dates(DAY, DAY, DAY)
    assert df.height == 1
    assert bs.logins == 2

    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            ad.trade_dates(DAY - dt.timedelta(days=i + 1), DAY, DAY)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert not bs.overlap
