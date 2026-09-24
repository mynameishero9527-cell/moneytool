"""中证指数：指数成分（含当日快照日期）。index_id 形如 `000300.SH`。"""

from __future__ import annotations

import datetime as dt
from typing import Any

import polars as pl

from moneytool.adapters.base import AdapterError, FetchContext, normalize_code, retry
from moneytool.adapters.contracts import CSINDEX

SOURCE = "csindex"

# 需求 8.6 所属指数 → 中证接口 symbol
INDEX_SYMBOLS: dict[str, str] = {
    "000016.SH": "000016",
    "000300.SH": "000300",
    "000905.SH": "000905",
    "000852.SH": "000852",
    "932000.CSI": "932000",
    "399006.SZ": "399006",
    "000688.SH": "000688",
}
INDEX_NAMES: dict[str, str] = {
    "000016.SH": "上证50",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "932000.CSI": "中证2000",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
}


class CsindexAdapter:
    source = SOURCE

    def __init__(self, ctx: FetchContext, ak: Any | None = None) -> None:
        self.ctx = ctx
        self._ak = ak

    @property
    def ak(self) -> Any:
        if self._ak is None:
            import akshare  # noqa: PLC0415

            self._ak = akshare
        return self._ak

    def contract(self, endpoint: str) -> Any:
        return CSINDEX[endpoint]

    def cons(self, index_id: str, day: dt.date) -> pl.DataFrame:
        symbol = INDEX_SYMBOLS[index_id]
        params = {"index_id": index_id}
        cached = self.ctx.cache.get(SOURCE, "cons", day, params)

        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.lit(index_id).alias("index_id"),
                pl.col("成分券代码")
                .cast(pl.Utf8)
                .map_elements(normalize_code, return_dtype=pl.Utf8)
                .alias("code"),
                pl.col("成分券名称").cast(pl.Utf8).alias("name"),
                pl.col("日期").cast(pl.Utf8).str.to_date("%Y-%m-%d", strict=False).alias("as_of"),
            )

        if cached is not None:
            return std(cached)

        def once() -> pl.DataFrame:
            self.ctx.limiter.wait()
            raw = pl.from_pandas(self.ak.index_stock_cons_csindex(symbol=symbol))
            if raw.is_empty():
                raise AdapterError(SOURCE, "cons", "空响应")
            return raw

        raw = retry(once, source=SOURCE, endpoint="cons", backoff=self.ctx.rate.backoff_seconds)
        self.ctx.cache.put(SOURCE, "cons", day, params, raw, {"source": SOURCE, "endpoint": "cons"})
        out = std(raw)
        CSINDEX["cons"].validate(out, SOURCE, "cons")
        return out

    def health(self, day: dt.date) -> dict[str, Any]:
        started = dt.datetime.now()
        try:
            df = self.cons("000300.SH", day)
            ms = int((dt.datetime.now() - started).total_seconds() * 1000)
            return {"source": SOURCE, "endpoint": "cons", "ok": True, "rows": df.height, "ms": ms}
        except (AdapterError, KeyError) as exc:
            ms = int((dt.datetime.now() - started).total_seconds() * 1000)
            return {"source": SOURCE, "endpoint": "cons", "ok": False, "error": str(exc), "ms": ms}
