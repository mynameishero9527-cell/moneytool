from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest

from moneytool.params import Params, builtin_params_dir, latest_params
from moneytool.storage.conn import Database
from moneytool.storage.migrate import migrate


@pytest.fixture(scope="session")
def params() -> Params:
    return latest_params(builtin_params_dir())


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database(":memory:")
    migrate(database.rw)
    yield database
    database.close()


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def trading_dates(n: int, start: dt.date = dt.date(2026, 1, 5)) -> list[dt.date]:
    """生成 n 个工作日（不查真实日历）。"""
    out: list[dt.date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def make_stock_frame(
    code: str,
    dates: list[dt.date],
    *,
    net_main: list[float],
    pct_chg: list[float],
    close: list[float] | None = None,
    amount: float = 1e8,
    turnover: float = 0.02,
    limit_up: float | None = None,
) -> pl.DataFrame:
    """构造单只股票的多日输入行（features 输入契约）。"""
    n = len(dates)
    if close is None:
        c = 10.0
        close = []
        for r in pct_chg:
            c *= 1 + r
            close.append(round(c, 2))
    pre = [close[0] / (1 + pct_chg[0]), *close[:-1]]
    return pl.DataFrame(
        {
            "code": [code] * n,
            "trade_date": dates,
            "net_main": net_main,
            "net_super": [x * 0.6 for x in net_main],
            "net_large": [x * 0.4 for x in net_main],
            "net_medium": [-x * 0.5 for x in net_main],
            "net_small": [-x * 0.5 for x in net_main],
            "open": pre,
            "high": [max(a, b) * 1.01 for a, b in zip(pre, close, strict=True)],
            "low": [min(a, b) * 0.99 for a, b in zip(pre, close, strict=True)],
            "close": close,
            "pre_close": pre,
            "amount": [amount] * n,
            "turnover": [turnover] * n,
            "pct_chg": pct_chg,
            "adj_factor": [1.0] * n,
            "limit_up": [limit_up if limit_up is not None else round(p * 1.1, 2) for p in pre],
            "float_mv": [5e9] * n,
        },
        schema_overrides={"trade_date": pl.Date},
    )
