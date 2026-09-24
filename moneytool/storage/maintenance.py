"""夜间维护（架构 5 / 8 节）：盘中中间结果清理、库文件备份轮换。"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import duckdb

# (表, 盘中条件)。收盘确认结果与 close 段快照永久保留。
INTRADAY_TABLES: tuple[tuple[str, str], ...] = (
    ("flow_intraday", "TRUE"),
    ("flow_snapshot", "segment <> 'close'"),
    ("sector_stage_intraday", "TRUE"),
    ("stock_role_intraday", "TRUE"),
    ("action_list_intraday", "TRUE"),
    ("feature_daily", "segment <> 'close'"),
    ("index_daily", "segment <> 'close'"),
    ("hint", "segment <> 'close'"),
    ("stock_profile", "segment <> 'close'"),
    ("market_daily", "segment <> 'close'"),
)


def prune_intraday(conn: duckdb.DuckDBPyConnection, day: dt.date, keep_days: int) -> dict[str, int]:
    cutoff = day - dt.timedelta(days=keep_days)
    out: dict[str, int] = {}
    for table, cond in INTRADAY_TABLES:
        row = conn.execute(
            f"SELECT count(*) FROM {table} WHERE trade_date < ? AND {cond}", [cutoff]
        ).fetchone()
        n = int(row[0]) if row else 0
        if n:
            conn.execute(f"DELETE FROM {table} WHERE trade_date < ? AND {cond}", [cutoff])
        out[table] = n
    return out


def backup_database(
    conn: duckdb.DuckDBPyConnection, db_path: Path, backups_dir: Path, day: dt.date, keep: int
) -> Path | None:
    """CHECKPOINT 后拷贝库文件，保留最近 `keep` 份。内存库不备份。"""
    if str(db_path) == ":memory:" or not db_path.exists():
        return None
    conn.execute("CHECKPOINT")
    backups_dir.mkdir(parents=True, exist_ok=True)
    dst = backups_dir / f"{db_path.stem}-{day.strftime('%Y%m%d')}{db_path.suffix}"
    shutil.copy2(db_path, dst)
    olds = sorted(backups_dir.glob(f"{db_path.stem}-*{db_path.suffix}"))
    for old in olds[: max(len(olds) - keep, 0)]:
        old.unlink(missing_ok=True)
    return dst
