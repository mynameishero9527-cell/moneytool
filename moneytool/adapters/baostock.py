"""Baostock：交易日历、日线（不复权）、复权因子、证券基础信息。登录一次复用会话。"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable
from typing import Any

import polars as pl

from moneytool.adapters.base import (
    AdapterError,
    FetchContext,
    code_to_baostock,
    normalize_code,
    retry,
)
from moneytool.adapters.contracts import BAOSTOCK
from moneytool.logging import get_logger

SOURCE = "baostock"
log = get_logger(__name__)

K_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST"


def _rows(rs: Any) -> pl.DataFrame:
    """ResultData → DataFrame（全部 Utf8，再由调用方 cast）。"""
    if rs.error_code != "0":
        raise AdapterError(SOURCE, "query", f"{rs.error_code} {rs.error_msg}")
    data: list[list[str]] = []
    while rs.next():
        data.append(rs.get_row_data())
    fields: list[str] = list(rs.fields)
    if not data:
        return pl.DataFrame({f: pl.Series(f, [], dtype=pl.Utf8) for f in fields})
    return pl.DataFrame({f: [row[i] for row in data] for i, f in enumerate(fields)})


def _f(col: str) -> pl.Expr:
    return (
        pl.when(pl.col(col) == "").then(None).otherwise(pl.col(col)).cast(pl.Float64, strict=False)
    )


def _d(col: str) -> pl.Expr:
    return (
        pl.when(pl.col(col) == "")
        .then(None)
        .otherwise(pl.col(col))
        .str.to_date("%Y-%m-%d", strict=False)
    )


class BaostockAdapter:
    source = SOURCE

    def __init__(self, ctx: FetchContext, bs: Any | None = None) -> None:
        self.ctx = ctx
        self._bs = bs
        self._logged_in = False
        self._lock = threading.Lock()

    @property
    def bs(self) -> Any:
        if self._bs is None:
            import baostock  # noqa: PLC0415

            self._bs = baostock
        return self._bs

    def _ensure_login(self) -> None:
        with self._lock:
            if self._logged_in:
                return
            lg = self.bs.login()
            if getattr(lg, "error_code", "0") != "0":
                raise AdapterError(SOURCE, "login", f"{lg.error_code} {lg.error_msg}")
            self._logged_in = True

    def close(self) -> None:
        if self._logged_in:
            try:
                self.bs.logout()
            finally:
                self._logged_in = False

    def contract(self, endpoint: str) -> Any:
        return BAOSTOCK[endpoint]

    def _run(
        self,
        endpoint: str,
        params: dict[str, Any],
        day: dt.date,
        query: Callable[[], Any],
        std: Callable[[pl.DataFrame], pl.DataFrame],
    ) -> pl.DataFrame:
        cached = self.ctx.cache.get(SOURCE, endpoint, day, params)
        if cached is not None:
            out: pl.DataFrame = std(cached)
            BAOSTOCK[endpoint].validate(out, SOURCE, endpoint)
            return out

        def once() -> pl.DataFrame:
            self._ensure_login()
            self.ctx.limiter.wait()
            return _rows(query())

        raw = retry(once, source=SOURCE, endpoint=endpoint, backoff=self.ctx.rate.backoff_seconds)
        self.ctx.cache.put(
            SOURCE, endpoint, day, params, raw, {"source": SOURCE, "endpoint": endpoint}
        )
        result: pl.DataFrame = std(raw)
        BAOSTOCK[endpoint].validate(result, SOURCE, endpoint)
        return result

    def trade_dates(self, start: dt.date, end: dt.date, day: dt.date) -> pl.DataFrame:
        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                _d("calendar_date").alias("trade_date"),
                (pl.col("is_trading_day") == "1").alias("is_open"),
            )

        return self._run(
            "trade_dates",
            {"start": start.isoformat(), "end": end.isoformat()},
            day,
            lambda: self.bs.query_trade_dates(
                start_date=start.isoformat(), end_date=end.isoformat()
            ),
            std,
        )

    def kdata(self, code: str, start: dt.date, end: dt.date, day: dt.date) -> pl.DataFrame:
        """不复权日线；`turn` 与 `pctChg` 源为百分数。"""

        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("code").map_elements(normalize_code, return_dtype=pl.Utf8).alias("code"),
                _d("date").alias("trade_date"),
                _f("open").alias("open"),
                _f("high").alias("high"),
                _f("low").alias("low"),
                _f("close").alias("close"),
                _f("preclose").alias("pre_close"),
                _f("volume").alias("volume"),
                _f("amount").alias("amount"),
                (_f("turn") / 100.0).alias("turnover"),
                (_f("pctChg") / 100.0).alias("pct_chg"),
                (pl.col("tradestatus") == "0").alias("is_suspended"),
                (pl.col("isST") == "1").alias("is_st"),
            )

        return self._run(
            "kdata",
            {"code": code, "start": start.isoformat(), "end": end.isoformat()},
            day,
            lambda: self.bs.query_history_k_data_plus(
                code_to_baostock(code),
                K_FIELDS,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag="3",
            ),
            std,
        )

    def adjust_factor(self, code: str, start: dt.date, end: dt.date, day: dt.date) -> pl.DataFrame:
        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("code").map_elements(normalize_code, return_dtype=pl.Utf8).alias("code"),
                _d("dividOperateDate").alias("effective_date"),
                _f("backAdjustFactor").alias("adj_factor"),
            )

        return self._run(
            "adjust_factor",
            {"code": code, "start": start.isoformat(), "end": end.isoformat()},
            day,
            lambda: self.bs.query_adjust_factor(
                code=code_to_baostock(code), start_date=start.isoformat(), end_date=end.isoformat()
            ),
            std,
        )

    def stock_basic(self, day: dt.date) -> pl.DataFrame:
        """全部证券基础信息（含指数，`is_stock` 区分）。"""

        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("code").map_elements(normalize_code, return_dtype=pl.Utf8).alias("code"),
                pl.col("code_name").alias("name"),
                _d("ipoDate").alias("list_date"),
                _d("outDate").alias("delist_date"),
                (pl.col("type") == "1").alias("is_stock"),
                (pl.col("status") == "1").alias("is_listed"),
            )

        return self._run("stock_basic", {}, day, lambda: self.bs.query_stock_basic(), std)

    def all_stock(self, day: dt.date) -> pl.DataFrame:
        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("code").map_elements(normalize_code, return_dtype=pl.Utf8).alias("code"),
                pl.col("code_name").alias("name"),
                (pl.col("tradeStatus") == "1").alias("is_trading"),
            )

        return self._run(
            "all_stock",
            {"day": day.isoformat()},
            day,
            lambda: self.bs.query_all_stock(day=day.isoformat()),
            std,
        )

    def health(self, day: dt.date) -> dict[str, Any]:
        started = dt.datetime.now()
        try:
            df = self.trade_dates(day - dt.timedelta(days=10), day, day)
            ms = int((dt.datetime.now() - started).total_seconds() * 1000)
            return {
                "source": SOURCE,
                "endpoint": "trade_dates",
                "ok": True,
                "rows": df.height,
                "ms": ms,
            }
        except AdapterError as exc:
            ms = int((dt.datetime.now() - started).total_seconds() * 1000)
            return {
                "source": SOURCE,
                "endpoint": "trade_dates",
                "ok": False,
                "error": str(exc),
                "ms": ms,
            }
