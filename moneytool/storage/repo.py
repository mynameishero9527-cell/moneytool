"""写入与常用查询助手。计算层通过这里读写 DuckDB，不在别处拼 SQL。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import duckdb
import polars as pl

from moneytool.types import QualityStatus


def upsert(conn: duckdb.DuckDBPyConnection, table: str, df: pl.DataFrame) -> int:
    """`INSERT OR REPLACE` 整个 DataFrame。列名必须是表的子集；返回行数。"""
    if df.is_empty():
        return 0
    cols = ", ".join(df.columns)
    conn.register("_upsert_tmp", df)
    try:
        conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM _upsert_tmp")
    finally:
        conn.unregister("_upsert_tmp")
    return df.height


def record_quality(
    conn: duckdb.DuckDBPyConnection,
    *,
    source: str,
    endpoint: str,
    trade_date: dt.date,
    status: QualityStatus,
    segment: str = "",
    reason: str | None = None,
    value: float | None = None,
) -> None:
    """写一条 `data_quality`。需求 11.3：缺失记录不填补。"""
    conn.execute(
        "INSERT OR REPLACE INTO data_quality (source, endpoint, trade_date, segment, status, reason, value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [source, endpoint, trade_date, segment, status.value, reason, value],
    )


def quality_for_date(conn: duckdb.DuckDBPyConnection, trade_date: dt.date) -> pl.DataFrame:
    return conn.execute(
        "SELECT source, endpoint, segment, status, reason, value, created_at FROM data_quality "
        "WHERE trade_date = ? ORDER BY created_at",
        [trade_date],
    ).pl()


def latest_confirmed_date(
    conn: duckdb.DuckDBPyConnection, table: str = "sector_stage_confirmed"
) -> dt.date | None:
    row = conn.execute(f"SELECT max(trade_date) FROM {table}").fetchone()
    if row is None or row[0] is None:
        return None
    value = row[0]
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def trading_days(conn: duckdb.DuckDBPyConnection, start: dt.date, end: dt.date) -> list[dt.date]:
    rows = conn.execute(
        "SELECT trade_date FROM trade_calendar WHERE is_open AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [start, end],
    ).fetchall()
    return [r[0] for r in rows]


def previous_trading_day(conn: duckdb.DuckDBPyConnection, trade_date: dt.date) -> dt.date | None:
    row = conn.execute(
        "SELECT max(trade_date) FROM trade_calendar WHERE is_open AND trade_date < ?", [trade_date]
    ).fetchone()
    return None if row is None else row[0]


def is_trading_day(conn: duckdb.DuckDBPyConnection, trade_date: dt.date) -> bool:
    row = conn.execute(
        "SELECT is_open FROM trade_calendar WHERE trade_date = ?", [trade_date]
    ).fetchone()
    return bool(row[0]) if row else False


def setting_get(
    conn: duckdb.DuckDBPyConnection, key: str, default: str | None = None
) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", [key]).fetchone()
    return default if row is None else row[0]


def setting_set(conn: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, now())",
        [key, value],
    )


def to_json(obj: Any) -> str:
    """证据、分项等结构化字段统一 JSON 序列化；日期转 ISO。"""

    def default(o: Any) -> Any:
        if isinstance(o, dt.date | dt.datetime):
            return o.isoformat()
        if hasattr(o, "__dataclass_fields__"):
            return {k: getattr(o, k) for k in o.__dataclass_fields__}
        if hasattr(o, "value"):
            return o.value
        raise TypeError(f"不可序列化: {type(o).__name__}")

    return json.dumps(obj, ensure_ascii=False, default=default)
