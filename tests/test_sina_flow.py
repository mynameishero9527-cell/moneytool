"""新浪日频资金流：字段映射、增量取数、每日刷新选股、快照近似值被改写后重算。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from moneytool.adapters.base import AdapterError, FetchContext, RateLimiter, RawCache
from moneytool.adapters.sina import SinaAdapter, code_to_sina, rows_for
from moneytool.app import build_context, init_data_dir
from moneytool.config import BackfillConfig, SourceRateLimit
from moneytool.ingest.bars import mark_progress
from moneytool.ingest.flow import (
    TASK_FLOW,
    flow_last_dates,
    pending_flow_codes,
    restated_dates,
    store_flow,
)
from moneytool.scheduler.jobs import JobRunner
from tests.compute.test_pipeline import seed_market

ROW = {
    "opendate": "2026-09-24",
    "trade": "40.6900",
    "changeratio": "0.00221675",
    "r0": "900.0",
    "r1": "600.0",
    "r2": "400.0",
    "r3": "100.0",
    "r0_net": "-40.0",
    "r1_net": "100.0",
    "r2_net": "30.0",
    "r3_net": "2.0",
}


class _Resp:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _adapter(tmp_path: Path, text: str) -> tuple[SinaAdapter, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def get(url: str, params: dict[str, Any], **kw: Any) -> _Resp:
        calls.append(params)
        return _Resp(text)

    ctx = FetchContext(
        cache=RawCache(tmp_path),
        rate=SourceRateLimit(backoff_seconds=()),
        limiter=RateLimiter(0),
        per_stock_limiter=RateLimiter(0),
    )
    return SinaAdapter(ctx, years=2, get=get), calls


def test_sina_maps_tiers_and_ratios(tmp_path: Path) -> None:
    older = {**ROW, "opendate": "2026-09-23"}
    ad, calls = _adapter(tmp_path, json.dumps([ROW, older]))
    df = ad.flow_daily_stock("600036.SH", dt.date(2026, 9, 26))
    assert calls[0]["daima"] == "sh600036"
    assert calls[0]["num"] == 520
    assert df["trade_date"].to_list() == [dt.date(2026, 9, 23), dt.date(2026, 9, 24)]
    r = df.row(1, named=True)
    assert r["net_super"] == -40.0 and r["net_large"] == 100.0
    assert r["net_main"] == 60.0
    assert r["net_small"] == 2.0
    assert r["main_ratio"] == pytest.approx(60.0 / 2000.0)
    assert r["pct_chg"] == pytest.approx(0.00221675)
    assert r["close"] == pytest.approx(40.69)


def test_sina_empty_and_bad_responses(tmp_path: Path) -> None:
    ad, _ = _adapter(tmp_path, "null")
    assert ad.flow_daily_stock("430017.BJ", dt.date(2026, 9, 26)).is_empty()
    bad, _ = _adapter(tmp_path / "b", "<html>blocked</html>")
    with pytest.raises(AdapterError, match="非 JSON"):
        bad.flow_daily_stock("600036.SH", dt.date(2026, 9, 26), retries=False)


def test_rows_for_and_codes() -> None:
    day = dt.date(2026, 9, 26)
    assert rows_for(None, day, 2) == 520
    assert rows_for(None, day, 20) == 2000
    assert rows_for(day - dt.timedelta(days=3), day, 2) == 13
    assert code_to_sina("000001.SZ") == "sz000001"


def test_pace_defaults_by_source() -> None:
    assert BackfillConfig().flow_pace("sina") == (2, 1.0)
    assert BackfillConfig().flow_pace("eastmoney") == (1, 3.0)
    assert BackfillConfig(flow_workers=3, flow_interval_seconds=2.5).flow_pace("sina") == (3, 2.5)


def test_pending_flow_codes_refreshes_stale_once(tmp_data_dir: Path) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    seed_market(ctx.db, n_days=3)
    target = dt.date(2026, 9, 24)
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    earlier = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    with ctx.db.write() as conn:
        conn.execute("DELETE FROM backfill_progress")
        codes = [r[0] for r in conn.execute("SELECT code FROM security ORDER BY code").fetchall()]
        mark_progress(conn, TASK_FLOW, codes[0], "done", target)
        mark_progress(conn, TASK_FLOW, codes[1], "done", target - dt.timedelta(days=1))
        mark_progress(conn, TASK_FLOW, codes[2], "failed")
        todo = pending_flow_codes(conn, target, later)
        assert codes[0] not in todo
        assert todo[-1] == codes[1]  # 增量刷新排最后
        assert todo[-2] == codes[2]  # 失败的排在从未拉过的后面
        assert codes[1] not in pending_flow_codes(conn, target, earlier)  # 刚试过不再重复
        assert codes[1] not in pending_flow_codes(conn, None, later)  # 东财来源不做每日刷新
        mark_progress(conn, TASK_FLOW, codes[1], "done", None)
        assert flow_last_dates(conn, [codes[1]]) == {codes[1]: target - dt.timedelta(days=1)}
    ctx.close()


def test_restated_day_is_recomputed(tmp_data_dir: Path) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    dates = seed_market(ctx.db, n_days=70)
    ctx.settings.schedule.catchup_days = 1
    runner = JobRunner(ctx)
    last = dates[-1]
    assert runner.job_catchup() == 1
    assert runner.job_catchup() == 0
    with ctx.db.write() as conn:
        conn.execute("UPDATE flow_daily SET reconciled = FALSE WHERE trade_date = ?", [last])
        rows = conn.execute(
            "SELECT code, trade_date, net_main, net_super, net_large, net_medium, net_small, "
            "main_ratio, super_ratio, large_ratio, medium_ratio, small_ratio, close, pct_chg "
            "FROM flow_daily WHERE trade_date = ? ORDER BY code LIMIT 1",
            [last],
        ).pl()
        code = rows["code"][0]
        store_flow(conn, [(code, rows.drop("code"))], last, last - dt.timedelta(days=30))
        assert restated_dates(conn) == [last]
    assert runner.job_catchup() == 1
    with ctx.db.read() as conn:
        assert restated_dates(conn) == []
    assert runner.job_catchup() == 0
    ctx.close()


def test_batch_uses_configured_source(tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    seed_market(ctx.db, n_days=3)
    with ctx.db.write() as conn:
        conn.execute("DELETE FROM backfill_progress")
    seen: dict[str, Any] = {}

    def fake_flow(ad: Any, codes: list[str], day: dt.date, **kw: Any) -> list[Any]:
        seen.update(kw, n=len(codes))
        return []

    monkeypatch.setattr("moneytool.scheduler.jobs.fetch_flow", fake_flow)
    monkeypatch.setattr("moneytool.scheduler.jobs.fetch_bars", lambda *a, **k: [])
    runner = JobRunner(ctx)
    runner.job_backfill_batch(4)
    assert seen["source"] == "sina"
    assert seen["workers"] == 2
    assert seen["n"] == 4
    assert runner.flow_limiter.floor == 1.0
    ctx.close()


def test_sina_health_uses_no_cache(tmp_path: Path) -> None:
    ad, calls = _adapter(tmp_path, json.dumps([ROW]))
    assert ad.health(dt.date(2026, 9, 26))["ok"]
    assert ad.health(dt.date(2026, 9, 26))["ok"]
    assert len(calls) == 2


def test_reset_flow_history_marks_days_for_recompute(tmp_data_dir: Path) -> None:
    from moneytool.ingest.flow import reset_flow_history

    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    dates = seed_market(ctx.db, n_days=3)
    with ctx.db.write() as conn:
        assert reset_flow_history(conn) > 0
        assert conn.execute("SELECT count(*) FROM flow_daily").fetchone() == (0,)
        assert set(restated_dates(conn)) == set(dates)
    ctx.close()
