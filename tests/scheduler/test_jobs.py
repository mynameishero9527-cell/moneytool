"""任务壳：持锁、记 job_run、异常落 data_quality；时间表包含全部时点。"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from moneytool.app import AppContext, build_context, init_data_dir
from moneytool.lock import LockBusyError, write_lock
from moneytool.scheduler.jobs import JobRunner
from moneytool.scheduler.schedule import build_scheduler


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


def test_startup_check_noop_without_data(ctx: AppContext) -> None:
    runner = JobRunner(ctx)
    runner.job_startup_check()
    with ctx.db.read() as conn:
        assert conn.execute("SELECT ok FROM job_run WHERE job = 'startup_check'").fetchone() == (
            True,
        )
