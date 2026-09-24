"""任务函数（架构 8 节时间表）。每个任务：取写锁 → 干活 → 记 job_run。"""

from __future__ import annotations

import datetime as dt
import threading
import traceback
from collections.abc import Callable
from typing import Any

import duckdb
import polars as pl

from moneytool.adapters.base import AdapterError
from moneytool.app import AppContext, now_sh, today_sh
from moneytool.compute.labels import compute_labels
from moneytool.compute.pipeline import PipelineResult, run
from moneytool.ingest.bars import backfill_bars, daily_bars_from_snapshot, update_float_mv
from moneytool.ingest.flow import (
    CLOSE_SEGMENT,
    backfill_flow,
    capture_segment,
    confirm_from_snapshot,
    rebuild_intraday,
    reconcile_sample,
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
from moneytool.types import QualityStatus

log = get_logger(__name__)


class JobRunner:
    """把「持锁 + 记录 + 兜异常」从任务逻辑里剥出来。"""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.stop_event = threading.Event()

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
        """启动时：最近交易日 confirmed 缺则补跑收盘全量（用日频数据）。"""
        day = today_sh()

        def work(conn: duckdb.DuckDBPyConnection) -> int:
            prev = previous_trading_day(conn, day + dt.timedelta(days=1))
            if prev is None:
                return 0
            last = latest_confirmed_date(conn)
            if last is not None and last >= prev:
                return 0
            has_flow = conn.execute(
                "SELECT count(*) FROM flow_daily WHERE trade_date = ?", [prev]
            ).fetchone()
            if not has_flow or has_flow[0] == 0:
                return 0
            res = run(conn, self.ctx.params_for(prev), prev)
            return res.stage_rows

        self.run_job("startup_check", work)

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
                codes = self._reconcile_sample_codes(conn, day)
                n = reconcile_sample(conn, ad, day, codes)
                try:
                    sync_concepts(conn, ad, day, max_boards=60)
                except AdapterError as exc:
                    record_quality(
                        conn,
                        source="eastmoney",
                        endpoint="concept_list",
                        trade_date=day,
                        status=QualityStatus.MISSING,
                        reason=str(exc),
                    )
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

    def job_backfill_batch(self, batch: int = 50) -> tuple[int, int]:
        day = today_sh()
        ad = self.ctx.adapters
        s = self.ctx.settings.data
        bars_start = day - dt.timedelta(days=365 * s.backfill_years_bars)
        flow_since = day - dt.timedelta(days=365 * s.backfill_years_flow)

        def work(conn: duckdb.DuckDBPyConnection) -> tuple[int, int]:
            b = backfill_bars(
                conn,
                ad,
                start=bars_start,
                end=day,
                day=day,
                limit=batch,
                should_stop=self.stop_event.is_set,
            )
            f = backfill_flow(
                conn, ad, day=day, since=flow_since, limit=batch, should_stop=self.stop_event.is_set
            )
            return b, f

        out = self.run_job("backfill", work)
        return out if isinstance(out, tuple) else (0, 0)

    def backfill_loop(self) -> None:
        """后台循环，直到没有待回补或收到停止。分批释放锁让盘中任务插队。"""
        while not self.stop_event.is_set():
            b, f = self.job_backfill_batch()
            if b == 0 and f == 0:
                log.info("backfill_idle")
                self.stop_event.wait(1800)
            else:
                self.stop_event.wait(1)


def clock_drift_seconds(ad: Any) -> float | None:
    """架构 6 节：与东财服务器时间比较。接口不可用返回 None。"""
    try:
        import httpx  # noqa: PLC0415

        r = httpx.head("https://quote.eastmoney.com/", timeout=5.0)
        server = r.headers.get("date")
        if not server:
            return None
        from email.utils import parsedate_to_datetime  # noqa: PLC0415

        return (dt.datetime.now(dt.UTC) - parsedate_to_datetime(server)).total_seconds()
    except Exception:
        return None
