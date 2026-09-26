"""日线与复权回补（Baostock），可中断续跑；进度写 backfill_progress。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import duckdb
import polars as pl

from moneytool.adapters.base import AdapterError
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
    return store_bars(
        conn, fetch_bars(ad, todo, start=start, end=end, day=day, should_stop=should_stop), day
    )


BarsResult = tuple[str, tuple[pl.DataFrame, pl.DataFrame] | AdapterError]


def fetch_bars(
    ad: Adapters,
    codes: list[str],
    *,
    start: dt.date,
    end: dt.date,
    day: dt.date,
    should_stop: Callable[[], bool] | None = None,
    on_item: Callable[[str], None] | None = None,
) -> list[BarsResult]:
    """只拉数据不写库（可在写锁外执行）。Baostock 会话不支持并发，逐只顺序拉。
    每拉完一只（成功或失败）调用 `on_item(code)`，供进度显示。"""
    out: list[BarsResult] = []
    for code in codes:
        if should_stop and should_stop():
            break
        try:
            k = ad.baostock.kdata(code, start, end, day)
            adj = ad.baostock.adjust_factor(code, start, end, day)
            out.append((code, (k, adj)))
        except AdapterError as exc:
            out.append((code, exc))
        if on_item:
            on_item(code)
    return out


def store_bars(conn: duckdb.DuckDBPyConnection, results: list[BarsResult], day: dt.date) -> int:
    done = 0
    for code, res in results:
        if isinstance(res, AdapterError):
            record_quality(
                conn,
                source="baostock",
                endpoint="kdata",
                trade_date=day,
                status=QualityStatus.MISSING,
                reason=str(res),
            )
            mark_progress(conn, TASK, code, "failed")
            continue
        k, adj = res
        if k.is_empty():
            mark_progress(conn, TASK, code, "done", None)
            continue
        bars = bars_with_adjust(k, adj)
        upsert(conn, "bar_daily", bars)
        mark_progress(conn, TASK, code, "done", bars["trade_date"].max())  # type: ignore[arg-type]
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
