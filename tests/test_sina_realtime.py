"""东财限流时的新浪替代：盘中全市场资金流、全 A 快照、概念成分；auto 模式的取数顺序。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from moneytool.adapters.base import AdapterError, FetchContext, RateLimiter, RawCache
from moneytool.adapters.contracts import SINA
from moneytool.adapters.registry import Adapters
from moneytool.adapters.sina import SinaAdapter, sina_to_code, standardize_realtime
from moneytool.config import SourceRateLimit
from moneytool.ingest.reference import concept_sources, fetch_concepts

DAY = dt.date(2026, 9, 24)


class _Resp:
    def __init__(self, text: str, content: bytes | None = None) -> None:
        self.text = text
        self.content = content if content is not None else text.encode("gbk")
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


def _rt_row(i: int, symbol: str | None = None) -> dict[str, str]:
    # 与 2026-09-24 平安银行实测值同构：netamount = 四档净额之和
    return {
        "symbol": symbol or f"sz{i:06d}",
        "name": f"股{i}",
        "trade": "11.3",
        "changeratio": "-0.00440529",
        "amount": "1186736896",
        "netamount": "3569902",
        "r0_net": "18469334",
        "r3_net": "1935843",
    }


def _ctx(tmp_path: Path) -> FetchContext:
    return FetchContext(
        cache=RawCache(tmp_path),
        rate=SourceRateLimit(backoff_seconds=()),
        limiter=RateLimiter(0),
        per_stock_limiter=RateLimiter(0),
    )


def test_sina_to_code_keeps_only_a_shares() -> None:
    assert sina_to_code("sz000001") == "000001.SZ"
    assert sina_to_code("sh688981") == "688981.SH"
    assert sina_to_code("bj920000") == "920000.BJ"
    assert sina_to_code("sh511030") is None  # ETF
    assert sina_to_code("sz200011") is None  # B 股


def test_realtime_estimates_main_from_super_and_mid() -> None:
    raw = pl.DataFrame([_rt_row(1), _rt_row(2, "sh511030")])
    out = standardize_realtime(raw.cast(pl.Utf8))
    assert set(SINA["flow_rank_stock"].columns) <= set(out.columns)
    assert out["code"].to_list() == ["000001.SZ"]
    r = out.row(0, named=True)
    mid = 3569902 - 18469334 - 1935843
    assert r["net_super"] == 18469334
    assert r["net_small"] == 1935843
    assert r["net_large"] == pytest.approx(mid / 2)
    assert r["net_main"] == pytest.approx(18469334 + mid / 2)
    assert r["main_ratio"] == pytest.approx(r["net_main"] / 1186736896)
    total = r["net_super"] + r["net_large"] + r["net_medium"] + r["net_small"]
    assert total == pytest.approx(3569902)


def test_flow_rank_stock_pages_until_short_page(tmp_path: Path) -> None:
    pages: list[int] = []

    def get(url: str, params: dict[str, Any], **kw: Any) -> _Resp:
        assert "ssl_bkzj_ssggzj" in url
        pages.append(params["page"])
        start = (params["page"] - 1) * 1000
        n = 1000 if params["page"] < 4 else 200
        return _Resp(json.dumps([_rt_row(start + i + 1) for i in range(n)]))

    ad = SinaAdapter(_ctx(tmp_path), get=get)
    out = ad.flow_rank_stock(DAY, "1030_1130")
    assert pages == [1, 2, 3, 4]
    assert out.height == 3200
    # 同一分段第二次读缓存，不再请求
    ad.flow_rank_stock(DAY, "1030_1130")
    assert pages == [1, 2, 3, 4]


def test_concepts_from_sina(tmp_path: Path) -> None:
    boards = {f"gn_b{i}": f"gn_b{i},概念{i},2,10.0,0.1,1.5,1,1,sz000001,1,1,1,x" for i in range(60)}
    js = "var S_Finance_bankuai_class = " + json.dumps(boards, ensure_ascii=False)

    def get(url: str, params: dict[str, Any], **kw: Any) -> _Resp:
        if "newFLJK" in url:
            return _Resp(js)
        assert params["node"].startswith("gn_b")
        return _Resp(
            json.dumps(
                [
                    {"symbol": "sz000001", "name": "平安银行"},
                    {"symbol": "sh600036", "name": "招商银行"},
                ]
            )
        )

    sina = SinaAdapter(_ctx(tmp_path), get=get)
    ad = _adapters(sina, realtime="sina")
    got = fetch_concepts(ad, DAY, max_boards=5)
    assert got.source == "sina"
    assert got.sectors.height == 60
    assert got.sectors["sector_id"][0] == "concept:gn_b0"
    assert got.sectors["source"].unique().to_list() == ["sina"]
    assert len(got.members) == 60  # 新浪不限板块数
    assert got.members[0]["code"].to_list() == ["000001.SZ", "600036.SH"]


class _Breaker:
    def __init__(self, left: float) -> None:
        self.left = left

    def remaining(self) -> float:
        return self.left


class _FailingEm:
    def __init__(self, left: float = 0.0) -> None:
        self.breaker = _Breaker(left)
        self.calls = 0

    def flow_rank_stock(self, day: dt.date, segment: str) -> pl.DataFrame:
        self.calls += 1
        raise AdapterError("eastmoney", "flow_rank_stock", "RemoteDisconnected")


class _OkSina:
    def flow_rank_stock(self, day: dt.date, segment: str) -> pl.DataFrame:
        return pl.DataFrame({"code": ["000001.SZ"]})


def _adapters(sina: Any, em: Any = None, realtime: str = "auto") -> Adapters:
    return Adapters(
        eastmoney=em or _FailingEm(),
        baostock=None,  # type: ignore[arg-type]
        shenwan=None,  # type: ignore[arg-type]
        csindex=None,  # type: ignore[arg-type]
        sina=sina,
        realtime_source=realtime,
    )


def test_auto_falls_back_to_sina() -> None:
    em = _FailingEm()
    ad = _adapters(_OkSina(), em)
    assert ad.realtime_order() == ["eastmoney", "sina"]
    source, df = ad.flow_rank_stock(DAY, "t")
    assert source == "sina" and df.height == 1
    assert em.calls == 1


def test_auto_skips_eastmoney_in_cooldown() -> None:
    em = _FailingEm(left=600)
    ad = _adapters(_OkSina(), em)
    assert ad.realtime_order() == ["sina"]
    assert ad.flow_rank_stock(DAY, "t")[0] == "sina"
    assert em.calls == 0


def test_explicit_eastmoney_does_not_fall_back() -> None:
    ad = _adapters(_OkSina(), _FailingEm(), realtime="eastmoney")
    with pytest.raises(AdapterError):
        ad.flow_rank_stock(DAY, "t")


def test_concepts_stick_to_existing_source() -> None:
    ad = _adapters(_OkSina())
    assert concept_sources(ad, None) == ["eastmoney", "sina"]
    assert concept_sources(ad, "sina") == ["sina"]
    assert concept_sources(ad, "eastmoney") == ["eastmoney"]
