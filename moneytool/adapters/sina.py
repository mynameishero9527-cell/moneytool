"""新浪财经资金流：个股日频历史（一次请求可取约 8 年），作为日频资金流的正式来源。

接口 `MoneyFlow.ssl_qsfx_lscjfb`（资金流向-历史成交分布），按日期倒序分页；字段口径见 contracts.SINA。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import polars as pl
import requests

from moneytool.adapters.base import AdapterError, FetchContext, RateLimiter, retry
from moneytool.adapters.contracts import FLOW_COLS, SINA

SOURCE = "sina"
BASE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
MAX_ROWS = 2000
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://vip.stock.finance.sina.com.cn/moneyflow/",
}
_TIERS = (("super", "r0"), ("large", "r1"), ("medium", "r2"), ("small", "r3"))
_RAW_COLS = (
    "opendate",
    "trade",
    "changeratio",
    *(t for _, t in _TIERS),
    *(f"{t}_net" for _, t in _TIERS),
)


def code_to_sina(code: str) -> str:
    num, ex = code.split(".")
    return f"{ex.lower()}{num}"


def rows_for(since: dt.date | None, day: dt.date, years: int) -> int:
    """要取的行数：按自然日粗估交易日（多取一些），上限 MAX_ROWS。"""
    if since is None:
        return min(MAX_ROWS, years * 250 + 20)
    return min(MAX_ROWS, max(10, (day - since).days + 10))


def _num(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Float64, strict=False)


def standardize(raw: pl.DataFrame) -> pl.DataFrame:
    if raw.is_empty():
        return pl.DataFrame(
            schema={
                "trade_date": pl.Date(),
                "close": pl.Float64(),
                "pct_chg": pl.Float64(),
                **FLOW_COLS,
            }
        )
    total = sum((_num(t).fill_null(0.0) for _, t in _TIERS), pl.lit(0.0))
    df = raw.with_columns(_total=total).with_columns(
        *[_num(f"{t}_net").alias(f"net_{name}") for name, t in _TIERS],
    )
    df = df.with_columns(net_main=pl.col("net_super") + pl.col("net_large"))
    ratio = {
        f"{k}_ratio": pl.when(pl.col("_total") > 0)
        .then(pl.col(f"net_{k}") / pl.col("_total"))
        .otherwise(None)
        for k in ("main", "super", "large", "medium", "small")
    }
    return (
        df.with_columns(**ratio)
        .select(
            pl.col("opendate")
            .cast(pl.Utf8)
            .str.to_date("%Y-%m-%d", strict=False)
            .alias("trade_date"),
            _num("trade").alias("close"),
            _num("changeratio").alias("pct_chg"),
            *[pl.col(c) for c in FLOW_COLS],
        )
        .filter(pl.col("trade_date").is_not_null())
        .sort("trade_date")
    )


class SinaAdapter:
    source = SOURCE

    def __init__(self, ctx: FetchContext, years: int = 2, get: Any | None = None) -> None:
        self.ctx = ctx
        self.years = years
        self._get = get or requests.get

    def contract(self, endpoint: str) -> Any:
        return SINA[endpoint]

    def _json(self, method: str, params: dict[str, Any]) -> Any:
        r = self._get(
            f"{BASE}/MoneyFlow.{method}",
            params=params,
            headers=HEADERS,
            timeout=self.ctx.rate.timeout_seconds,
        )
        r.raise_for_status()
        text = r.text.strip()
        if not text or text == "null":
            return []
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AdapterError(SOURCE, method, f"非 JSON 响应（可能被限流）: {text[:80]}") from exc

    def flow_daily_stock(
        self,
        code: str,
        day: dt.date,
        limiter: RateLimiter | None = None,
        *,
        retries: bool = True,
        since: dt.date | None = None,
        cache: bool = True,
    ) -> pl.DataFrame:
        """个股日频资金流，按日期升序。`since` 为已有数据的最后一天，只取之后的少量行。"""
        endpoint = "flow_daily_stock"
        num = rows_for(since, day, self.years)
        params = {"code": code, "num": num}
        cached = self.ctx.cache.get(SOURCE, endpoint, day, params) if cache else None
        if cached is not None:
            return standardize(cached)

        def once() -> pl.DataFrame:
            (limiter or self.ctx.per_stock_limiter).wait()
            data = self._json(
                "ssl_qsfx_lscjfb",
                {"page": 1, "num": num, "sort": "opendate", "asc": 0, "daima": code_to_sina(code)},
            )
            if not isinstance(data, list):
                raise AdapterError(SOURCE, endpoint, f"响应格式异常: {str(data)[:80]}")
            rows = [
                {k: (None if row.get(k) is None else str(row.get(k))) for k in _RAW_COLS}
                for row in data
            ]
            return pl.DataFrame(rows, schema={k: pl.Utf8() for k in _RAW_COLS})

        backoff = self.ctx.rate.backoff_seconds if retries else ()
        raw = retry(once, source=SOURCE, endpoint=endpoint, backoff=backoff)
        if cache and not raw.is_empty():
            self.ctx.cache.put(
                SOURCE, endpoint, day, params, raw, {"source": SOURCE, "endpoint": endpoint}
            )
        out = standardize(raw)
        SINA[endpoint].validate(out, SOURCE, endpoint)
        return out

    def health(self, day: dt.date) -> dict[str, Any]:
        started = dt.datetime.now()
        try:
            df = self.flow_daily_stock(
                "600036.SH", day, since=day - dt.timedelta(days=10), retries=False, cache=False
            )
            ok = df.height > 0
            return {
                "source": SOURCE,
                "endpoint": "flow_daily_stock",
                "ok": ok,
                "rows": df.height,
                "ms": _ms(started),
                **({} if ok else {"error": "空响应"}),
            }
        except AdapterError as exc:
            return {
                "source": SOURCE,
                "endpoint": "flow_daily_stock",
                "ok": False,
                "error": str(exc),
                "ms": _ms(started),
            }


def _ms(started: dt.datetime) -> int:
    return int((dt.datetime.now() - started).total_seconds() * 1000)
