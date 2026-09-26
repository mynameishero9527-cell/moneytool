"""资金流接入（架构 3.1、11 节第 3 步）。

- 分段：每段末拉「今日」全市场累计（个股 1 次 + 概念 1 次 + 东财行业 1 次），存 flow_snapshot，差分写 flow_intraday。
- 收盘：15:00 后最后一次「今日」累计即当日正式值，写 flow_daily（reconciled=False）；
  夜间对候选名单 / 自选样本用个股日频接口对账，偏差写 data_quality，样本标 reconciled=True。
- 回补：个股日频接口（只有最近约 120 个交易日），小线程池并发、共享自适应限速，进度写 backfill_progress。
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import duckdb
import polars as pl

from moneytool.adapters.base import AdapterError, AdaptiveLimiter, CaptchaError
from moneytool.adapters.registry import Adapters
from moneytool.compute.flow import FLOW_COLS, reconcile, segment_diff
from moneytool.ingest.bars import mark_progress, pending_codes
from moneytool.logging import get_logger
from moneytool.storage.repo import record_quality, upsert
from moneytool.types import QualityStatus, Segment

log = get_logger(__name__)
TASK_FLOW = "flow_daily"
CLOSE_SEGMENT = "close"
EM_SECTOR_TYPES = {"concept": "概念资金流", "em_industry": "行业资金流"}


def _quality(
    conn: duckdb.DuckDBPyConnection, endpoint: str, day: dt.date, segment: str, exc: AdapterError
) -> None:
    status = QualityStatus.CAPTCHA if isinstance(exc, CaptchaError) else QualityStatus.MISSING
    record_quality(
        conn,
        source="eastmoney",
        endpoint=endpoint,
        trade_date=day,
        segment=segment,
        status=status,
        reason=exc.reason,
    )


def capture_segment(
    conn: duckdb.DuckDBPyConnection,
    ad: Adapters,
    day: dt.date,
    segment: str,
    captured_at: dt.datetime,
) -> dict[str, int]:
    """拉一次今日累计并落 flow_snapshot。返回各主体行数；失败的主体写 data_quality 并跳过。"""
    counts: dict[str, int] = {}
    try:
        stock = ad.eastmoney.flow_rank_stock(day, segment)
        snap = stock.select(
            pl.lit("stock").alias("subject_type"),
            pl.col("code").alias("subject_id"),
            pl.lit(day).alias("trade_date"),
            pl.lit(segment).alias("segment"),
            pl.lit(captured_at).alias("captured_at"),
            *[pl.col(c) for c in FLOW_COLS],
            "pct_chg",
            "close",
            _amount_from_ratio(stock),
        )
        counts["stock"] = upsert(conn, "flow_snapshot", snap)
    except AdapterError as exc:
        _quality(conn, "flow_rank_stock", day, segment, exc)
        counts["stock"] = 0

    concept_names = _concept_name_map(conn)
    for kind, sector_type in EM_SECTOR_TYPES.items():
        try:
            sec = ad.eastmoney.flow_rank_sector(day, segment, sector_type)
        except AdapterError as exc:
            _quality(conn, f"flow_rank_sector:{kind}", day, segment, exc)
            counts[kind] = 0
            continue
        if kind == "concept":
            sec = sec.with_columns(
                subject_id=pl.col("name").replace_strict(concept_names, default=None)
            ).filter(pl.col("subject_id").is_not_null())
        else:
            sec = sec.with_columns(subject_id="em_industry:" + pl.col("name"))
        snap = sec.select(
            pl.lit("sector").alias("subject_type"),
            "subject_id",
            pl.lit(day).alias("trade_date"),
            pl.lit(segment).alias("segment"),
            pl.lit(captured_at).alias("captured_at"),
            *[pl.col(c) for c in FLOW_COLS],
            "pct_chg",
            pl.lit(None, dtype=pl.Float64).alias("close"),
        )
        counts[kind] = upsert(conn, "flow_snapshot", snap)
    return counts


def _amount_from_ratio(df: pl.DataFrame) -> pl.Expr:
    """成交额 ≈ 主力净额 / 主力净占比；净占比为 0 或缺失时为 null（不填补）。"""
    if "main_ratio" not in df.columns:
        return pl.lit(None, dtype=pl.Float64).alias("amount")
    return (
        pl.when(pl.col("main_ratio").abs() > 1e-6)
        .then((pl.col("net_main") / pl.col("main_ratio")).abs())
        .otherwise(None)
        .alias("amount")
    )


def _concept_name_map(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    rows = conn.execute("SELECT name, sector_id FROM sector WHERE level = 'concept'").fetchall()
    return {str(n): str(s) for n, s in rows}


def rebuild_intraday(conn: duckdb.DuckDBPyConnection, day: dt.date) -> tuple[int, int]:
    """由当日全部快照重算分段差分，写 flow_intraday / sector_flow_intraday（幂等）。"""
    snaps = conn.execute(
        "SELECT subject_type, subject_id, trade_date, segment, net_main, net_super, net_large, net_medium, net_small "
        "FROM flow_snapshot WHERE trade_date = ? AND segment <> ?",
        [day, CLOSE_SEGMENT],
    ).pl()
    if snaps.is_empty():
        return 0, 0
    stock = segment_diff(snaps.filter(pl.col("subject_type") == "stock"))
    sector = segment_diff(snaps.filter(pl.col("subject_type") == "sector"))
    n1 = upsert(
        conn,
        "flow_intraday",
        stock.select(
            pl.col("subject_id").alias("code"), "trade_date", "segment", *FLOW_COLS, "spans_missing"
        ),
    )
    n2 = upsert(
        conn,
        "sector_flow_intraday",
        sector.select(
            pl.col("subject_id").alias("sector_id"),
            "trade_date",
            "segment",
            *FLOW_COLS,
            "spans_missing",
        ),
    )
    return n1, n2


def confirm_from_snapshot(
    conn: duckdb.DuckDBPyConnection, ad: Adapters, day: dt.date, captured_at: dt.datetime
) -> int:
    """收盘后拉一次「今日」累计作为当日正式值写 flow_daily（reconciled=False）。"""
    counts = capture_segment(conn, ad, day, CLOSE_SEGMENT, captured_at)
    if counts.get("stock", 0) == 0:
        return 0
    snap = conn.execute(
        "SELECT subject_id AS code, trade_date, net_main, net_super, net_large, net_medium, net_small, close, pct_chg, "
        "amount AS snap_amount FROM flow_snapshot WHERE trade_date = ? AND segment = ? AND subject_type = 'stock'",
        [day, CLOSE_SEGMENT],
    ).pl()
    amount = conn.execute("SELECT code, amount FROM bar_daily WHERE trade_date = ?", [day]).pl()
    # 比例分母优先用日线成交额；日线未到时用快照反推值
    df = (
        snap.join(amount, on="code", how="left")
        .with_columns(amount=pl.coalesce("amount", "snap_amount"))
        .drop("snap_amount")
    )
    ratios = [
        pl.when(pl.col("amount") > 0)
        .then(pl.col(c) / pl.col("amount"))
        .otherwise(None)
        .alias(c.replace("net_", "") + "_ratio")
        for c in FLOW_COLS
    ]
    df = df.with_columns(ratios).with_columns(
        reconciled=pl.lit(False), reconcile_diff=pl.lit(None, dtype=pl.Float64)
    )
    n = upsert(conn, "flow_daily", df.drop("amount"))

    sec = conn.execute(
        "SELECT subject_id AS sector_id, trade_date, net_main, net_super, net_large, net_medium, net_small, pct_chg "
        "FROM flow_snapshot WHERE trade_date = ? AND segment = ? AND subject_type = 'sector'",
        [day, CLOSE_SEGMENT],
    ).pl()
    if not sec.is_empty():
        upsert(conn, "sector_flow_daily", sec.with_columns(reconciled=pl.lit(False)))
    return n


def reconcile_sample(
    conn: duckdb.DuckDBPyConnection, ad: Adapters, day: dt.date, codes: list[str]
) -> int:
    """对样本股票用个股日频接口对账：分段之和 vs 日频、快照 vs 日频；偏差写 data_quality。"""
    segments = conn.execute(
        "SELECT code AS subject_id, trade_date, net_main FROM flow_intraday WHERE trade_date = ?",
        [day],
    ).pl()
    n = 0
    for code in codes:
        try:
            hist = ad.eastmoney.flow_daily_stock(code, day)
        except AdapterError as exc:
            _quality(conn, "flow_daily_stock", day, "", exc)
            if isinstance(exc, CaptchaError):
                break
            continue
        today = hist.filter(pl.col("trade_date") == day)
        if today.is_empty():
            record_quality(
                conn,
                source="eastmoney",
                endpoint="flow_daily_stock",
                trade_date=day,
                status=QualityStatus.STALE,
                reason=f"{code} 日频尚无当日行",
            )
            continue
        official = float(today["net_main"][0])
        seg_part = segments.filter(pl.col("subject_id") == code)
        if not seg_part.is_empty():
            r = reconcile(
                seg_part, today.select(pl.lit(code).alias("subject_id"), "trade_date", "net_main")
            )
            diff = float(r["diff_ratio"][0]) if not r.is_empty() else None
        else:
            diff = None
        conn.execute(
            "UPDATE flow_daily SET reconciled = TRUE, reconcile_diff = ? WHERE code = ? AND trade_date = ?",
            [diff, code, day],
        )
        if diff is not None:
            record_quality(
                conn,
                source="eastmoney",
                endpoint="reconcile",
                trade_date=day,
                segment=code,
                status=QualityStatus.RECONCILE,
                reason=f"official={official:.0f}",
                value=diff,
            )
        n += 1
    return n


def backfill_flow(
    conn: duckdb.DuckDBPyConnection,
    ad: Adapters,
    *,
    day: dt.date,
    since: dt.date,
    codes: list[str] | None = None,
    limit: int | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_stall: Callable[[str], None] | None = None,
    max_consecutive_failures: int = 3,
    workers: int = 1,
    limiter: AdaptiveLimiter | None = None,
) -> int:
    """拉个股日频资金流写 flow_daily（reconciled=True，因为就是日频源）。返回完成的股票数。"""
    todo = codes if codes is not None else pending_codes(conn, TASK_FLOW)
    if limit is not None:
        todo = todo[:limit]
    results = fetch_flow(
        ad,
        todo,
        day,
        workers=workers,
        limiter=limiter,
        should_stop=should_stop,
        on_stall=on_stall,
        max_consecutive_failures=max_consecutive_failures,
    )
    return store_flow(conn, results, day, since)


FlowResult = tuple[str, pl.DataFrame | AdapterError]


def fetch_flow(
    ad: Adapters,
    codes: list[str],
    day: dt.date,
    *,
    workers: int = 1,
    limiter: AdaptiveLimiter | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_stall: Callable[[str], None] | None = None,
    max_consecutive_failures: int = 3,
) -> list[FlowResult]:
    """只拉数据不写库（可在写锁外执行）。`workers` 个线程并发，请求节奏由共享的 `limiter` 控制。

    遇验证、或连续 `max_consecutive_failures` 只失败（多为代理 / 网络问题）即停止本轮，
    通过 `on_stall(原因)` 通知调用方暂停，避免每只都走完重试退避、拖住整个回补。"""
    halt = threading.Event()
    state_lock = threading.Lock()
    failures = 0
    stall: list[str] = []

    def one(code: str) -> FlowResult | None:
        nonlocal failures
        if halt.is_set() or (should_stop and should_stop()):
            return None
        try:
            hist = ad.eastmoney.flow_daily_stock(code, day, limiter=limiter)
        except AdapterError as exc:
            if limiter:
                limiter.failed()
            with state_lock:
                failures += 1
                if isinstance(exc, CaptchaError):
                    stall.append(f"触发验证：{exc}")
                    halt.set()
                elif failures >= max_consecutive_failures and not halt.is_set():
                    stall.append(f"连续 {failures} 只失败：{str(exc)[:200]}")
                    halt.set()
            return code, exc
        if limiter:
            limiter.succeeded()
        with state_lock:
            failures = 0
        return code, hist

    with ThreadPoolExecutor(max(1, workers), thread_name_prefix="flow-backfill") as pool:
        fetched = list(pool.map(one, codes))
    if stall and on_stall:
        on_stall(stall[0])
    return [r for r in fetched if r is not None]


def store_flow(
    conn: duckdb.DuckDBPyConnection, results: list[FlowResult], day: dt.date, since: dt.date
) -> int:
    done = 0
    for code, res in results:
        if isinstance(res, AdapterError):
            _quality(conn, "flow_daily_stock", day, "", res)
            if not isinstance(res, CaptchaError):
                mark_progress(conn, TASK_FLOW, code, "failed")
            continue
        hist = res.filter(pl.col("trade_date") >= since)
        if hist.is_empty():
            mark_progress(conn, TASK_FLOW, code, "done", None)
            continue
        df = hist.with_columns(
            code=pl.lit(code), reconciled=pl.lit(True), reconcile_diff=pl.lit(0.0)
        ).select(
            "code",
            "trade_date",
            *FLOW_COLS,
            "main_ratio",
            "super_ratio",
            "large_ratio",
            "medium_ratio",
            "small_ratio",
            "close",
            "pct_chg",
            "reconciled",
            "reconcile_diff",
        )
        upsert(conn, "flow_daily", df)
        mark_progress(conn, TASK_FLOW, code, "done", df["trade_date"].max())  # type: ignore[arg-type]
        done += 1
    return done


def segments_captured(conn: duckdb.DuckDBPyConnection, day: dt.date) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT segment FROM flow_snapshot WHERE trade_date = ? AND subject_type = 'stock'",
        [day],
    ).fetchall()
    order = {s.value: i for i, s in enumerate(Segment)}
    return sorted((str(r[0]) for r in rows), key=lambda s: order.get(s, 99))
