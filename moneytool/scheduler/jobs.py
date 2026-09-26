"""任务函数（架构 8 节时间表）。每个任务：取写锁 → 干活 → 记 job_run。"""

from __future__ import annotations

import datetime as dt
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import duckdb
import polars as pl

from moneytool.adapters.base import AdapterError, AdaptiveLimiter
from moneytool.app import AppContext, now_sh, today_sh
from moneytool.compute.labels import compute_labels
from moneytool.compute.pipeline import PipelineResult, run
from moneytool.ingest.bars import TASK as BARS_TASK
from moneytool.ingest.bars import (
    backfill_bars,
    daily_bars_from_snapshot,
    fetch_bars,
    pending_codes,
    store_bars,
    update_float_mv,
)
from moneytool.ingest.bars_pool import BarsPool
from moneytool.ingest.flow import (
    CLOSE_SEGMENT,
    TASK_FLOW,
    capture_segment,
    clear_restated,
    confirm_from_snapshot,
    fetch_flow,
    flow_last_dates,
    pending_flow_codes,
    rebuild_intraday,
    reconcile_sample,
    restated_dates,
    store_flow,
)
from moneytool.ingest.reference import (
    sync_calendar,
    sync_concepts,
    sync_index_members,
    sync_securities,
    sync_shenwan,
)
from moneytool.lock import write_lock
from moneytool.logging import get_logger
from moneytool.notify import dispatch, pending_quiet, push
from moneytool.report import close_brief, premarket_brief, save_brief
from moneytool.scheduler.progress import SyncProgress, console_line, sync_overview
from moneytool.storage.maintenance import backup_database, prune_intraday
from moneytool.storage.repo import (
    is_trading_day,
    latest_confirmed_date,
    previous_trading_day,
    record_quality,
    setting_get,
    setting_set,
    upsert,
)
from moneytool.types import DataStatus, QualityStatus

log = get_logger(__name__)
FLOW_PAUSE_MINUTES = 30
FLOW_PAUSE_MAX_MINUTES = 240
FLOW_READY_HOUR = 18  # 新浪日频资金流当日数据可用的大致时刻（北京时间）
FLOW_REFRESH_HOURS = 6
LANE_POLL_SECONDS = 60
LANE_IDLE_SECONDS = 600  # 一路没有待办后隔多久再查（资金流每日增量靠它触发）
CATCHUP_EVERY_SECONDS = 1800  # 同一只股票两次增量尝试的最小间隔，源端未更新时不反复请求


