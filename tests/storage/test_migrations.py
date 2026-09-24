from __future__ import annotations

import datetime as dt

import polars as pl

from moneytool.storage.conn import Database
from moneytool.storage.migrate import applied_versions, migrate, table_columns
from moneytool.storage.repo import record_quality, upsert
from moneytool.types import QualityStatus

EXPECTED_TABLES = {
    "trade_calendar",
    "security",
    "sector",
    "sector_member_snapshot",
    "index_member",
    "bar_daily",
    "flow_snapshot",
    "flow_intraday",
    "flow_daily",
    "sector_flow_intraday",
    "sector_flow_daily",
    "feature_daily",
    "market_daily",
    "sector_stage_confirmed",
    "sector_stage_intraday",
    "stock_role_confirmed",
    "stock_role_intraday",
    "action_list_confirmed",
    "action_list_intraday",
    "tracking",
    "label_outcome",
    "index_daily",
    "hold_eval",
    "hint",
    "user_mark",
    "brief",
    "data_quality",
    "external_asset_map",
    "external_asset_daily",
    "watchlist_group",
    "watchlist",
    "alert_log",
    "settings",
    "backfill_progress",
    "job_run",
    "schema_version",
    "stock_profile",
}


def test_all_tables_created(db: Database) -> None:
    tables = set(table_columns(db.rw))
    assert tables >= EXPECTED_TABLES


def test_migrate_is_idempotent(db: Database) -> None:
    assert migrate(db.rw) == []
    assert applied_versions(db.rw) == {1, 2}


def test_intraday_tables_have_segment(db: Database) -> None:
    cols = table_columns(db.rw)
    for t in ("sector_stage", "stock_role", "action_list"):
        confirmed = {c for c, _ in cols[f"{t}_confirmed"]}
        intraday = {c for c, _ in cols[f"{t}_intraday"]}
        assert intraday - confirmed == {"segment"}


def test_upsert_replaces(db: Database) -> None:
    d = dt.date(2026, 3, 2)
    df = pl.DataFrame({"trade_date": [d], "is_open": [True]})
    with db.write() as conn:
        assert upsert(conn, "trade_calendar", df) == 1
        assert upsert(conn, "trade_calendar", df.with_columns(is_open=pl.lit(False))) == 1
        rows = conn.execute("SELECT is_open FROM trade_calendar").fetchall()
    assert rows == [(False,)]


def test_record_quality(db: Database) -> None:
    with db.write() as conn:
        record_quality(
            conn,
            source="eastmoney",
            endpoint="flow_rank",
            trade_date=dt.date(2026, 3, 2),
            status=QualityStatus.MISSING,
            segment="0930_1030",
            reason="timeout",
        )
        n = conn.execute("SELECT count(*) FROM data_quality").fetchone()
    assert n == (1,)
