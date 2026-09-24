from __future__ import annotations

import polars as pl
import pytest

from moneytool.compute.features import compute_stock_features
from moneytool.params import Params
from tests.conftest import make_stock_frame, trading_dates


def test_retention_and_streak(params: Params) -> None:
    dates = trading_dates(6)
    # 6 日：涨日流入、跌日流出、跌日流入（不减）、涨日流出（不加）……
    net = [100.0, -50.0, 30.0, -20.0, 80.0, 60.0]
    pct = [0.01, -0.01, -0.01, 0.01, 0.02, 0.01]
    df = compute_stock_features(
        make_stock_frame("000001.SZ", dates, net_main=net, pct_chg=pct), params
    )
    last = df.row(-1, named=True)
    # 近 5 日窗口 = 日 2..6：涨日流入 80+60=140；跌日流出 50 → 留存 90（日 3 跌日流入不减，日 4 涨日流出不加）
    assert last["retention_5d"] == pytest.approx(90.0)
    assert last["inflow_streak"] == 2
    assert df.row(1, named=True)["inflow_streak"] == -1
    assert last["inflow_days_5d"] == 3


def test_main_ratio_and_pulse(params: Params) -> None:
    dates = trading_dates(8)
    net = [10.0, 10.0, 10.0, 10.0, 10.0, 50.0, -20.0, -15.0]
    pct = [0.0] * 8
    df = compute_stock_features(
        make_stock_frame("000001.SZ", dates, net_main=net, pct_chg=pct, amount=1000.0), params
    )
    assert df["main_ratio"][0] == pytest.approx(0.01)
    # 第 6 日：前 5 日均值 10，当日 50 > 2 × 10 → 脉冲
    assert df["is_pulse"][5] is True
    # 脉冲后 2 日合计 -35 ≤ -0.6 × 50 = -30 → 脉冲回吐在第 8 日成立
    assert df["pulse_giveback"][7] is True
    assert df["pulse_giveback"][6] is False


def test_missing_column_raises(params: Params) -> None:
    with pytest.raises(ValueError, match="缺列"):
        compute_stock_features(pl.DataFrame({"code": ["a"]}), params)


def test_ols_slope_sign(params: Params) -> None:
    dates = trading_dates(6)
    net = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    df = compute_stock_features(
        make_stock_frame("000001.SZ", dates, net_main=net, pct_chg=[0.0] * 6, amount=1000.0), params
    )
    assert df["main_slope_5d"][-1] > 0
    assert df["main_slope_3d"][-1] == pytest.approx(0.01)


def test_limit_up_and_consecutive(params: Params) -> None:
    dates = trading_dates(4)
    pct = [0.10, 0.10, 0.10, -0.02]
    df = compute_stock_features(
        make_stock_frame("000001.SZ", dates, net_main=[1.0] * 4, pct_chg=pct), params
    )
    assert df["is_limit_up"].to_list() == [True, True, True, False]
    assert df["limit_streak"].to_list() == [1, 2, 3, 0]
    assert df["is_consecutive_limit"].to_list() == [False, True, True, False]
