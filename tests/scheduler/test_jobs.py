"""任务壳：持锁、记 job_run、异常落 data_quality；时间表包含全部时点。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest

from moneytool.app import AppContext, build_context, init_data_dir
from moneytool.lock import LockBusyError, write_lock
from moneytool.scheduler.jobs import JobRunner, reference_stale
from moneytool.scheduler.schedule import build_scheduler
from tests.compute.test_pipeline import seed_market


@pytest.fixture
def ctx(tmp_data_dir: Path) -> AppContext:
    init_data_dir(tmp_data_dir)
    c = build_context(tmp_data_dir, log_to_file=False)
    yield c  # type: ignore[misc]
    c.close()


def test_run_job_records_success_and_failure(ctx: AppContext) -> None:
    runner = JobRunner(ctx)

    def ok(conn: duckdb.DuckDBPyConnection) -> int:
        conn.execute("INSERT INTO settings (key, value) VALUES ('x', '1')")
        return 7

    def boom(conn: duckdb.DuckDBPyConnection) -> int:
        raise RuntimeError("接口挂了")

    assert runner.run_job("t_ok", ok) == 7
    assert runner.run_job("t_boom", boom, segment="1030_1130") is None
    with ctx.db.read() as conn:
        runs = conn.execute("SELECT job, ok, rows, error FROM job_run ORDER BY job").fetchall()
        assert runs[0][:3] == ("t_boom", False, None)
        assert "接口挂了" in str(runs[0][3])
        assert runs[1] == ("t_ok", True, 7, None)
        q = conn.execute("SELECT source, endpoint, segment, status FROM data_quality").fetchall()
        assert q == [("scheduler", "t_boom", "1030_1130", "missing")]
    # 锁已释放
    with write_lock(ctx.settings.lock_path, owner="test", timeout=1):
        pass


def test_run_job_waits_for_lock(ctx: AppContext) -> None:
    runner = JobRunner(ctx)
    with (
        write_lock(ctx.settings.lock_path, owner="other", timeout=1),
        pytest.raises(LockBusyError),
        write_lock(ctx.settings.lock_path, owner="me", timeout=0.2),
    ):
        pass
    assert runner.run_job("after", lambda conn: 1) == 1


def test_schedule_has_all_slots(ctx: AppContext) -> None:
    sch = build_scheduler(JobRunner(ctx))
    ids = {j.id for j in sch.get_jobs()}
    assert ids == {
        "reference_sync",
        "premarket_brief",
        "segment_auction",
        "segment_0930_1030",
        "segment_1030_1130",
        "segment_1300_1400",
        "segment_1400_1430",
        "segment_1430_1500",
        "close_confirm",
        "close_confirm_deadline",
        "nightly",
    }
    assert str(sch.timezone) == "Asia/Shanghai"


def _offline(runner: JobRunner, calls: list[str]) -> None:
    runner.job_reference_sync = lambda day=None: calls.append("reference")  # type: ignore[method-assign]
    runner._sync_concepts = lambda conn, day: calls.append("concepts") or 0  # type: ignore[method-assign]


def test_startup_syncs_reference_when_empty(ctx: AppContext) -> None:
    runner = JobRunner(ctx)
    calls: list[str] = []
    _offline(runner, calls)
    with ctx.db.read() as conn:
        assert reference_stale(conn, dt.date(2026, 9, 26))
    runner.job_startup_check()
    assert calls == ["reference", "concepts"]
    with ctx.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM job_run WHERE job = 'catchup'").fetchone() == (0,)


def test_catchup_computes_missing_days_in_order(ctx: AppContext) -> None:
    dates = seed_market(ctx.db, n_days=70, n_sectors=3, per_sector=10)
    ctx.settings.schedule.catchup_days = 3
    runner = JobRunner(ctx)
    with ctx.db.read() as conn:
        assert runner.catchup_days(conn, dates[-1] + dt.timedelta(days=1)) == dates[-3:]
    with ctx.db.write() as conn:
        conn.execute(
            "DELETE FROM bar_daily WHERE trade_date = ? AND code > '000005.SZ'", [dates[-2]]
        )
    with ctx.db.write() as conn:
        conn.execute(
            "DELETE FROM flow_daily WHERE trade_date = ? AND code > '000005.SZ'", [dates[-1]]
        )
    with ctx.db.read() as conn:
        assert runner.catchup_days(conn, dates[-1] + dt.timedelta(days=1)) == dates[-3:-2]
    with ctx.db.write() as conn:
        conn.execute("DELETE FROM flow_daily WHERE trade_date = ?", [dates[-1]])
    assert runner.job_catchup() == 2
    with ctx.db.read() as conn:
        got = conn.execute(
            "SELECT trade_date FROM market_daily WHERE segment = 'close' ORDER BY trade_date"
        ).fetchall()
        assert [r[0] for r in got] == [dates[-3], dates[-1]]
        brief = conn.execute("SELECT trade_date FROM brief WHERE kind = 'close'").fetchall()
        assert brief == [(dates[-1],)]
    # 已算过的不重复算
    assert runner.job_catchup() == 0
