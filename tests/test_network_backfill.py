"""代理设置、资金流回补遇连续失败暂停、价格模式结果在资金流补齐后重算。"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from moneytool.adapters.base import AdapterError
from moneytool.app import build_context, init_data_dir
from moneytool.config import Settings
from moneytool.ingest.flow import backfill_flow
from moneytool.network import SOURCE_DOMAINS, apply_network
from moneytool.scheduler.jobs import JobRunner
from moneytool.storage.conn import Database
from moneytool.storage.migrate import migrate
from tests.compute.test_pipeline import seed_market


def test_direct_mode_bypasses_proxy_for_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "localhost")
    s = Settings()
    assert apply_network(s) == "数据源直连（不走代理）"
    assert os.environ["NO_PROXY"].split(",")[0] == "localhost"
    assert set(SOURCE_DOMAINS) <= set(os.environ["NO_PROXY"].split(","))
    assert requests.utils.should_bypass_proxies("https://push2his.eastmoney.com/api", no_proxy=None)
    assert not requests.utils.should_bypass_proxies("https://example.org/", no_proxy=None)


def test_explicit_proxy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(network={"proxy": "http://127.0.0.1:7890"})
    assert apply_network(s) == "使用代理 http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"


class _FailingEm:
    def __init__(self) -> None:
        self.calls = 0

    def flow_daily_stock(self, code: str, day: dt.date, limiter: Any = None) -> Any:
        self.calls += 1
        raise AdapterError("eastmoney", "flow_daily_stock", "ProxyError")


def test_backfill_flow_stops_after_consecutive_failures() -> None:
    db = Database(":memory:")
    migrate(db.rw)
    seed_market(db, n_days=5)
    em = _FailingEm()
    reasons: list[str] = []
    with db.write() as conn:
        done = backfill_flow(
            conn,
            SimpleNamespace(eastmoney=em),  # type: ignore[arg-type]
            day=dt.date(2026, 1, 9),
            since=dt.date(2025, 1, 1),
            on_stall=reasons.append,
        )
    assert done == 0
    assert em.calls == 3
    assert reasons and "连续 3 只失败" in reasons[0]


def test_runner_pauses_flow_and_skips_it(tmp_data_dir: Path) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    runner = JobRunner(ctx)
    assert not runner.flow_paused()
    runner._pause_flow("连续 3 只失败")
    assert runner.flow_paused()
    ctx.close()


def test_catchup_recomputes_degraded_day_after_flow_arrives(tmp_data_dir: Path) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    dates = seed_market(ctx.db, n_days=70)
    ctx.settings.schedule.catchup_days = 1
    runner = JobRunner(ctx)
    last = dates[-1]
    with ctx.db.write() as conn:
        saved = conn.execute("SELECT * FROM flow_daily WHERE trade_date = ?", [last]).pl()
        conn.execute("DELETE FROM flow_daily WHERE trade_date = ?", [last])
    assert runner.job_catchup() == 1
    with ctx.db.read() as conn:
        status = conn.execute(
            "SELECT data_status FROM market_daily WHERE trade_date = ? AND segment = 'close'",
            [last],
        ).fetchone()
        assert status == ("degraded",)
    assert runner.job_catchup() == 0
    with ctx.db.write() as conn:
        conn.register("saved", saved)
        conn.execute("INSERT INTO flow_daily SELECT * FROM saved")
    assert runner.job_catchup() == 1
    with ctx.db.read() as conn:
        status = conn.execute(
            "SELECT data_status FROM market_daily WHERE trade_date = ? AND segment = 'close'",
            [last],
        ).fetchone()
        assert status == ("confirmed",)
    ctx.close()
