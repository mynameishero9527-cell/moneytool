"""Baostock 黑名单（10001011）：不重试、不反复登录；日线回补暂停且不计入各股失败次数。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from moneytool.adapters.baostock import BaostockAdapter
from moneytool.adapters.base import FetchContext, RateLimiter, RawCache, SourceBlockedError
from moneytool.config import SourceRateLimit
from moneytool.ingest.bars import tier_counts
from tests.test_backfill_tiers import DAY, _setup


class _Login:
    def __init__(self, code: str, msg: str) -> None:
        self.error_code = code
        self.error_msg = msg


class _BlacklistedBs:
    def __init__(self) -> None:
        self.logins = 0

    def login(self) -> _Login:
        self.logins += 1
        return _Login("10001011", "黑名单用户，请与管理员联系")

    def logout(self) -> None:
        pass


def test_blacklist_raises_blocked_without_retry_or_relogin(tmp_path: Path) -> None:
    ctx = FetchContext(
        cache=RawCache(tmp_path),
        rate=SourceRateLimit(backoff_seconds=(0.0, 0.0, 0.0)),
        limiter=RateLimiter(0),
        per_stock_limiter=RateLimiter(0),
    )
    bs = _BlacklistedBs()
    ad = BaostockAdapter(ctx, bs=bs)
    with pytest.raises(SourceBlockedError, match="黑名单"):
        ad.kdata("000001.SZ", DAY, DAY, DAY)
    with pytest.raises(SourceBlockedError):
        ad.kdata("000002.SZ", DAY, DAY, DAY)
    assert bs.logins == 1
    assert ad.health(DAY)["ok"] is False


class _BlockedFake:
    def __init__(self) -> None:
        self.calls = 0

    def kdata(self, code: str, *a: Any) -> Any:
        self.calls += 1
        raise SourceBlockedError("baostock", "login", "10001011 黑名单用户")

    def adjust_factor(self, *a: Any) -> Any:
        raise AssertionError("不应调用")

    def close(self) -> None:
        pass


def test_blacklist_pauses_bars_lane_without_failures(tmp_data_dir: Path) -> None:
    ctx, runner, _ = _setup(
        tmp_data_dir, {"000001.SZ": dt.date(2000, 1, 1), "000002.SZ": dt.date(2000, 1, 1)}
    )
    blocked = _BlockedFake()
    ctx.adapters.baostock = blocked
    assert runner._backfill_bars_batch(DAY, 10) == 0
    assert blocked.calls == 1  # 第一只就停，不再逐只试
    assert runner.bars_paused()
    first = runner.bars_paused_until
    with ctx.db.read() as conn:
        c = tier_counts(conn, runner.bars_tiers(DAY))[0]
        assert c["failed"] == 0 and c["pending"] == 2
        n = conn.execute("SELECT count(*) FROM data_quality WHERE source = 'baostock'").fetchone()
        assert n == (1,)
        assert runner._bars_ready(conn, DAY) is False
    assert blocked.calls == 1
    runner._pause_bars("again")
    assert runner.bars_paused_until is not None and first is not None
    assert runner.bars_paused_until > first
    ctx.close()
