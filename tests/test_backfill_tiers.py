"""日线分层回补：先近半年、再近一年、最后补满；第一层补完即可计算，补深后重算近期结果。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from moneytool.adapters.base import AdapterError
from moneytool.app import build_context, init_data_dir
from moneytool.ingest.bars import (
    ADJ_FROM,
    bars_tiers,
    mark_progress,
    reset_tier_failures,
    tier_counts,
)
from moneytool.ingest.flow import restated_dates
from moneytool.scheduler.jobs import TIER_DONE_KEY, JobRunner
from moneytool.scheduler.progress import sync_overview
from moneytool.storage.repo import setting_get

DAY = dt.date(2026, 9, 25)


class FakeBaostock:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dt.date, dt.date]] = []
        self.fail = fail or set()

    def kdata(self, code: str, start: dt.date, end: dt.date, day: dt.date) -> pl.DataFrame:
        self.calls.append(("k", code, start, end))
        if code in self.fail:
            raise AdapterError("baostock", "kdata", "boom")
        dates = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        dates = [d for d in dates if d.weekday() < 5]
        n = len(dates)
        return pl.DataFrame(
            {
                "code": [code] * n,
                "trade_date": dates,
                "open": [10.0] * n,
                "high": [10.5] * n,
                "low": [9.5] * n,
                "close": [10.0] * n,
                "pre_close": [10.0] * n,
                "volume": [1e6] * n,
                "amount": [1e7] * n,
                "turnover": [0.01] * n,
                "pct_chg": [0.0] * n,
                "is_suspended": [False] * n,
                "is_st": [False] * n,
            }
        )

    def close(self) -> None:
        pass

    def adjust_factor(self, code: str, start: dt.date, end: dt.date, day: dt.date) -> pl.DataFrame:
        self.calls.append(("adj", code, start, end))
        return pl.DataFrame(
            {"code": [code], "effective_date": [dt.date(2015, 6, 1)], "adj_factor": [3.5]}
        )


def _setup(
    tmp_data_dir: Path, codes: dict[str, dt.date | None]
) -> tuple[Any, JobRunner, FakeBaostock]:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    with ctx.db.write() as conn:
        for c, listed in codes.items():
            conn.execute(
                "INSERT INTO security (code, name, exchange, board, list_date, is_st, is_delisting) "
                "VALUES (?, ?, 'SZ', '主板', ?, FALSE, FALSE)",
                [c, c, listed],
            )
    fake = FakeBaostock()
    ctx.adapters.baostock = fake
    return ctx, JobRunner(ctx), fake


def test_tier_definitions() -> None:
    tiers = bars_tiers(DAY, [183, 365], 5)
    assert [t.label for t in tiers] == ["近半年", "近 1 年", "近 5 年"]
    assert tiers[0].start == DAY - dt.timedelta(days=183)
    assert [t.label for t in bars_tiers(DAY, [183, 365, 5000], 1)] == ["近半年", "近 1 年"]


def test_tiers_fill_recent_first_then_extend(tmp_data_dir: Path) -> None:
    ctx, runner, fake = _setup(
        tmp_data_dir, {"000001.SZ": dt.date(2000, 1, 1), "000002.SZ": dt.date(2000, 1, 1)}
    )
    tiers = runner.bars_tiers(DAY)
    assert runner._backfill_bars_batch(DAY, 10) == 2
    k = [c for c in fake.calls if c[0] == "k"]
    assert {(c[2], c[3]) for c in k} == {(tiers[0].start, DAY)}
    # 复权因子从上市起取，区间前累计的因子不会丢
    assert {c[2] for c in fake.calls if c[0] == "adj"} == {ADJ_FROM}
    with ctx.db.read() as conn:
        assert runner.tiers_done(conn, DAY) == 1
        st = conn.execute(
            "SELECT DISTINCT status FROM backfill_progress WHERE task = 'bars'"
        ).fetchall()
        assert st == [("partial",)]
        adj = conn.execute("SELECT DISTINCT adj_factor FROM bar_daily").fetchall()
        assert adj == [(3.5,)]

    fake.calls.clear()
    assert runner._backfill_bars_batch(DAY, 10) == 2
    k = [c for c in fake.calls if c[0] == "k"]
    assert {(c[2], c[3]) for c in k} == {(tiers[1].start, tiers[0].start - dt.timedelta(days=1))}
    # 复权因子第一次已从上市起取全量，补更早的层不再请求
    assert [c for c in fake.calls if c[0] == "adj"] == []

    fake.calls.clear()
    assert runner._backfill_bars_batch(DAY, 10) == 2
    assert {c[2] for c in fake.calls if c[0] == "k"} == {tiers[2].start}
    assert runner._backfill_bars_batch(DAY, 10) == 0
    with ctx.db.read() as conn:
        assert runner.tiers_done(conn, DAY) == 3
        rows = conn.execute(
            "SELECT status, last_date FROM backfill_progress WHERE task = 'bars'"
        ).fetchall()
        # 补更早的层不会把最后日期改旧
        assert {r[0] for r in rows} == {"done"}
        assert {r[1] for r in rows} == {DAY}
    ctx.close()


def test_recently_listed_stock_skips_older_tiers(tmp_data_dir: Path) -> None:
    ctx, runner, fake = _setup(
        tmp_data_dir, {"000001.SZ": dt.date(2000, 1, 1), "688999.SH": DAY - dt.timedelta(days=30)}
    )
    runner._backfill_bars_batch(DAY, 10)
    fake.calls.clear()
    runner._backfill_bars_batch(DAY, 10)
    assert {c[1] for c in fake.calls if c[0] == "k"} == {"000001.SZ"}
    ctx.close()


def test_failing_stock_is_skipped_until_restart(tmp_data_dir: Path) -> None:
    ctx, runner, fake = _setup(
        tmp_data_dir, {"000001.SZ": dt.date(2000, 1, 1), "000002.SZ": dt.date(2000, 1, 1)}
    )
    fake.fail = {"000002.SZ"}
    for _ in range(3):
        runner._backfill_bars_batch(DAY, 10)
    with ctx.db.read() as conn:
        # 连续失败 3 次后本层视为补完（跳过它），计算不被个别股票卡住
        assert runner.tiers_done(conn, DAY) >= 1
        c = tier_counts(conn, runner.bars_tiers(DAY))[0]
        assert c["done"] == 1 and c["failed"] == 1 and c["pending"] == 0
    fake.fail = set()
    runner.run_job("retry", reset_tier_failures)
    with ctx.db.read() as conn:
        assert tier_counts(conn, runner.bars_tiers(DAY))[0]["pending"] == 1
    ctx.close()


def test_ready_needs_first_tier_and_flow_first_pass(tmp_data_dir: Path) -> None:
    ctx, runner, _ = _setup(tmp_data_dir, {"000001.SZ": dt.date(2000, 1, 1)})
    with ctx.db.read() as conn:
        assert not runner.history_ready(conn, DAY)
    runner._backfill_bars_batch(DAY, 10)
    with ctx.db.read() as conn:
        assert not runner.history_ready(conn, DAY)  # 资金流还没拉过
    with ctx.db.write() as conn:
        mark_progress(conn, "flow_daily", "000001.SZ", "failed")
    with ctx.db.read() as conn:
        assert runner.history_ready(conn, DAY)
    ctx.close()


def test_deeper_tier_restates_recent_results(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, runner, _ = _setup(tmp_data_dir, {"000001.SZ": dt.date(2000, 1, 1)})
    monkeypatch.setattr("moneytool.scheduler.jobs.today_sh", lambda: DAY)
    # 计算要用约 380 个交易日的历史：近半年、近一年都不够，补深后要重算
    days = [DAY - dt.timedelta(days=i) for i in range(1, 900)]
    recent = [d for d in days if d.weekday() < 5]
    with ctx.db.write() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO trade_calendar (trade_date, is_open) VALUES (?, TRUE)",
            [[d] for d in recent],
        )
    runner._backfill_bars_batch(DAY, 10)
    runner.check_tier_progress(DAY)
    with ctx.db.write() as conn:
        assert setting_get(conn, TIER_DONE_KEY) == "1"
        assert restated_dates(conn) == []  # 第一层补完前没有结果，无需重算
        conn.execute(
            "INSERT INTO market_daily (trade_date, segment, param_version, regime, risk_gate, data_status) "
            "VALUES (?, 'close', 'v2', 'none', FALSE, 'confirmed')",
            [recent[0]],
        )
    runner._backfill_bars_batch(DAY, 10)
    runner.check_tier_progress(DAY)
    with ctx.db.read() as conn:
        assert setting_get(conn, TIER_DONE_KEY) == "2"
        assert restated_dates(conn) == [recent[0]]
    ctx.close()


def test_overview_reports_current_tier(tmp_data_dir: Path) -> None:
    ctx, runner, _ = _setup(
        tmp_data_dir, {"000001.SZ": dt.date(2000, 1, 1), "000002.SZ": dt.date(2000, 1, 1)}
    )
    runner.check_tier_progress(DAY)
    with ctx.db.read() as conn:
        ov = sync_overview(conn, runner.progress.snapshot())
    assert ov["lanes"]["bars"]["label"] == "日线 · 近半年"
    assert [t["label"] for t in ov["lanes"]["bars"]["tiers"]] == ["近半年", "近 1 年", "近 5 年"]
    runner._backfill_bars_batch(DAY, 10)
    runner.progress.set_usable(True)
    with ctx.db.read() as conn:
        ov = sync_overview(conn, runner.progress.snapshot())
    assert ov["lanes"]["bars"]["label"] == "日线 · 近 1 年"
    assert ov["lanes"]["bars"]["tiers"][0]["percent"] == 100.0
    assert ov["usable"] is True and not ov["complete"]
    ctx.close()
