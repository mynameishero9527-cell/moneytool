"""按配置组装全部适配器；`doctor` 逐源体检。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from moneytool.adapters.baostock import BaostockAdapter
from moneytool.adapters.base import FetchContext, RateLimiter, RawCache
from moneytool.adapters.csindex import CsindexAdapter
from moneytool.adapters.eastmoney import EastmoneyAdapter
from moneytool.adapters.shenwan import ShenwanAdapter
from moneytool.adapters.sina import SinaAdapter
from moneytool.config import Settings, SourceRateLimit


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

    def close(self) -> None:
        self.baostock.close()

    def health(self, day: dt.date | None = None) -> list[dict[str, Any]]:
        day = day or dt.date.today()
        out = [
            a.health(day)
            for a in (self.sina, self.eastmoney, self.baostock, self.shenwan, self.csindex)
        ]
        out.insert(2, self.eastmoney.health_realtime(day))
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
    )
