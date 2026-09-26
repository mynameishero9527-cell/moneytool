"""按配置组装全部适配器；`doctor` 逐源体检。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import polars as pl

from moneytool.adapters.baostock import BaostockAdapter
from moneytool.adapters.base import AdapterError, FetchContext, RateLimiter, RawCache
from moneytool.adapters.csindex import CsindexAdapter
from moneytool.adapters.eastmoney import SOURCE as EASTMONEY
from moneytool.adapters.eastmoney import EastmoneyAdapter
from moneytool.adapters.shenwan import ShenwanAdapter
from moneytool.adapters.sina import SOURCE as SINA
from moneytool.adapters.sina import SinaAdapter
from moneytool.config import Settings, SourceRateLimit
from moneytool.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")


def _ctx(cache: RawCache, rate: SourceRateLimit) -> FetchContext:
    return FetchContext(
        cache=cache,
        rate=rate,
        limiter=RateLimiter(rate.min_interval_seconds),
        per_stock_limiter=RateLimiter(rate.per_stock_interval_seconds),
    )


@dataclass
class Adapters:
    eastmoney: EastmoneyAdapter
    baostock: BaostockAdapter
    shenwan: ShenwanAdapter
    csindex: CsindexAdapter
    sina: SinaAdapter
    realtime_source: str = "auto"

    def close(self) -> None:
        self.baostock.close()

    def realtime_order(self) -> list[str]:
        """盘中资金流 / 快照的取数顺序。auto 下东财冷却中直接走新浪，不白等重试。"""
        if self.realtime_source != "auto":
            return [self.realtime_source]
        if self.eastmoney.breaker.remaining() > 0:
            return [SINA]
        return [EASTMONEY, SINA]

    def _first(self, order: list[str], endpoint: str, call: Callable[[Any], T]) -> tuple[str, T]:
        last: AdapterError | None = None
        for name in order:
            try:
                return name, call(getattr(self, name))
            except AdapterError as exc:
                last = exc
                if name != order[-1]:
                    log.warning("realtime_fallback", endpoint=endpoint, source=name, error=str(exc))
        assert last is not None
        raise last

    def flow_rank_stock(self, day: dt.date, segment: str) -> tuple[str, pl.DataFrame]:
        return self._first(
            self.realtime_order(), "flow_rank_stock", lambda a: a.flow_rank_stock(day, segment)
        )

    def spot(self, day: dt.date) -> tuple[str, pl.DataFrame]:
        return self._first(self.realtime_order(), "spot", lambda a: a.spot(day))

    def health(self, day: dt.date | None = None) -> list[dict[str, Any]]:
        day = day or dt.date.today()
        out = [
            a.health(day)
            for a in (self.sina, self.eastmoney, self.baostock, self.shenwan, self.csindex)
        ]
        out.insert(2, self.eastmoney.health_realtime(day))
        out.insert(3, self.sina.health_realtime(day))
        return out


def build_adapters(settings: Settings) -> Adapters:
    cache = RawCache(settings.raw_dir)
    rl = settings.rate_limit
    return Adapters(
        eastmoney=EastmoneyAdapter(_ctx(cache, rl.eastmoney)),
        baostock=BaostockAdapter(_ctx(cache, rl.baostock)),
        shenwan=ShenwanAdapter(_ctx(cache, rl.shenwan)),
        csindex=CsindexAdapter(_ctx(cache, rl.csindex)),
        sina=SinaAdapter(_ctx(cache, rl.sina), years=settings.data.backfill_years_flow),
        realtime_source=settings.data.realtime_source,
    )
