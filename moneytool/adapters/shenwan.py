"""申万（经 AkShare 申万研究接口）：一级 / 二级列表、成分、指数日线。sector_id 形如 `sw:801010`。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

import polars as pl

from moneytool.adapters.base import AdapterError, FetchContext, normalize_code, retry
from moneytool.adapters.contracts import SHENWAN

SOURCE = "shenwan"


def sw_sector_id(index_code: str) -> str:
    """`801010.SI` / `801010` → `sw:801010`。"""
    return "sw:" + index_code.split(".", maxsplit=1)[0]


class ShenwanAdapter:
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
        return SHENWAN[endpoint]

    def _run(
        self,
        endpoint: str,
        params: dict[str, Any],
        day: dt.date,
        call: Callable[[], Any],
        std: Callable[[pl.DataFrame], pl.DataFrame],
    ) -> pl.DataFrame:
        cached = self.ctx.cache.get(SOURCE, endpoint, day, params)
        if cached is not None:
            return std(cached)

        def once() -> pl.DataFrame:
            self.ctx.limiter.wait()
            raw = pl.from_pandas(call())
            if raw.is_empty():
                raise AdapterError(SOURCE, endpoint, "空响应")
            return raw

        raw = retry(once, source=SOURCE, endpoint=endpoint, backoff=self.ctx.rate.backoff_seconds)
        self.ctx.cache.put(
            SOURCE, endpoint, day, params, raw, {"source": SOURCE, "endpoint": endpoint}
        )
        out = std(raw)
        SHENWAN[endpoint].validate(out, SOURCE, endpoint)
        return out

    def l1_list(self, day: dt.date) -> pl.DataFrame:
        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("行业代码")
                .cast(pl.Utf8)
                .map_elements(sw_sector_id, return_dtype=pl.Utf8)
                .alias("sector_id"),
                pl.col("行业名称").cast(pl.Utf8).alias("name"),
                pl.col("成份个数").cast(pl.Int64, strict=False).alias("member_count"),
            )

        return self._run("l1_list", {}, day, lambda: self.ak.sw_index_first_info(), std)

    def l2_list(self, day: dt.date) -> pl.DataFrame:
        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("行业代码")
                .cast(pl.Utf8)
                .map_elements(sw_sector_id, return_dtype=pl.Utf8)
                .alias("sector_id"),
                pl.col("行业名称").cast(pl.Utf8).alias("name"),
                pl.col("上级行业").cast(pl.Utf8).alias("parent_name"),
                pl.col("成份个数").cast(pl.Int64, strict=False).alias("member_count"),
            )

        return self._run("l2_list", {}, day, lambda: self.ak.sw_index_second_info(), std)

    def cons(self, sector_id: str, day: dt.date) -> pl.DataFrame:
        """`index_component_sw(symbol="801010")`：成分、权重、计入日期。"""
        code = sector_id.removeprefix("sw:")

        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("证券代码")
                .cast(pl.Utf8)
                .map_elements(normalize_code, return_dtype=pl.Utf8)
                .alias("code"),
                pl.col("证券名称").cast(pl.Utf8).alias("name"),
                pl.col("最新权重").cast(pl.Float64, strict=False).alias("weight"),
                pl.col("计入日期")
                .cast(pl.Utf8)
                .str.to_date("%Y-%m-%d", strict=False)
                .alias("since"),
            )

        return self._run(
            "cons",
            {"sector_id": sector_id},
            day,
            lambda: self.ak.index_component_sw(symbol=code),
            std,
        )

    def index_daily(self, sector_id: str, day: dt.date) -> pl.DataFrame:
        """`index_hist_sw(symbol, period="day")`：申万指数日线，用于板块收益。"""
        code = sector_id.removeprefix("sw:")

        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("日期")
                .cast(pl.Utf8)
                .str.to_date("%Y-%m-%d", strict=False)
                .alias("trade_date"),
                pl.col("收盘").cast(pl.Float64, strict=False).alias("close"),
                pl.col("成交额").cast(pl.Float64, strict=False).alias("amount"),
            ).filter(pl.col("trade_date").is_not_null())

        return self._run(
            "index_daily",
            {"sector_id": sector_id},
            day,
            lambda: self.ak.index_hist_sw(symbol=code, period="day"),
            std,
        )

    def health(self, day: dt.date) -> dict[str, Any]:
        started = dt.datetime.now()
        try:
            df = self.l1_list(day)
            ms = int((dt.datetime.now() - started).total_seconds() * 1000)
            return {
                "source": SOURCE,
                "endpoint": "l1_list",
                "ok": True,
                "rows": df.height,
                "ms": ms,
            }
        except AdapterError as exc:
            ms = int((dt.datetime.now() - started).total_seconds() * 1000)
            return {
                "source": SOURCE,
                "endpoint": "l1_list",
                "ok": False,
                "error": str(exc),
                "ms": ms,
            }