class JobRunner:
    """把「持锁 + 记录 + 兜异常」从任务逻辑里剥出来。"""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.stop_event = threading.Event()
        self.flow_paused_until: dt.datetime | None = None
        self.flow_pause_streak = 0
        self.flow_pause_reason = ""
        self.progress = SyncProgress()
        cfg = ctx.settings.backfill
        self.flow_source = ctx.settings.data.flow_source
        self.flow_workers, interval = cfg.flow_pace(self.flow_source)
        self.flow_limiter = AdaptiveLimiter(interval, cfg.flow_max_interval_seconds)
        self.bars_pool = (
            BarsPool(ctx.settings.data_dir, cfg.bars_workers) if cfg.bars_workers > 1 else None
        )

    def run_job(
        self, name: str, fn: Callable[[duckdb.DuckDBPyConnection], Any], *, segment: str = ""
    ) -> Any:
        day = today_sh()
        started = now_sh()
        with write_lock(self.ctx.settings.lock_path, owner=name), self.ctx.db.write() as conn:
            try:
                out = fn(conn)
                rows = out if isinstance(out, int) else None
                self._record(conn, name, day, segment, started, True, rows, None)
                return out
            except Exception as exc:
                log.error("job_failed", job=name, error=str(exc), tb=traceback.format_exc())
                self._record(conn, name, day, segment, started, False, None, str(exc)[:500])
                record_quality(
                    conn,
                    source="scheduler",
                    endpoint=name,
                    trade_date=day,
                    segment=segment,
                    status=QualityStatus.MISSING,
                    reason=str(exc)[:500],
                )
                return None

    @staticmethod
    def _record(
        conn: duckdb.DuckDBPyConnection,
        job: str,
        day: dt.date,
        segment: str,
        started: dt.datetime,
        ok: bool,
        rows: int | None,
        error: str | None,
    ) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO job_run (job, trade_date, segment, started_at, finished_at, ok, rows, error) "
            "VALUES (?, ?, ?, ?, now(), ?, ?, ?)",
            [job, day, segment, started, ok, rows, error],
        )

    # ---- 08:00 自检 / 08:25 参考数据 ----

    def job_reference_sync(self, day: dt.date | None = None) -> None:
        day = day or today_sh()
        ad = self.ctx.adapters

        def work(conn: duckdb.DuckDBPyConnection) -> int:
            n = sync_calendar(
                conn, ad, day - dt.timedelta(days=3700), day + dt.timedelta(days=400), day
            )
            try:
                sync_securities(conn, ad, day)
            except AdapterError as exc:
                record_quality(
                    conn,
                    source="baostock",
                    endpoint="stock_basic",
                    trade_date=day,
                    status=QualityStatus.MISSING,
                    reason=str(exc),
                )
            try:
                sync_shenwan(conn, ad, day)
            except AdapterError as exc:
                record_quality(
                    conn,
                    source="shenwan",
                    endpoint="list",
                    trade_date=day,
                    status=QualityStatus.MISSING,
                    reason=str(exc),
                )
            sync_index_members(conn, ad, day)
            return n

        self.run_job("reference_sync", work)

    def job_startup_check(self) -> None:
        """启动时：参考数据缺失（首次运行、或周末 / 节假日启动错过 08:25）先同步，再补算缺失的收盘结果。"""
        day = today_sh()
        with self.ctx.db.read() as conn:
            stale = reference_stale(conn, day)
        if stale:
            self.progress.set_stage("reference")
            self.job_reference_sync(day)
        self.job_catchup()
        self.progress.set_stage("backfill")

    def _concepts_if_empty(self) -> None:
        """概念成分走东财、逐板块拉，放在回补空闲时做，不挡住启动后的日线回补。"""
        day = today_sh()
        with self.ctx.db.read() as conn:
            has = conn.execute("SELECT 1 FROM sector WHERE level = 'concept' LIMIT 1").fetchone()
        if not has:
            self.run_job("concept_sync", lambda conn: self._sync_concepts(conn, day))

    def _sync_concepts(self, conn: duckdb.DuckDBPyConnection, day: dt.date) -> int:
        try:
            return sync_concepts(conn, self.ctx.adapters, day, max_boards=60)[0]
        except AdapterError as exc:
            record_quality(
                conn,
                source="eastmoney",
                endpoint="concept_list",
                trade_date=day,
                status=QualityStatus.MISSING,
                reason=str(exc),
            )
            return 0

    def catchup_days(self, conn: duckdb.DuckDBPyConnection, day: dt.date) -> list[dt.date]:
        """最近 N 个已收盘交易日中：当日参数版本下无 confirmed 结果、且日线覆盖足够的日期（升序）。"""
        s = self.ctx.settings.schedule
        rows = conn.execute(
            "SELECT trade_date FROM trade_calendar WHERE is_open AND trade_date < ? "
            "ORDER BY trade_date DESC LIMIT ?",
            [day, s.catchup_days],
        ).fetchall()
        total = conn.execute("SELECT count(*) FROM security WHERE NOT is_delisting").fetchone()
        if not rows or not total or total[0] == 0:
            return []
        out: list[dt.date] = []
        need = s.catchup_min_coverage * total[0]
        restated = set(restated_dates(conn))
        for (d,) in sorted(rows):
            cov = conn.execute(
                "SELECT (SELECT count(*) FROM bar_daily WHERE trade_date = ?), "
                "(SELECT count(*) FROM flow_daily WHERE trade_date = ?)",
                [d, d],
            ).fetchone()
            if cov is None:
                continue
            prev = conn.execute(
                "SELECT data_status FROM market_daily WHERE trade_date = ? AND segment = 'close' "
                "AND param_version = ? LIMIT 1",
                [d, self.ctx.params_for(d).version],
            ).fetchone()
            if prev is not None:
                # 先前资金流全缺按价格模式算过、或当时用的是收盘快照近似值，后来有了日频正式值则重算
                if cov[1] >= need and (prev[0] == DataStatus.DEGRADED.value or d in restated):
                    out.append(d)
                continue
            # 资金流部分到位时板块合计会失真，等回补完；全缺则走价格模式（结果标降级）
            if cov[0] >= need and (cov[1] == 0 or cov[1] >= need):
                out.append(d)
        return out

    def job_catchup(self) -> int:
        """按日期顺序补算，每天单独持锁，盘中任务可插队。补算完为最后一天生成收盘简报。"""
        day = today_sh()
        with self.ctx.db.read() as conn:
            days = self.catchup_days(conn, day)
        done: list[dt.date] = []

        def compute(d: dt.date) -> Callable[[duckdb.DuckDBPyConnection], PipelineResult]:
            return lambda conn: run(conn, self.ctx.params_for(d), d)

        prev_stage = self.progress.begin_catchup(len(days)) if days else None
        for d in days:
            if self.stop_event.is_set():
                break
            res = self.run_job("catchup", compute(d))
            self.progress.catchup_tick()
            if res is not None:
                done.append(d)
        if prev_stage is not None:
            self.progress.set_stage(prev_stage)
        if done:

            def forget(conn: duckdb.DuckDBPyConnection) -> int:
                clear_restated(conn, done)
                return len(done)

            self.run_job("catchup_restated", forget)
        if done:
            last = done[-1]
            version = self.ctx.params_for(last).version
            self.run_job(
                "catchup_brief",
                lambda conn: save_brief(
                    conn,
                    last,
                    "close",
                    close_brief(conn, last, version),
                    self.ctx.settings.briefs_dir,
                ),
            )
            log.info("catchup_done", days=len(done), first=str(done[0]), last=str(last))
        return len(done)

    # ---- 盘中分段 ----

    def job_segment(self, segment: str) -> PipelineResult | None:
        day = today_sh()
        ad = self.ctx.adapters

        def work(conn: duckdb.DuckDBPyConnection) -> PipelineResult | None:
            if not is_trading_day(conn, day):
                return None
            counts = capture_segment(conn, ad, day, segment, now_sh())
            if counts.get("stock", 0) == 0:
                return None
            rebuild_intraday(conn, day)
            p = self.ctx.params_for(day)
            res = run(conn, p, day, segment=segment)
            self._alerts(conn, day, segment, p.version)
            return res

        out = self.run_job("segment", work, segment=segment)
        return out if isinstance(out, PipelineResult) else None

    # ---- 收盘确认（15:30 起探测） ----

    def job_close_confirm(self, force: bool = False) -> PipelineResult | None:
        day = today_sh()
        ad = self.ctx.adapters

        def work(conn: duckdb.DuckDBPyConnection) -> PipelineResult | None:
            if not is_trading_day(conn, day):
                return None
            if setting_get(conn, f"confirmed:{day.isoformat()}") == "1" and not force:
                return None
            # 1) 当日日线：Baostock 日线到位则全量回补当日，否则用东财收盘快照生成近似日线
            bars_ready = self._bars_ready(conn, day)
            if not bars_ready:
                deadline = now_sh().time() >= dt.time.fromisoformat(
                    self.ctx.settings.schedule.confirm_deadline
                )
                if not deadline and not force:
                    return None
            # 2) 当日正式资金流 = 收盘后最后一次「今日」累计；同时拉全 A 快照写流通市值与换手
            spot = self._spot(conn, day)
            n = confirm_from_snapshot(conn, ad, day, now_sh())
            if n == 0:
                record_quality(
                    conn,
                    source="eastmoney",
                    endpoint="flow_rank_stock",
                    trade_date=day,
                    segment=CLOSE_SEGMENT,
                    status=QualityStatus.MISSING,
                    reason="收盘累计拉取失败",
                )
            if not bars_ready:
                self._bars_from_snapshot(conn, day, spot)
            if spot is not None:
                update_float_mv(conn, spot, day)
            # 3) 计算与存档 → 提醒 → 收盘简报
            p = self.ctx.params_for(day)
            res = run(conn, p, day)
            setting_set(conn, f"confirmed:{day.isoformat()}", "1")
            self._alerts(conn, day, None, p.version)
            save_brief(
                conn, day, "close", close_brief(conn, day, p.version), self.ctx.settings.briefs_dir
            )
            return res

        out = self.run_job("close_confirm", work)
        return out if isinstance(out, PipelineResult) else None

    def _bars_ready(self, conn: duckdb.DuckDBPyConnection, day: dt.date) -> bool:
        """探测 Baostock 当日日线是否到位（拉 1 只样本）；到位则全量拉当日。"""
        sample = conn.execute(
            "SELECT code FROM security WHERE NOT is_delisting ORDER BY code LIMIT 1"
        ).fetchone()
        if sample is None:
            return False
        try:
            k = self.ctx.adapters.baostock.kdata(sample[0], day, day, day)
        except AdapterError:
            return False
        if k.is_empty():
            return False
        codes = [
            r[0]
            for r in conn.execute("SELECT code FROM security WHERE NOT is_delisting").fetchall()
        ]
        backfill_bars(
            conn,
            self.ctx.adapters,
            start=day,
            end=day,
            day=day,
            codes=codes,
            should_stop=self.stop_event.is_set,
        )
        return True

    def _spot(self, conn: duckdb.DuckDBPyConnection, day: dt.date) -> pl.DataFrame | None:
        try:
            return self.ctx.adapters.eastmoney.spot(day)
        except AdapterError as exc:
            record_quality(
                conn,
                source="eastmoney",
                endpoint="spot",
                trade_date=day,
                status=QualityStatus.MISSING,
                reason=str(exc)[:300],
            )
            return None

    def _bars_from_snapshot(
        self, conn: duckdb.DuckDBPyConnection, day: dt.date, spot: pl.DataFrame | None
    ) -> None:
        """日线未到：优先用全 A 快照（成交额、换手、流通市值齐全），否则用资金流快照反推成交额。"""
        snap = conn.execute(
            "SELECT s.subject_id AS code, sec.name, s.close, s.pct_chg, s.amount, "
            "NULL::DOUBLE AS turnover, NULL::DOUBLE AS float_mv "
            "FROM flow_snapshot s LEFT JOIN security sec ON sec.code = s.subject_id "
            "WHERE s.trade_date = ? AND s.segment = ? AND s.subject_type = 'stock'",
            [day, CLOSE_SEGMENT],
        ).pl()
        if spot is not None and not spot.is_empty():
            snap = spot.select("code", "name", "close", "pct_chg", "amount", "turnover", "float_mv")
        if snap.is_empty():
            return
        prev_close = conn.execute(
            "SELECT code, close AS pre_close FROM bar_daily WHERE trade_date = (SELECT max(trade_date) FROM bar_daily WHERE trade_date < ?)",
            [day],
        ).pl()
        bars = daily_bars_from_snapshot(
            snap.with_columns(pl.col("name").fill_null("")), day, prev_close
        )
        upsert(conn, "bar_daily", bars)
        record_quality(
            conn,
            source="baostock",
            endpoint="kdata",
            trade_date=day,
            status=QualityStatus.STALE,
            reason="当日日线未到，暂用东财收盘快照近似（次日自检覆盖）",
        )

    # ---- 夜间 ----

    def job_nightly(self) -> None:
        day = today_sh()
        ad = self.ctx.adapters

        def work(conn: duckdb.DuckDBPyConnection) -> int:
            n = 0
            if is_trading_day(conn, day):
                if self.flow_source == "eastmoney":  # 新浪来源的日频正式值由回补线程每日增量拉取
                    codes = self._reconcile_sample_codes(conn, day)
                    n = reconcile_sample(conn, ad, day, codes)
                self._sync_concepts(conn, day)
            confirmed = latest_confirmed_date(conn)
            if confirmed is not None:
                labels = compute_labels(conn, confirmed, self.ctx.params_for(confirmed))
                log.info("labels_done", rows=labels)
            s = self.ctx.settings
            removed = ad.eastmoney.ctx.cache.prune(s.data.raw_retention_days)
            pruned = prune_intraday(conn, day, s.data.intraday_retention_days)
            backup = backup_database(conn, s.db_path, s.backups_dir, day, s.data.backup_keep)
            log.info("nightly_maintenance", raw_files=removed, intraday=pruned, backup=str(backup))
            return n

        self.run_job("nightly", work)

    # ---- 08:30 盘前简报 ----

    def job_premarket_brief(self) -> None:
        day = today_sh()

        def work(conn: duckdb.DuckDBPyConnection) -> int:
            if not is_trading_day(conn, day):
                return 0
            p = self.ctx.params_for(day)
            since = previous_trading_day(conn, day) or day
            quiet = pending_quiet(conn, since)
            md = premarket_brief(conn, day, p.version, quiet)
            save_brief(conn, day, "premarket", md, self.ctx.settings.briefs_dir)
            conn.execute(
                "UPDATE alert_log SET pushed_at = now() WHERE pushed_at IS NULL AND trade_date >= ?",
                [since],
            )
            push(self.ctx.settings.notify, [md.splitlines()[0], *quiet[:10]])
            return len(quiet)

        self.run_job("premarket_brief", work)

    def _alerts(
        self, conn: duckdb.DuckDBPyConnection, day: dt.date, segment: str | None, version: str
    ) -> None:
        try:
            dispatch(
                conn,
                day,
                segment,
                version,
                self.ctx.settings.notify,
                now_sh(),
                is_trading_day(conn, day),
            )
        except Exception as exc:
            log.error("alerts_failed", error=str(exc))

    @staticmethod
    def _reconcile_sample_codes(
        conn: duckdb.DuckDBPyConnection, day: dt.date, n: int = 20
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT code FROM (
                SELECT code FROM watchlist
                UNION SELECT code FROM action_list_confirmed WHERE trade_date = ?
                UNION SELECT code FROM flow_daily WHERE trade_date = ? ORDER BY random() LIMIT ?
            ) LIMIT ?
            """,
            [day, day, n, n],
        ).fetchall()
        return [r[0] for r in rows]

    # ---- 回补（后台线程，分批持锁） ----

    def job_backfill_batch(self, batch: int | None = None) -> tuple[int, int]:
        """日线与资金流各拉一批（命令行 backfill 用；后台由 backfill_loop 的两路循环分别调度）。
        网络请求都在写锁外进行，拉完一批才持锁写库，盘中任务不会被回补挡住。"""
        day = today_sh()
        n = batch or self.ctx.settings.backfill.batch
        with ThreadPoolExecutor(2, thread_name_prefix="backfill") as pool:
            bars = pool.submit(self._backfill_bars_batch, day, n)
            flow = None if self.flow_paused() else pool.submit(self._backfill_flow_batch, day, n)
            return bars.result(), (flow.result() if flow else 0)

    def _pending(self, task: str, n: int) -> list[str]:
        with self.ctx.db.read() as conn:
            return pending_codes(conn, task)[:n]

    def _backfill_bars_batch(self, day: dt.date, n: int) -> int:
        todo = self._pending(BARS_TASK, n)
        if not todo:
            return 0
        lane = self.progress.lanes["bars"]
        lane.begin(len(todo))
        years = self.ctx.settings.data.backfill_years_bars
        start = day - dt.timedelta(days=365 * years)
        try:
            if self.bars_pool is not None:
                results = self.bars_pool.fetch(
                    todo,
                    start=start,
                    end=day,
                    day=day,
                    should_stop=self.stop_event.is_set,
                    on_item=lane.tick,
                )
            else:
                results = fetch_bars(
                    self.ctx.adapters,
                    todo,
                    start=start,
                    end=day,
                    day=day,
                    should_stop=self.stop_event.is_set,
                    on_item=lane.tick,
                )
        except Exception as exc:
            log.error("backfill_fetch_failed", task=BARS_TASK, error=str(exc))
            lane.end()
            return 0
        out = self.run_job("backfill", lambda conn: store_bars(conn, results, day))
        lane.end()
        return out if isinstance(out, int) else 0

    def _flow_target(self, conn: duckdb.DuckDBPyConnection) -> dt.date | None:
        """新浪来源每日增量要补到的交易日：收盘数据约 18 点后才齐，之前以上一交易日为准。"""
        if self.flow_source != "sina":
            return None
        now = now_sh()
        if is_trading_day(conn, now.date()) and now.hour >= FLOW_READY_HOUR:
            return now.date()
        return previous_trading_day(conn, now.date())

    def _backfill_flow_batch(self, day: dt.date, n: int) -> int:
        with self.ctx.db.read() as conn:
            target = self._flow_target(conn)
            refresh_before = now_sh() - dt.timedelta(hours=FLOW_REFRESH_HOURS)
            todo = pending_flow_codes(conn, target, refresh_before)[:n]
            last = flow_last_dates(conn, todo)
        if not todo:
            return 0
        lane = self.progress.lanes["flow"]
        lane.begin(len(todo))
        since = day - dt.timedelta(days=365 * self.ctx.settings.data.backfill_years_flow)
        try:
            results = fetch_flow(
                self.ctx.adapters,
                todo,
                day,
                source=self.flow_source,
                since=last,
                workers=self.flow_workers,
                limiter=self.flow_limiter,
                should_stop=self.stop_event.is_set,
                on_stall=self._pause_flow,
                on_item=lane.tick,
            )
        except Exception as exc:
            log.error("backfill_fetch_failed", task=TASK_FLOW, error=str(exc))
            lane.end()
            return 0
        if any(not isinstance(r, AdapterError) for _, r in results):
            self.flow_pause_streak = 0
        out = self.run_job("backfill_flow", lambda conn: store_flow(conn, results, day, since))
        lane.end()
        return out if isinstance(out, int) else 0

    def flow_paused(self) -> bool:
        return self.flow_paused_until is not None and now_sh() < self.flow_paused_until

    def _pause_flow(self, reason: str) -> None:
        """东财封 IP 通常持续几十分钟到数小时：连续暂停时时长翻倍（30 → 60 → 120 … 最长 4 小时），
        期间一个请求都不发，等封禁自然解除；恢复后有成功请求即重置。"""
        minutes = min(FLOW_PAUSE_MINUTES * 2**self.flow_pause_streak, FLOW_PAUSE_MAX_MINUTES)
        self.flow_pause_streak += 1
        self.flow_paused_until = now_sh() + dt.timedelta(minutes=minutes)
        self.flow_pause_reason = reason
        log.warning(
            "backfill_flow_paused",
            reason=reason,
            minutes=minutes,
            until=self.flow_paused_until.isoformat(timespec="minutes"),
        )

    def backfill_loop(self) -> None:
        """后台回补：先做启动自检（参考数据 → 补算），再让日线与资金流各跑一个循环，
        按各自数据源的节奏分批拉取，互不等待（一路被限流或暂停不拖慢另一路）。
        两路都没有新进展即视为回补完成，随即同步概念成分（首次）并补算缺失的收盘结果；
        资金流暂停期间不补算，免得整段历史都落成价格模式。"""
        self.job_startup_check()
        lanes = {
            "bars": BackfillLane("bars", self._backfill_bars_batch),
            "flow": BackfillLane("flow", self._backfill_flow_batch, paused=self.flow_paused),
        }
        reporter = ProgressReporter(self)
        threads = [
            threading.Thread(
                target=self._lane_loop, args=(lane,), name=f"backfill-{lane.name}", daemon=True
            )
            for lane in lanes.values()
        ]
        for t in threads:
            t.start()
        last_catchup: float | None = None
        while not self.stop_event.is_set():
            idle = all(lane.idle.is_set() for lane in lanes.values())
            if idle and not self.flow_paused():
                progressed = sum(lane.take_progress() for lane in lanes.values()) > 0
                due = (
                    last_catchup is None or time.monotonic() - last_catchup >= CATCHUP_EVERY_SECONDS
                )
                if progressed or due:
                    log.info("backfill_idle")
                    self._concepts_if_empty()
                    self.job_catchup()
                    last_catchup = time.monotonic()
                self.progress.set_stage("ready")
            elif self.progress.stage == "ready":
                self.progress.set_stage("backfill")
            reporter.maybe_print()
            self.stop_event.wait(LANE_POLL_SECONDS)
        for t in threads:
            t.join(timeout=5)
        self.close()

    def close(self) -> None:
        if self.bars_pool is not None:
            self.bars_pool.close()

    def _lane_loop(self, lane: BackfillLane) -> None:
        size = self.ctx.settings.backfill.batch
        while not self.stop_event.is_set():
            live = self.progress.lanes[lane.name]
            if lane.paused():
                lane.idle.clear()
                live.pause(self.flow_pause_reason, self.flow_paused_until)
                self.stop_event.wait(LANE_POLL_SECONDS)
                continue
            try:
                n = lane.batch(today_sh(), size)
            except Exception as exc:
                log.error("backfill_lane_failed", lane=lane.name, error=str(exc))
                n = 0
            if n > 0:
                lane.record(n)
                self.stop_event.wait(1)
            else:
                lane.idle.set()
                live.idle()
                self.stop_event.wait(LANE_IDLE_SECONDS)


class ProgressReporter:
    """回补期间每分钟在控制台（start.bat 窗口）打印一行进度；全部补完时打印一次“已完成”。"""

    def __init__(self, runner: JobRunner, every: float = 60.0) -> None:
        self.runner = runner
        self.every = every
        self._last = 0.0
        self._ready_printed = False

    def maybe_print(self) -> None:
        now = time.monotonic()
        ready = self.runner.progress.stage == "ready"
        if ready and self._ready_printed:
            return
        if not ready and now - self._last < self.every:
            return
        try:
            with self.runner.ctx.db.read() as conn:
                ov = sync_overview(conn, self.runner.progress.snapshot())
        except Exception as exc:
            log.warning("progress_print_failed", error=str(exc))
            return
        self._last = now
        self._ready_printed = ready
        print(console_line(ov, now_sh()), flush=True)


class BackfillLane:
    """一路回补（日线或资金流）的状态：是否已无待办、自上次补算以来是否有新进展。"""

    def __init__(
        self,
        name: str,
        batch: Callable[[dt.date, int], int],
        paused: Callable[[], bool] | None = None,
    ) -> None:
        self.name = name
        self.batch = batch
        self.paused = paused or (lambda: False)
        self.idle = threading.Event()
        self._lock = threading.Lock()
        self._progress = 0

    def record(self, n: int) -> None:
        with self._lock:
            self._progress += n
        self.idle.clear()

    def take_progress(self) -> int:
        with self._lock:
            n, self._progress = self._progress, 0
        return n


def reference_stale(conn: duckdb.DuckDBPyConnection, day: dt.date) -> bool:
    """证券表为空或交易日历未覆盖今天：需要立即同步参考数据。"""
    sec = conn.execute("SELECT count(*) FROM security").fetchone()
    cal = conn.execute("SELECT max(trade_date) FROM trade_calendar").fetchone()
    return not sec or sec[0] == 0 or not cal or cal[0] is None or cal[0] < day


def clock_drift_seconds(ad: Any) -> float | None:
    """架构 6 节：与东财服务器时间比较。接口不可用返回 None。"""
    try:
        import httpx  # noqa: PLC0415

        started = dt.datetime.now(dt.UTC)
        r = httpx.head("https://quote.eastmoney.com/", timeout=5.0)
        local = started + (dt.datetime.now(dt.UTC) - started) / 2
        server = r.headers.get("date")
        if not server:
            return None
        from email.utils import parsedate_to_datetime  # noqa: PLC0415

        # 该页走 CDN 缓存，Date 是源站生成时间；加上 Age（在缓存中停留的秒数）才是当前服务器时间
        age = float(r.headers.get("age") or 0)
        return (local - parsedate_to_datetime(server)).total_seconds() - age
    except Exception:
        return None
