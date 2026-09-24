"""顺序应用 `migrations/NNNN_*.sql`，`schema_version` 记录已应用版本。"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import duckdb

MIGRATIONS_PKG = "moneytool.storage.migrations"


def list_migrations() -> list[tuple[int, str, str]]:
    """返回 (版本号, 文件名, SQL) 按版本升序。"""
    out: list[tuple[int, str, str]] = []
    for entry in resources.files(MIGRATIONS_PKG).iterdir():
        name = entry.name
        if not name.endswith(".sql"):
            continue
        version = int(name.split("_", 1)[0])
        out.append((version, name, entry.read_text(encoding="utf-8")))
    out.sort(key=lambda t: t[0])
    return out


def applied_versions(conn: duckdb.DuckDBPyConnection) -> set[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, "
        "name VARCHAR NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {int(r[0]) for r in rows}


def migrate(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """应用未应用的迁移，返回本次应用的文件名。"""
    done = applied_versions(conn)
    applied: list[str] = []
    for version, name, sql in list_migrations():
        if version in done:
            continue
        conn.execute("BEGIN")
        try:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)", [version, name]
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(name)
    return applied


def table_columns(conn: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[str, str]]]:
    """全部表的 (列名, 类型)，用于结构测试。"""
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
    ).fetchall()
    out: dict[str, list[tuple[str, str]]] = {}
    for table, col, dtype in rows:
        out.setdefault(str(table), []).append((str(col), str(dtype)))
    return out


def migrations_dir() -> Path:
    return Path(str(resources.files(MIGRATIONS_PKG)))
