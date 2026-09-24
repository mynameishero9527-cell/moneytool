"""东方财富（经 AkShare）：资金流唯一口径 + 概念成分 + 行情快照。列名契约见 contracts.EASTMONEY。

调用纪律（架构 3.3）：资金流用全市场排名接口一次拉全部，每分段 3 次；个股级接口只在回补与夜间用。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

import polars as pl

from moneytool.adapters.base import (
    AdapterError,
    CaptchaError,
    FetchContext,
    code_to_em_market,
    is_captcha_like,
    normalize_code,
    retry,
)
from moneytool.adapters.contracts import EASTMONEY
from moneytool.logging import get_logger

SOURCE = "eastmoney"
log = get_logger(__name__)

_FLOW_RENAME_TODAY = {
    "今日主力净流入-净额": "net_main",
    "今日超大单净流入-净额": "net_super",
    "今日大单净流入-净额": "net_large",
    "今日中单净流入-净额": "net_medium",
    "今日小单净流入-净额": "net_small",
    "今日主力净流入-净占比": "main_ratio",
    "今日超大单净流入-净占比": "super_ratio",
    "今日大单净流入-净占比": "large_ratio",
    "今日中单净流入-净占比": "medium_ratio",
    "今日小单净流入-净占比": "small_ratio",
}
_FLOW_RENAME_HIST = {k.removeprefix("今日"): v for k, v in _FLOW_RENAME_TODAY.items()}
_RATIO_COLS = ("main_ratio", "super_ratio", "large_ratio", "medium_ratio", "small_ratio")
_NET_COLS = ("net_main", "net_super", "net_large", "net_medium", "net_small")


def _pdf(df: Any) -> pl.DataFrame:
    return pl.from_pandas(df)


def _pct(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Float64, strict=False) / 100.0


def _num(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Float64, strict=False)


def _flow_std(df: pl.DataFrame, rename: dict[str, str]) -> pl.DataFrame:
    df = df.rename({k: v for k, v in rename.items() if k in df.columns})
    exprs = [_num(c).alias(c) for c in _NET_COLS if c in df.columns]
    exprs += [_pct(c).alias(c) for c in _RATIO_COLS if c in df.columns]
    return df.with_columns(exprs)


class EastmoneyAdapter:
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
        return EASTMONEY[endpoint]

    # ---- 内部 ----

    def _fetch(
        self,
        endpoint: str,
        params: dict[str, Any],
        call: Callable[[], Any],
        standardize: Callable[[pl.DataFrame], pl.DataFrame],
        *,
        day: dt.date,
        per_stock: bool = False,
        cache: bool = True,
    ) -> pl.DataFrame:
        if cache:
            cached = self.ctx.cache.get(SOURCE, endpoint, day, params)
            if cached is not None:
                return standardize(cached)
        if per_stock and self.ctx.captcha_blocked(SOURCE, day):
            raise CaptchaError(SOURCE, endpoint, "当日已触发验证，个股级请求暂停")

        def once() -> pl.DataFrame:
            (self.ctx.per_stock_limiter if per_stock else self.ctx.limiter).wait()
            try:
                raw = _pdf(call())
            except Exception as exc:
                if is_captcha_like(exc):
                    self.ctx.captcha_tripped[SOURCE] = day
                    raise CaptchaError(
                        SOURCE, endpoint, f"疑似验证拦截: {type(exc).__name__}"
                    ) from exc
                raise
            if raw.is_empty():
                raise AdapterError(SOURCE, endpoint, "空响应")
            return raw

        raw = retry(once, source=SOURCE, endpoint=endpoint, backoff=self.ctx.rate.backoff_seconds)
        self.ctx.cache.put(
            SOURCE, endpoint, day, params, raw, {"source": SOURCE, "endpoint": endpoint}
        )
        out = standardize(raw)
        EASTMONEY[endpoint].validate(out, SOURCE, endpoint)
        return out

    # ---- 资金流：今日实时（分段用，不走缓存） ----

    def flow_rank_stock(self, day: dt.date, segment: str) -> pl.DataFrame:
        """`stock_individual_fund_flow_rank(indicator="今日")`：全市场今日累计资金流。"""

        def std(df: pl.DataFrame) -> pl.DataFrame:
            df = _flow_std(df, _FLOW_RENAME_TODAY)
            return df.select(
                pl.col("代码").map_elements(normalize_code, return_dtype=pl.Utf8).alias("code"),
                pl.col("名称").cast(pl.Utf8).alias("name"),
                _num("最新价").alias("close"),
                _pct("今日涨跌幅").alias("pct_chg"),
                *[pl.col(c) for c in (*_NET_COLS, *_RATIO_COLS)],
            ).filter(pl.col("code").is_not_null())

        return self._fetch(
            "flow_rank_stock",
            {"indicator": "今日", "segment": segment},
            lambda: self.ak.stock_individual_fund_flow_rank(indicator="今日"),
            std,
            day=day,
        )

    def flow_rank_sector(self, day: dt.date, segment: str, sector_type: str) -> pl.DataFrame:
        """`stock_sector_fund_flow_rank(indicator="今日", sector_type=...)`；`sector_type` ∈ 行业资金流 / 概念资金流。"""

        def std(df: pl.DataFrame) -> pl.DataFrame:
            df = _flow_std(df, _FLOW_RENAME_TODAY)
            return df.select(
                pl.col("名称").cast(pl.Utf8).alias("name"),
                _pct("今日涨跌幅").alias("pct_chg"),
                *[pl.col(c) for c in (*_NET_COLS, *_RATIO_COLS)],
                pl.col("今日主力净流入最大股").cast(pl.Utf8).alias("top_stock"),
            )

        return self._fetch(
            "flow_rank_sector",
            {"indicator": "今日", "sector_type": sector_type, "segment": segment},
            lambda: self.ak.stock_sector_fund_flow_rank(indicator="今日", sector_type=sector_type),
            std,
            day=day,
        )

    # ---- 资金流：日频正式值 ----

    def flow_daily_stock(self, code: str, day: dt.date) -> pl.DataFrame:
        """`stock_individual_fund_flow(stock, market)`：该股全部历史日频。个股级，限速 5 秒。"""
        num, _ = code.split(".")

        def std(df: pl.DataFrame) -> pl.DataFrame:
            df = _flow_std(df, _FLOW_RENAME_HIST)
            return df.select(
                pl.col("日期")
                .cast(pl.Utf8)
                .str.to_date("%Y-%m-%d", strict=False)
                .alias("trade_date"),
                _num("收盘价").alias("close"),
                _pct("涨跌幅").alias("pct_chg"),
                *[pl.col(c) for c in (*_NET_COLS, *_RATIO_COLS)],
            ).filter(pl.col("trade_date").is_not_null())

        return self._fetch(
            "flow_daily_stock",
            {"code": code},
            lambda: self.ak.stock_individual_fund_flow(stock=num, market=code_to_em_market(code)),
            std,
            day=day,
            per_stock=True,
        )

    def flow_daily_sector(self, name: str, day: dt.date) -> pl.DataFrame:
        """`stock_sector_fund_flow_hist(symbol=板块名)`：东财板块（行业 / 概念）日频历史。"""

        def std(df: pl.DataFrame) -> pl.DataFrame:
            df = _flow_std(df, _FLOW_RENAME_HIST)
            return df.select(
                pl.col("日期")
                .cast(pl.Utf8)
                .str.to_date("%Y-%m-%d", strict=False)
                .alias("trade_date"),
                *[pl.col(c) for c in (*_NET_COLS, *_RATIO_COLS)],
            ).filter(pl.col("trade_date").is_not_null())

        return self._fetch(
            "flow_daily_sector",
            {"name": name},
            lambda: self.ak.stock_sector_fund_flow_hist(symbol=name),
            std,
            day=day,
            per_stock=True,
        )

    def flow_market(self, day: dt.date) -> pl.DataFrame:
        """`stock_market_fund_flow()`：大盘日频资金流。"""

        def std(df: pl.DataFrame) -> pl.DataFrame:
            df = _flow_std(df, _FLOW_RENAME_HIST)
            return df.select(
                pl.col("日期")
                .cast(pl.Utf8)
                .str.to_date("%Y-%m-%d", strict=False)
                .alias("trade_date"),
                *[pl.col(c) for c in (*_NET_COLS, *_RATIO_COLS)],
                _num("上证-收盘价").alias("sh_close"),
                _pct("上证-涨跌幅").alias("sh_pct_chg"),
            ).filter(pl.col("trade_date").is_not_null())

        return self._fetch(
            "flow_market", {}, lambda: self.ak.stock_market_fund_flow(), std, day=day
        )

    # ---- 概念 ----

    def concept_list(self, day: dt.date) -> pl.DataFrame:
        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("板块代码").cast(pl.Utf8).alias("board_code"),
                pl.col("板块名称").cast(pl.Utf8).alias("name"),
                _pct("涨跌幅").alias("pct_chg"),
                pl.col("上涨家数").cast(pl.Int64, strict=False).alias("up_count"),
            )

        return self._fetch(
            "concept_list", {}, lambda: self.ak.stock_board_concept_name_em(), std, day=day
        )

    def concept_cons(self, name: str, day: dt.date) -> pl.DataFrame:
        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("代码").map_elements(normalize_code, return_dtype=pl.Utf8).alias("code"),
                pl.col("名称").cast(pl.Utf8).alias("name"),
            )

        return self._fetch(
            "concept_cons",
            {"name": name},
            lambda: self.ak.stock_board_concept_cons_em(symbol=name),
            std,
            day=day,
            per_stock=True,
        )

    # ---- 行情 ----

    def spot(self, day: dt.date) -> pl.DataFrame:
        """`stock_zh_a_spot_em()`：全 A 实时快照，用于证券表、流通市值、名称与 ST 标记。"""

        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("代码").map_elements(normalize_code, return_dtype=pl.Utf8).alias("code"),
                pl.col("名称").cast(pl.Utf8).alias("name"),
                _num("最新价").alias("close"),
                _pct("涨跌幅").alias("pct_chg"),
                _num("成交额").alias("amount"),
                _pct("换手率").alias("turnover"),
                _num("总市值").alias("total_mv"),
                _num("流通市值").alias("float_mv"),
            )

        return self._fetch("spot", {}, lambda: self.ak.stock_zh_a_spot_em(), std, day=day)

    def hist(self, code: str, start: dt.date, end: dt.date, day: dt.date) -> pl.DataFrame:
        """`stock_zh_a_hist`：不复权日线（Baostock 的备选）。"""
        num, _ = code.split(".")

        def std(df: pl.DataFrame) -> pl.DataFrame:
            return df.select(
                pl.col("日期")
                .cast(pl.Utf8)
                .str.to_date("%Y-%m-%d", strict=False)
                .alias("trade_date"),
                _num("开盘").alias("open"),
                _num("收盘").alias("close"),
                _num("最高").alias("high"),
                _num("最低").alias("low"),
                _num("成交量").alias("volume"),
                _num("成交额").alias("amount"),
                _pct("涨跌幅").alias("pct_chg"),
                _pct("换手率").alias("turnover"),
            )

        return self._fetch(
            "hist",
            {"code": code, "start": start.isoformat(), "end": end.isoformat()},
            lambda: self.ak.stock_zh_a_hist(
                symbol=num,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="",
            ),
            std,
            day=day,
            per_stock=True,
        )

    def health(self, day: dt.date) -> dict[str, Any]:
        """doctor 用：拉一次最小请求并跑契约。"""
        started = dt.datetime.now()
        try:
            df = self.flow_market(day)
            return {
                "source": SOURCE,
                "endpoint": "flow_market",
                "ok": True,
                "rows": df.height,
                "ms": _ms(started),
            }
        except AdapterError as exc:
            return {
                "source": SOURCE,
                "endpoint": "flow_market",
                "ok": False,
                "error": str(exc),
                "ms": _ms(started),
            }


def _ms(started: dt.datetime) -> int:
    return int((dt.datetime.now() - started).total_seconds() * 1000)
