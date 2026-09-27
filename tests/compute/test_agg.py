"""市场与板块合计：整天没有资金流或涨跌时保持空，不收成 0。"""

from __future__ import annotations

import polars as pl

from moneytool.compute.market import compute_market_daily
from moneytool.compute.sector import aggregate_sector_daily
from moneytool.params import Params
from tests.conftest import trading_dates


def test_market_all_null_flow_stays_null(params: Params) -> None:
    day = trading_dates(1)[0]
    df = pl.DataFrame(
        {
            "trade_date": [day, day],
            "pct_chg": [0.01, -0.02],
            "is_limit_up": [False, False],
            "is_consecutive_limit": [False, False],
            "amount": [1.0e8, 2.0e8],
            "net_main": pl.Series([None, None], dtype=pl.Float64),
            "net_small": pl.Series([None, None], dtype=pl.Float64),
        }
    )
    out = compute_market_daily(df, params)
    assert out["net_main_all"][0] is None
    assert out["net_small_all"][0] is None
    assert out["amount_all"][0] == 3.0e8
    assert out["eqw_ret"][0] == (0.01 - 0.02) / 2


def test_sector_missing_flow_and_price_stay_null(params: Params) -> None:
    day = trading_dates(1)[0]
    stock = pl.DataFrame(
        {
            "code": ["000001.SZ", "000002.SZ"],
            "trade_date": [day, day],
            "net_main": pl.Series([None, None], dtype=pl.Float64),
            "net_super": pl.Series([None, None], dtype=pl.Float64),
            "net_large": pl.Series([None, None], dtype=pl.Float64),
            "net_medium": pl.Series([None, None], dtype=pl.Float64),
            "net_small": pl.Series([None, None], dtype=pl.Float64),
            "amount": [100.0, 200.0],
            "pct_chg": [None, None],
            "is_one_word": [False, False],
            "is_limit_up": [False, False],
            "is_consecutive_limit": [False, False],
            "turnover": [0.01, 0.02],
            "float_mv": [1.0e9, 1.0e9],
            "amount_ma_20d": [1.0e8, 1.0e8],
        }
    )
    member = pl.DataFrame(
        {
            "sector_id": ["s", "s"],
            "code": ["000001.SZ", "000002.SZ"],
            "trade_date": [day, day],
        }
    )
    out = aggregate_sector_daily(stock, member, params)
    assert out["sector_net_main"][0] is None
    assert out["breadth"][0] is None
    assert out["sector_amount"][0] == 300.0


def test_one_word_flow_is_excluded_not_missing(params: Params) -> None:
    day = trading_dates(1)[0]
    stock = pl.DataFrame(
        {
            "code": ["000001.SZ", "000002.SZ"],
            "trade_date": [day, day],
            "net_main": [100.0, 50.0],
            "net_super": [60.0, 30.0],
            "net_large": [40.0, 20.0],
            "net_medium": [-50.0, -25.0],
            "net_small": [-50.0, -25.0],
            "amount": [1000.0, 1000.0],
            "pct_chg": [0.1, 0.01],
            "is_one_word": [True, False],
            "is_limit_up": [True, False],
            "is_consecutive_limit": [False, False],
            "turnover": [0.01, 0.02],
            "float_mv": [1.0e9, 1.0e9],
            "amount_ma_20d": [1.0e8, 1.0e8],
        }
    )
    member = pl.DataFrame(
        {
            "sector_id": ["s", "s"],
            "code": ["000001.SZ", "000002.SZ"],
            "trade_date": [day, day],
        }
    )
    out = aggregate_sector_daily(stock, member, params)
    assert out["sector_net_main"][0] == 50.0
    assert out["breadth"][0] == 1.0
