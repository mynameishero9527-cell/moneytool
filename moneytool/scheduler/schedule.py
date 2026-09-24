"""APScheduler 时间表（架构 8 节）。非交易日由任务内部判断后跳过。"""

from __future__ import annotations

import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from moneytool.app import TZ
from moneytool.scheduler.jobs import JobRunner
from moneytool.types import INTRADAY_SEGMENTS, Segment

SEGMENT_TIMES: dict[Segment, tuple[int, int]] = {
    Segment.S1: (10, 31),
    Segment.S2: (11, 31),
    Segment.S3: (14, 1),
    Segment.S4: (14, 31),
    Segment.S5: (15, 1),
}
AUCTION_TIME = (9, 26)


def build_scheduler(runner: JobRunner) -> BackgroundScheduler:
    sch = BackgroundScheduler(timezone=TZ, job_defaults={"coalesce": True, "max_instances": 1})
    weekdays = "mon-fri"
    sch.add_job(
        runner.job_reference_sync,
        CronTrigger(day_of_week=weekdays, hour=8, minute=25, timezone=TZ),
        id="reference_sync",
        misfire_grace_time=3600,
    )
    sch.add_job(
        lambda: runner.job_segment(Segment.AUCTION.value),
        CronTrigger(
            day_of_week=weekdays, hour=AUCTION_TIME[0], minute=AUCTION_TIME[1], timezone=TZ
        ),
        id="segment_auction",
        misfire_grace_time=300,
    )
    for seg in INTRADAY_SEGMENTS:
        h, m = SEGMENT_TIMES[seg]
        sch.add_job(
            runner.job_segment,
            CronTrigger(day_of_week=weekdays, hour=h, minute=m, timezone=TZ),
            args=[seg.value],
            id=f"segment_{seg.value}",
            misfire_grace_time=300,
        )
    # 15:30 起每 30 分钟探测到 20:00
    sch.add_job(
        runner.job_close_confirm,
        CronTrigger(day_of_week=weekdays, hour="15-19", minute="0,30", timezone=TZ),
        id="close_confirm",
        misfire_grace_time=6 * 3600,
    )
    sch.add_job(
        lambda: runner.job_close_confirm(force=True),
        CronTrigger(day_of_week=weekdays, hour=20, minute=0, timezone=TZ),
        id="close_confirm_deadline",
        misfire_grace_time=6 * 3600,
    )
    sch.add_job(
        runner.job_nightly,
        CronTrigger(day_of_week=weekdays, hour=20, minute=30, timezone=TZ),
        id="nightly",
        misfire_grace_time=3 * 3600,
    )
    return sch


def start_backfill_thread(runner: JobRunner) -> threading.Thread:
    t = threading.Thread(target=runner.backfill_loop, name="backfill", daemon=True)
    t.start()
    return t
