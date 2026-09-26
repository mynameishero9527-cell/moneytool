"""同步进度：速度与剩余时间估算、库内完成数合并、接口与控制台输出。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi.testclient import TestClient

from moneytool.api.server import create_app
from moneytool.app import build_context, init_data_dir
from moneytool.ingest.bars import mark_progress
from moneytool.scheduler.jobs import JobRunner
from moneytool.scheduler.progress import (
    LaneProgress,
    SyncProgress,
    console_line,
    format_duration,
    sync_overview,
)
from tests.compute.test_pipeline import seed_market


def test_lane_rate_and_batch() -> None:
    now = [0.0]
    lane = LaneProgress("flow", clock=lambda: now[0])
    lane.begin(100)
    assert lane.rate_per_minute() is None
    for _ in range(60):
        now[0] += 1.0
        lane.tick()
    snap = lane.snapshot()
    assert snap["state"] == "running" and snap["batch_done"] == 60 and snap["batch_total"] == 100
    assert snap["rate_per_min"] == 60.0
    now[0] += 700  # 超出统计窗口
    assert lane.rate_per_minute() is None
    lane.pause("连续 3 只失败", dt.datetime(2026, 9, 26, 12, 0))
    assert lane.snapshot()["state"] == "paused"
    assert lane.snapshot()["resume_at"] == "2026-09-26T12:00:00"


def test_format_duration() -> None:
    assert format_duration(None) == "估算中"
    assert format_duration(30) == "不到 1 分钟"
    assert format_duration(600) == "10 分钟"
    assert format_duration(3 * 3600 + 20 * 60) == "3 小时 20 分"


def test_overview_merges_db_counts_and_live(tmp_data_dir: Path) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    seed_market(ctx.db, n_days=3)
    now = [0.0]
    prog = SyncProgress(clock=lambda: now[0])
    prog.set_stage("backfill")
    flow = prog.lanes["flow"]
    flow.begin(10)
    for _ in range(30):
        now[0] += 2.0
        flow.tick()
    with ctx.db.write() as conn:
        conn.execute("DELETE FROM backfill_progress")
        codes = [r[0] for r in conn.execute("SELECT code FROM security ORDER BY code").fetchall()]
        for c in codes[:2]:
            mark_progress(conn, "flow_daily", c, "done", dt.date(2026, 9, 24))
        mark_progress(conn, "flow_daily", codes[2], "failed")
        ov = sync_overview(conn, prog.snapshot())
    f = ov["lanes"]["flow"]
    total = len(codes)
    assert f["done"] == 2 and f["failed"] == 1 and f["pending"] == total - 3
    assert f["rate_per_min"] == 30.0
    assert f["eta_seconds"] == int((total - 2) / 30.0 * 60)
    assert ov["stage_label"] == "回补历史数据" and not ov["complete"]
    line = console_line(ov, dt.datetime(2026, 9, 26, 14, 20))
    assert line.startswith("[同步 14:20] 回补历史数据 · 日线 0/")
    assert f"日频资金流 2/{total}" in line and "只/分" in line
    ctx.close()


def test_sync_endpoint(tmp_data_dir: Path) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    seed_market(ctx.db, n_days=3)
    runner = JobRunner(ctx)
    runner.progress.lanes["bars"].begin(5)
    client = TestClient(create_app(ctx.db, ctx=ctx, runner=runner))
    data = client.get("/api/sync").json()["data"]
    assert data["live"] is True
    assert data["lanes"]["bars"]["state"] == "running"
    assert data["lanes"]["bars"]["batch_total"] == 5
    assert set(data["lanes"]) == {"bars", "flow"}
    no_runner = TestClient(create_app(ctx.db, ctx=ctx)).get("/api/sync").json()["data"]
    assert no_runner["live"] is False and no_runner["lanes"]["flow"]["state"] == "unknown"
    ctx.close()


def test_batches_report_per_item_progress(tmp_data_dir: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    seed_market(ctx.db, n_days=3)
    with ctx.db.write() as conn:
        conn.execute("DELETE FROM backfill_progress")
    runner = JobRunner(ctx)
    seen: list[tuple[int, int]] = []

    def fake_bars(ad, codes, **kw):  # type: ignore[no-untyped-def]
        lane = runner.progress.lanes["bars"]
        for c in codes:
            kw["on_item"](c)
            snap = lane.snapshot()
            seen.append((snap["batch_done"], snap["batch_total"]))
        return []

    monkeypatch.setattr("moneytool.scheduler.jobs.fetch_bars", fake_bars)
    runner._backfill_bars_batch(dt.date(2026, 9, 26), 3)
    assert seen == [(1, 3), (2, 3), (3, 3)]
    assert runner.progress.lanes["bars"].snapshot()["batch_total"] == 0
    ctx.close()
