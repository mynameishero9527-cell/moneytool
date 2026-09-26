"""日线与复权回补（Baostock），可中断续跑；进度写 backfill_progress。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import duckdb
import polars as pl

from moneytool.adapters.base import AdapterError, SourceBlockedError
from moneytool.adapters.registry import Adapters
from moneytool.logging import get_logger
from moneytool.storage.repo import record_quality, upsert
from moneytool.types import QualityStatus

log = get_logger(__name__)
TASK = "bars"


def _limit_prices(pre_close: pl.Expr, code: pl.Expr, is_st: pl.Expr) -> tuple[pl.Expr, pl.Expr]:
    """涨跌停价：主板 10%、创业板 / 科创板 20%、北交所 30%、ST 5%；四舍五入到分。"""
    ratio = (
        pl.when(code.str.ends_with(".BJ"))
        .then(0.30)
        .when(code.str.starts_with("30") | code.str.starts_with("688"))
        .then(0.20)
        .when(is_st)
        .then(0.05)
        .otherwise(0.10)
    )
    return (pre_close * (1 + ratio)).round(2), (pre_close * (1 - ratio)).round(2)


def bars_with_adjust(kdata: pl.DataFrame, adjust: pl.DataFrame) -> pl.DataFrame:
    """把复权因子按生效日前向填充到日线；无因子时为 1.0。输出 bar_daily 列。"""
    df = kdata.sort("trade_date")
    if adjust.is_empty():
        df = df.with_columns(adj_factor=pl.lit(1.0))
    else:
        adj = adjust.sort("effective_date").select(
            pl.col("effective_date").alias("trade_date"), pl.col("adj_factor").alias("_adj")
        )
        df = df.join_asof(adj, on="trade_date", strategy="backward").with_columns(
            adj_factor=pl.col("_adj").fill_null(1.0)
        )
        df = df.drop("_adj")
    up, down = _limit_prices(pl.col("pre_close"), pl.col("code"), pl.col("is_st"))
    return df.with_columns(limit_up=up, limit_down=down).select(
        "code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "volume",
        "amount",
        "turnover",
        "pct_chg",
        "adj_factor",
        "limit_up",
        "limit_down",
        pl.lit(None, dtype=pl.Float64).alias("float_mv"),
        "is_suspended",
    )


def pending_codes(conn: duckdb.DuckDBPyConnection, task: str) -> list[str]:
    """未完成的标的；失败过的排在后面，免得少数一直失败的股票占满每一批。"""
    rows = conn.execute(
        "SELECT s.code FROM security s LEFT JOIN backfill_progress p ON p.task = ? AND p.subject_id = s.code "
        "WHERE (p.status IS NULL OR p.status <> 'done') AND NOT s.is_delisting "
        "ORDER BY COALESCE(p.attempts, 0), s.code",
        [task],
    ).fetchall()
    return [r[0] for r in rows]


def mark_progress(
    conn: duckdb.DuckDBPyConnection,
    task: str,
    code: str,
    status: str,
    last_date: dt.date | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO backfill_progress (task, subject_id, status, last_date, attempts, updated_at) "
        "VALUES (?, ?, ?, COALESCE(?, (SELECT last_date FROM backfill_progress WHERE task = ? AND subject_id = ?)), "
        "COALESCE((SELECT attempts FROM backfill_progress WHERE task = ? AND subject_id = ?), 0) + 1, now())",
        [task, code, status, last_date, task, code, task, code],
    )


# 复权因子每次都从上市起取：只取分层区间内的除权事件时，区间前累计的后复权因子会丢失
ADJ_FROM = dt.date(1990, 1, 1)
LEGACY_FULL = dt.date(1900, 1, 1)
MAX_TIER_FAILURES = 3

Ranges = dict[str, tuple[dt.date, dt.date]]


@dataclass(frozen=True)
class Tier:
    key: str
    label: str
    start: dt.date


def tier_label(days: int) -> str:
    if days % 365 == 0:
        return f"近 {days // 365} 年"
    if 175 <= days <= 190:
        return "近半年"
    return f"近 {days} 天"


def bars_tiers(day: dt.date, tiers_days: list[int], years: int) -> list[Tier]:
    """日线回补分层（由近及远），最后一层为配置的全部年数。"""
    full = 365 * years
    days = [*sorted({d for d in tiers_days if 0 < d < full}), full]
    return [Tier(f"{d}d", tier_label(d), day - dt.timedelta(days=d)) for d in days]


_COVERED = (
    "(t.covered_from IS NOT NULL AND (t.covered_from <= ? "
    "OR (s.list_date IS NOT NULL AND s.list_date >= t.covered_from)))"
)


def tier_pending(
    conn: duckdb.DuckDBPyConnection, tier_start: dt.date, day: dt.date, limit: int
) -> Ranges:
    """本层还没覆盖的股票及各自要拉的区间：没拉过的拉 [层起点, 今天]，拉过更近一层的只补 [层起点, 已覆盖起点)。
    连续失败达到上限的先跳过（启动时清零重试），免得个别股票卡住整层。"""
    rows = conn.execute(
        f"""
        SELECT s.code, t.covered_from FROM security s
        LEFT JOIN backfill_tier t ON t.task = ? AND t.subject_id = s.code
        WHERE NOT s.is_delisting AND NOT {_COVERED} IS TRUE
          AND COALESCE(t.failures, 0) < ?
        ORDER BY COALESCE(t.failures, 0), t.covered_from IS NOT NULL, s.code
        LIMIT ?
        """,
        [TASK, tier_start, MAX_TIER_FAILURES, limit],
    ).fetchall()
    return {
        str(code): (tier_start, (cov - dt.timedelta(days=1)) if cov is not None else day)
        for code, cov in rows
    }


def tier_counts(conn: duckdb.DuckDBPyConnection, tiers: list[Tier]) -> list[dict[str, Any]]:
    """每层：total 在市证券数、done 已覆盖、failed 连续失败达上限暂跳过、pending 待拉。"""
    total_row = conn.execute("SELECT count(*) FROM security WHERE NOT is_delisting").fetchone()
    total = int(total_row[0]) if total_row else 0
    out: list[dict[str, Any]] = []
    for t in tiers:
        row = conn.execute(
            f"""
            SELECT count(*) FILTER (WHERE {_COVERED}),
                   count(*) FILTER (WHERE NOT {_COVERED} IS TRUE AND t.failures >= ?)
            FROM security s JOIN backfill_tier t ON t.task = ? AND t.subject_id = s.code
            WHERE NOT s.is_delisting
            """,
            [t.start, t.start, MAX_TIER_FAILURES, TASK],
        ).fetchone()
        done, failed = (int(row[0]), int(row[1])) if row else (0, 0)
        out.append(
            {
                "key": t.key,
                "label": t.label,
                "start": t.start.isoformat(),
                "total": total,
                "done": done,
                "failed": failed,
                "pending": max(0, total - done - failed),
            }
        )
    return out


def reset_tier_failures(conn: duckdb.DuckDBPyConnection) -> int:
    row = conn.execute(
        "UPDATE backfill_tier SET failures = 0 WHERE task = ? AND failures > 0", [TASK]
    ).fetchone()
    return int(row[0]) if row else 0


def backfill_bars(
    conn: duckdb.DuckDBPyConnection,
    ad: Adapters,
    *,
    start: dt.date,
    end: dt.date,
    day: dt.date,
    codes: list[str] | None = None,
    limit: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """逐只拉日线 + 复权因子写 bar_daily。返回本次完成的股票数。"""
    todo = codes if codes is not None else pending_codes(conn, TASK)
    if limit is not None:
        todo = todo[:limit]
    ranges = {c: (start, end) for c in todo}
    results = fetch_bars(ad, todo, start=start, end=end, day=day, should_stop=should_stop)
    return store_bars(conn, results, day, ranges=ranges, full_start=start)


BarsResult = tuple[str, tuple[pl.DataFrame, pl.DataFrame] | AdapterError]


def fetch_bars(
    ad: Adapters,
    codes: list[str],
    *,
    start: dt.date,
    end: dt.date,
    day: dt.date,
    ranges: Ranges | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_item: Callable[[str], None] | None = None,
) -> list[BarsResult]:
    """只拉数据不写库（可在写锁外执行）。Baostock 会话不支持并发，逐只顺序拉。
    `ranges` 给出各股自己的区间（分层回补），缺省用 [start, end]。
    每拉完一只（成功或失败）调用 `on_item(code)`，供进度显示。"""
    out: list[BarsResult] = []
    for code in codes:
        if should_stop and should_stop():
            break
        a, b = (ranges or {}).get(code, (start, end))
        try:
            k = ad.baostock.kdata(code, a, b, day)
            adj = ad.baostock.adjust_factor(code, ADJ_FROM, day, day)
            out.append((code, (k, adj)))
        except AdapterError as exc:
            out.append((code, exc))
            if isinstance(exc, SourceBlockedError):
                break
        if on_item:
            on_item(code)
    return out


def _mark_tier(
    conn: duckdb.DuckDBPyConnection, code: str, covered_from: dt.date | None, failed: bool
) -> dt.date | None:
    """更新覆盖起点（取更早者）与连续失败次数，返回更新后的覆盖起点。"""
    row = conn.execute(
        "SELECT covered_from, failures FROM backfill_tier WHERE task = ? AND subject_id = ?",
        [TASK, code],
    ).fetchone()
    old_cov, old_fail = (row[0], int(row[1])) if row else (None, 0)
    cov = old_cov
    if not failed and covered_from is not None:
        cov = covered_from if old_cov is None else min(old_cov, covered_from)
    conn.execute(
        "INSERT OR REPLACE INTO backfill_tier (task, subject_id, covered_from, failures, updated_at) "
        "VALUES (?, ?, ?, ?, now())",
        [TASK, code, cov, old_fail + 1 if failed else 0],
    )
    return cov


def store_bars(
    conn: duckdb.DuckDBPyConnection,
    results: list[BarsResult],
    day: dt.date,
    *,
    ranges: Ranges | None = None,
    full_start: dt.date | None = None,
) -> int:
    """写日线并记进度。`ranges` 为各股本次拉取的区间；覆盖起点早于 `full_start`（最后一层起点）
    即记为 done，否则为 partial（近期已有、更早的还在补）。"""
    done = 0
    blocked = next((r for _, r in results if isinstance(r, SourceBlockedError)), None)
    if blocked is not None:
        # 数据源整体封禁不是个股的问题：只记一条，不计入各股失败次数（否则解封后这些股票会被跳过）
        record_quality(
            conn,
            source="baostock",
            endpoint=blocked.endpoint,
            trade_date=day,
            status=QualityStatus.MISSING,
            reason=str(blocked),
        )
    for code, res in results:
        rng = (ranges or {}).get(code)
        if isinstance(res, SourceBlockedError):
            continue
        if isinstance(res, AdapterError):
            record_quality(
                conn,
                source="baostock",
                endpoint="kdata",
                trade_date=day,
                status=QualityStatus.MISSING,
                reason=str(res),
            )
            _mark_tier(conn, code, None, failed=True)
            mark_progress(conn, TASK, code, "failed")
            continue
        cov = _mark_tier(conn, code, rng[0] if rng else None, failed=False)
        status = (
            "done" if full_start is None or (cov is not None and cov <= full_start) else "partial"
        )
        k, adj = res
        if k.is_empty():
            mark_progress(conn, TASK, code, status, None)
            continue
        bars = bars_with_adjust(k, adj)
        upsert(conn, "bar_daily", bars)
        # 补更早一层时区间不含近期，最后日期沿用已有值
        newest: dt.date = bars["trade_date"].max()  # type: ignore[assignment]
        prev = conn.execute(
            "SELECT last_date FROM backfill_progress WHERE task = ? AND subject_id = ?",
            [TASK, code],
        ).fetchone()
        last = newest if prev is None or prev[0] is None or newest > prev[0] else None
        mark_progress(conn, TASK, code, status, last)
        done += 1
    return done


def update_float_mv(
    conn: duckdb.DuckDBPyConnection, spot: pl.DataFrame, trade_date: dt.date
) -> int:
    """东财快照的流通市值写回当日 bar_daily.float_mv（架构 3.2：流通股本近似自由流通）。"""
    conn.register("_spot", spot.select("code", "float_mv"))
    try:
        conn.execute(
            "UPDATE bar_daily SET float_mv = s.float_mv FROM _spot s "
            "WHERE bar_daily.code = s.code AND bar_daily.trade_date = ?",
            [trade_date],
        )
    finally:
        conn.unregister("_spot")
    return spot.height


def daily_bars_from_snapshot(
    snapshot: pl.DataFrame, trade_date: dt.date, prev_close: pl.DataFrame
) -> pl.DataFrame:
    """价格模式 / 收盘当天日线未到时，用东财快照生成当日 bar_daily 行（架构 4.1.2）。"""
    df = snapshot.join(prev_close, on="code", how="left")
    up, down = _limit_prices(pl.col("pre_close"), pl.col("code"), pl.col("name").str.contains("ST"))
    return df.select(
        "code",
        pl.lit(trade_date).alias("trade_date"),
        pl.lit(None, dtype=pl.Float64).alias("open"),
        pl.lit(None, dtype=pl.Float64).alias("high"),
        pl.lit(None, dtype=pl.Float64).alias("low"),
        "close",
        "pre_close",
        pl.lit(None, dtype=pl.Float64).alias("volume"),
        "amount",
        "turnover",
        "pct_chg",
        pl.lit(None, dtype=pl.Float64).alias("adj_factor"),
        up.alias("limit_up"),
        down.alias("limit_down"),
        "float_mv",
        (pl.col("amount").fill_null(0.0) == 0).alias("is_suspended"),
    )
