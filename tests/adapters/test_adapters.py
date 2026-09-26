from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import polars as pl
import pytest

from moneytool.adapters.base import (
    AdapterError,
    CaptchaError,
    ContractError,
    FetchContext,
    RateLimiter,
    RawCache,
    normalize_code,
    retry,
)
from moneytool.adapters.eastmoney import EastmoneyAdapter
from moneytool.adapters.shenwan import ShenwanAdapter
from moneytool.config import SourceRateLimit

D = dt.date(2026, 9, 24)


def ctx(tmp_path: Path) -> FetchContext:
    rate = SourceRateLimit(
        min_interval_seconds=0.0, per_stock_interval_seconds=0.0, backoff_seconds=(0.0,)
    )
    return FetchContext(
        cache=RawCache(tmp_path / "raw"),
        rate=rate,
        limiter=RateLimiter(0),
        per_stock_limiter=RateLimiter(0),
    )


def fake_rank_stock(n: int = 3200) -> pd.DataFrame:
    codes = [f"{i:06d}" for i in range(1, n + 1)]
    return pd.DataFrame(
        {
            "序号": range(1, n + 1),
            "最新价": [10.0] * n,
            "今日涨跌幅": [1.5] * n,
            "代码": codes,
            "名称": ["测试"] * n,
            "今日主力净流入-净额": [1_000_000.0] * n,
            "今日超大单净流入-净额": [600_000.0] * n,
            "今日超大单净流入-净占比": [3.0] * n,
            "今日大单净流入-净额": [400_000.0] * n,
            "今日大单净流入-净占比": [2.0] * n,
            "今日中单净流入-净额": [-500_000.0] * n,
            "今日中单净流入-净占比": [-2.5] * n,
            "今日小单净流入-净额": [-500_000.0] * n,
            "今日小单净流入-净占比": [-2.5] * n,
            "今日主力净流入-净占比": [5.0] * n,
        }
    )


class FakeAk:
    def __init__(self, *, fail_json: bool = False) -> None:
        self.calls = 0
        self.fail_json = fail_json

    def stock_individual_fund_flow_rank(self, indicator: str) -> pd.DataFrame:
        self.calls += 1
        if self.fail_json:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return fake_rank_stock()

    def stock_individual_fund_flow(self, stock: str, market: str) -> pd.DataFrame:
        self.calls += 1
        if self.fail_json:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return pd.DataFrame(
            {
                "日期": ["2026-09-23", "2026-09-24"],
                "收盘价": [10.0, 10.5],
                "涨跌幅": [1.0, 5.0],
                "主力净流入-净额": [100.0, 200.0],
                "主力净流入-净占比": [1.0, 2.0],
                "超大单净流入-净额": [60.0, 120.0],
                "超大单净流入-净占比": [0.6, 1.2],
                "大单净流入-净额": [40.0, 80.0],
                "大单净流入-净占比": [0.4, 0.8],
                "中单净流入-净额": [-50.0, -100.0],
                "中单净流入-净占比": [-0.5, -1.0],
                "小单净流入-净额": [-50.0, -100.0],
                "小单净流入-净占比": [-0.5, -1.0],
            }
        )

    def sw_index_first_info(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "行业代码": [f"8010{i:02d}.SI" for i in range(10, 41)],
                "行业名称": [f"行业{i}" for i in range(31)],
                "成份个数": [100] * 31,
                "静态市盈率": [20.0] * 31,
                "TTM(滚动)市盈率": [20.0] * 31,
                "市净率": [2.0] * 31,
                "静态股息率": [1.0] * 31,
            }
        )


def test_normalize_code() -> None:
    assert normalize_code("600000") == "600000.SH"
    assert normalize_code("1") == "000001.SZ"
    assert normalize_code("sz.000001") == "000001.SZ"
    assert normalize_code("300750.SZ") == "300750.SZ"
    assert normalize_code("430047") == "430047.BJ"
    with pytest.raises(ValueError, match="无法识别"):
        normalize_code("700000")


