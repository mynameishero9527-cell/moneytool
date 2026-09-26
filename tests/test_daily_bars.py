"""当日日线由全 A 快照生成：复权因子沿用上一日，只对除权和快照缺漏的股票请求 Baostock。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl

from moneytool.ingest.bars import restated_codes
from moneytool.storage.repo import upsert
from tests.test_backfill_tiers import _setup

DAY = dt.date(2026, 9, 24)
PREV = DAY - dt.timedelta(days=1)


def _spot(codes: list[str], pre_close: list[float]) -> pl.DataFrame:
    n = len(codes)
    return pl.DataFrame(
        {
            "code": codes,
            "name": ["测试"] * n,
            "open": [10.1] * n,
            "high": [10.8] * n,
            "low": [9.9] * n,
            "close": [10.4] * n,
            "pre_close": pre_close,
            "volume": [1.0e6] * n,
            "pct_chg": [0.01] * n,
            "amount": [1.0e8] * n,
            "turnover": [0.02] * n,
            "total_mv": [2.0e11] * n,
            "float_mv": [1.0e11] * n,
        }
    )


def test_restated_codes_flags_only_changed_pre_close() -> None:
    spot = _spot(["000001.SZ", "000002.SZ"], [10.0, 18.0])
    prev = pl.DataFrame({"code": ["000001.SZ", "000002.SZ"], "pre_close": [10.0, 20.0]})
    assert restated_codes(spot, prev) == ["000002.SZ"]


def test_daily_bars_reuse_factor_and_fetch_only_gaps(tmp_data_dir: Path) -> None:
    ctx, runner, fake = _setup(
        tmp_data_dir,
        {
            "000001.SZ": dt.date(2010, 1, 1),
            "000002.SZ": dt.date(2010, 1, 1),
            "000003.SZ": dt.date(2010, 1, 1),
        },
    )
    with ctx.db.write() as conn:
        upsert(
            conn,
            "bar_daily",
            pl.DataFrame(
                {
                    "code": ["000001.SZ", "000002.SZ"],
                    "trade_date": [PREV, PREV],
                    "open": [10.0, 20.0],
                    "high": [10.0, 20.0],
                    "low": [10.0, 20.0],
                    "close": [10.0, 20.0],
                    "pre_close": [10.0, 20.0],
                    "volume": [1.0, 1.0],
                    "amount": [1.0, 1.0],
                    "turnover": [0.01, 0.01],
                    "pct_chg": [0.0, 0.0],
                    "adj_factor": [2.0, 2.0],
                    "limit_up": [11.0, 22.0],
                    "limit_down": [9.0, 18.0],
                    "float_mv": [1.0, 1.0],
                    "is_suspended": [False, False],
                }
            ),
        )
        assert runner._daily_bars(conn, DAY, _spot(["000001.SZ", "000002.SZ"], [10.0, 18.0]))
        rows = {
            r[0]: r
            for r in conn.execute(
                "SELECT code, open, adj_factor FROM bar_daily WHERE trade_date = ? ORDER BY code",
                [DAY],
            ).fetchall()
        }
    assert rows["000001.SZ"][1] == 10.1
    assert rows["000001.SZ"][2] == 2.0  # 未除权，沿用上一日
    assert rows["000002.SZ"][2] == 3.5  # 昨收变化，取了新因子
    assert "000003.SZ" in rows  # 快照没有，逐只补上
    touched = {c[1] for c in fake.calls}
    assert "000001.SZ" not in touched
    assert ("adj", "000002.SZ") in {(c[0], c[1]) for c in fake.calls}
    ctx.close()