def test_flow_rank_stock_standardizes_and_caches(tmp_path: Path) -> None:
    ak = FakeAk()
    ad = EastmoneyAdapter(ctx(tmp_path), ak=ak)
    df = ad.flow_rank_stock(D, "0930_1030")
    assert df.height == 3200
    assert df["code"][0] == "000001.SZ"
    assert df["main_ratio"][0] == pytest.approx(0.05)
    assert df["pct_chg"][0] == pytest.approx(0.015)
    assert df.schema["net_main"] == pl.Float64
    # 同一分段第二次不再请求（只追加缓存）
    ad.flow_rank_stock(D, "0930_1030")
    assert ak.calls == 1
    # 不同分段是新请求
    ad.flow_rank_stock(D, "1030_1130")
    assert ak.calls == 2
    assert any(
        tmp_path.joinpath("raw", "eastmoney", "flow_rank_stock", D.isoformat()).glob("*.parquet")
    )


def test_captcha_trips_and_blocks_per_stock(tmp_path: Path) -> None:
    ak = FakeAk(fail_json=True)
    c = ctx(tmp_path)
    ad = EastmoneyAdapter(c, ak=ak)
    with pytest.raises(CaptchaError):
        ad.flow_rank_stock(D, "0930_1030")
    assert ak.calls == 1  # 不重试
    assert not c.captcha_blocked("eastmoney", D)  # 全市场接口异常不连带停掉个股接口
    with pytest.raises(CaptchaError, match="疑似验证"):
        ad.flow_daily_stock("000001.SZ", D)
    assert ak.calls == 2
    with pytest.raises(CaptchaError, match="暂停"):
        ad.flow_daily_stock("000002.SZ", D)
    assert ak.calls == 2


def test_flow_daily_stock(tmp_path: Path) -> None:
    ad = EastmoneyAdapter(ctx(tmp_path), ak=FakeAk())
    df = ad.flow_daily_stock("000001.SZ", D)
    assert df["trade_date"].to_list() == [dt.date(2026, 9, 23), dt.date(2026, 9, 24)]
    assert df["pct_chg"][1] == pytest.approx(0.05)


def test_contract_error_on_short_response(tmp_path: Path) -> None:
    class ShortAk(FakeAk):
        def stock_individual_fund_flow_rank(self, indicator: str) -> pd.DataFrame:
            return fake_rank_stock(10)

    ad = EastmoneyAdapter(ctx(tmp_path), ak=ShortAk())
    with pytest.raises(ContractError, match="行数"):
        ad.flow_rank_stock(D, "0930_1030")


def test_shenwan_l1_list(tmp_path: Path) -> None:
    ad = ShenwanAdapter(ctx(tmp_path), ak=FakeAk())
    df = ad.l1_list(D)
    assert df.height == 31
    assert df["sector_id"][0] == "sw:801010"


def test_retry_backoff_then_raise() -> None:
    attempts: list[int] = []
    slept: list[float] = []

    def fn() -> pl.DataFrame:
        attempts.append(1)
        raise ConnectionError("boom")

    with pytest.raises(AdapterError, match="boom"):
        retry(fn, source="x", endpoint="y", backoff=(1.0, 2.0), sleep=slept.append)
    assert len(attempts) == 3
    assert slept == [1.0, 2.0]


def test_raw_cache_prune(tmp_path: Path) -> None:
    cache = RawCache(tmp_path / "raw")
    old = dt.date(2026, 1, 1)
    cache.put("s", "e", old, {"a": 1}, pl.DataFrame({"x": [1]}), {})
    cache.put("s", "e", D, {"a": 1}, pl.DataFrame({"x": [1]}), {})
    removed = cache.prune(90, today=D)
    assert removed == 2  # parquet + meta
    assert cache.get("s", "e", D, {"a": 1}) is not None
    assert cache.get("s", "e", old, {"a": 1}) is None


def test_rate_limiter_spacing() -> None:
    import time

    rl = RateLimiter(0.05)
    t0 = time.monotonic()
    rl.wait()
    rl.wait()
    assert time.monotonic() - t0 >= 0.05
