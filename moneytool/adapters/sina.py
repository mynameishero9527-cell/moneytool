"""新浪财经：个股日频资金流历史（一次请求可取约 8 年），作为日频资金流的正式来源；
东财限流时替代盘中全市场资金流、全 A 快照与概念成分。

接口 `MoneyFlow.ssl_qsfx_lscjfb`（资金流向-历史成交分布），按日期倒序分页；字段口径见 contracts.SINA。
"""

from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import polars as pl
import requests

from moneytool.adapters.base import AdapterError, FetchContext, RateLimiter, retry
from moneytool.adapters.contracts import FLOW_COLS, SINA

SOURCE = "sina"
BASE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
MAX_ROWS = 2000
MAX_PAGES = 80
SPOT_PAGE = 100  # 行情节点接口每页上限
CONCEPT_CLASS_URL = "https://money.finance.sina.com.cn/q/view/newFLJK.php"
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


_A_SHARE_PREFIX = {"SH": ("60", "68"), "SZ": ("00", "30"), "BJ": ("4", "8", "92")}


def sina_to_code(symbol: str) -> str | None:
    """`sz000001` → `000001.SZ`；基金、债券、B 股等非 A 股返回 None。"""
    s = str(symbol).strip()
    ex, num = s[:2].upper(), s[2:]
    if len(num) != 6 or not num.isdigit() or not num.startswith(_A_SHARE_PREFIX.get(ex, ())):
        return None
    return f"{num}.{ex}"


def standardize_realtime(raw: pl.DataFrame) -> pl.DataFrame:
    """实时接口只给特大单（r0）与小单（r3）净额及四档净额合计，大单、中单合在一起。
    主力（特大 + 大单）按“特大单 + 大中单合计的一半”估算：抽样 200 只与当日日频正式值
    相关系数 0.99、方向一致 94%。收盘后由日频正式值覆盖。"""
    df = raw.with_columns(
        code=pl.col("symbol").map_elements(sina_to_code, return_dtype=pl.Utf8),
        net_super=_num("r0_net"),
        net_small=_num("r3_net"),
        _net=_num("netamount"),
        _amount=_num("amount"),
    ).filter(pl.col("code").is_not_null())
    mid = pl.col("_net") - pl.col("net_super") - pl.col("net_small")
    df = df.with_columns(net_large=mid * 0.5, net_medium=mid * 0.5).with_columns(
        net_main=pl.col("net_super") + pl.col("net_large")
    )
    ratio = {
        f"{k}_ratio": pl.when(pl.col("_amount") > 0)
        .then(pl.col(f"net_{k}") / pl.col("_amount"))
        .otherwise(None)
        for k in ("main", "super", "large", "medium", "small")
    }
    return (
        df.with_columns(**ratio)
        .select(
            "code",
            pl.col("name"),
            _num("trade").alias("close"),
            _num("changeratio").alias("pct_chg"),
            *[pl.col(c) for c in FLOW_COLS],
        )
        .unique("code", keep="first", maintain_order=True)
    )


def standardize_spot(raw: pl.DataFrame) -> pl.DataFrame:
    """市值源为万元，换手率、涨跌幅源为百分数。"""
    return (
        raw.select(
            pl.col("symbol").map_elements(sina_to_code, return_dtype=pl.Utf8).alias("code"),
            pl.col("name"),
            _num("trade").alias("close"),
            (_num("changepercent") / 100.0).alias("pct_chg"),
            _num("amount").alias("amount"),
            (_num("turnoverratio") / 100.0).alias("turnover"),
            (_num("mktcap") * 1e4).alias("total_mv"),
            (_num("nmc") * 1e4).alias("float_mv"),
        )
        .filter(pl.col("code").is_not_null())
        .unique("code", keep="first", maintain_order=True)
    )


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

    def _json(self, method: str, params: dict[str, Any], cls: str = "MoneyFlow") -> Any:
        r = self._get(
            f"{BASE}/{cls}.{method}",
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

    # ---- 东财限流时的替代：盘中资金流、全 A 快照、概念 ----

    def _pages(
        self, endpoint: str, method: str, params: dict[str, Any], *, cls: str, page_size: int
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            self.ctx.limiter.wait()
            data = self._json(method, {**params, "page": page, "num": page_size}, cls=cls)
            if not isinstance(data, list):
                raise AdapterError(SOURCE, endpoint, f"响应格式异常: {str(data)[:80]}")
            out.extend(data)
            if len(data) < page_size:
                break
            page += 1
        return out

    def _cached_rows(
        self, endpoint: str, day: dt.date, params: dict[str, Any], fetch: Any, *, cache: bool
    ) -> pl.DataFrame:
        cached = self.ctx.cache.get(SOURCE, endpoint, day, params) if cache else None
        if cached is not None:
            return cached

        def once() -> pl.DataFrame:
            rows = fetch()
            if not rows:
                raise AdapterError(SOURCE, endpoint, "空响应")
            keys = sorted({k for r in rows for k in r})
            return pl.DataFrame(
                [{k: (None if r.get(k) is None else str(r.get(k))) for k in keys} for r in rows],
                schema={k: pl.Utf8() for k in keys},
            )

        raw = retry(once, source=SOURCE, endpoint=endpoint, backoff=self.ctx.rate.backoff_seconds)
        if cache:
            self.ctx.cache.put(
                SOURCE, endpoint, day, params, raw, {"source": SOURCE, "endpoint": endpoint}
            )
        return raw

    def flow_rank_stock(self, day: dt.date, segment: str, *, cache: bool = True) -> pl.DataFrame:
        """`MoneyFlow.ssl_bkzj_ssggzj`：全市场今日累计资金流，每页 1000 只、约 7 页。"""
        endpoint = "flow_rank_stock"
        raw = self._cached_rows(
            endpoint,
            day,
            {"segment": segment},
            lambda: self._pages(
                endpoint,
                "ssl_bkzj_ssggzj",
                {"sort": "r0_net", "asc": 0, "bankuai": "", "shichang": ""},
                cls="MoneyFlow",
                page_size=1000,
            ),
            cache=cache,
        )
        out = standardize_realtime(raw)
        SINA[endpoint].validate(out, SOURCE, endpoint)
        return out

    def spot(self, day: dt.date) -> pl.DataFrame:
        """`Market_Center.getHQNodeData(node=hs_a)`：全 A 行情快照，每页 100 只，线程池并发取页。"""
        endpoint = "spot"

        def fetch() -> list[dict[str, Any]]:
            count = self._json("getHQNodeStockCount", {"node": "hs_a"}, cls="Market_Center")
            pages = min(MAX_PAGES, int(str(count).strip('"')) // SPOT_PAGE + 1)

            def one(page: int) -> list[dict[str, Any]]:
                self.ctx.limiter.wait()
                data = self._json(
                    "getHQNodeData",
                    {
                        "page": page,
                        "num": SPOT_PAGE,
                        "sort": "symbol",
                        "asc": 1,
                        "node": "hs_a",
                        "symbol": "",
                        "_s_r_a": "page",
                    },
                    cls="Market_Center",
                )
                return data if isinstance(data, list) else []

            with ThreadPoolExecutor(max_workers=4) as ex:
                return [r for rows in ex.map(one, range(1, pages + 1)) for r in rows]

        raw = self._cached_rows(endpoint, day, {}, fetch, cache=True)
        out = standardize_spot(raw)
        SINA[endpoint].validate(out, SOURCE, endpoint)
        return out

    def concept_list(self, day: dt.date) -> pl.DataFrame:
        """新浪概念板块列表（`newFLJK.php?param=class`，GBK 编码的 JS 对象）。"""
        endpoint = "concept_list"

        def fetch() -> list[dict[str, Any]]:
            self.ctx.limiter.wait()
            r = self._get(
                CONCEPT_CLASS_URL,
                params={"param": "class"},
                headers=HEADERS,
                timeout=self.ctx.rate.timeout_seconds,
            )
            r.raise_for_status()
            text = r.content.decode("gbk", errors="replace")
            try:
                obj = json.loads(text[text.index("{") : text.rindex("}") + 1])
            except ValueError as exc:
                raise AdapterError(SOURCE, endpoint, f"响应格式异常: {text[:80]}") from exc
            rows = []
            for value in obj.values():
                parts = str(value).split(",")
                if len(parts) > 5:
                    rows.append({"board_code": parts[0], "name": parts[1], "pct": parts[5]})
            return rows

        raw = self._cached_rows(endpoint, day, {}, fetch, cache=True)
        out = raw.select(
            pl.col("board_code"),
            pl.col("name"),
            (_num("pct") / 100.0).alias("pct_chg"),
            pl.lit(None, dtype=pl.Int64).alias("up_count"),
        )
        SINA[endpoint].validate(out, SOURCE, endpoint)
        return out

    def concept_cons(self, board_code: str, day: dt.date) -> pl.DataFrame:
        """`Market_Center.getHQNodeData(node=gn_xxx)`：概念成分。"""
        endpoint = "concept_cons"
        raw = self._cached_rows(
            endpoint,
            day,
            {"board": board_code},
            lambda: self._pages(
                endpoint,
                "getHQNodeData",
                {"sort": "symbol", "asc": 1, "node": board_code, "symbol": "", "_s_r_a": "page"},
                cls="Market_Center",
                page_size=SPOT_PAGE,
            ),
            cache=True,
        )
        out = (
            raw.select(
                pl.col("symbol").map_elements(sina_to_code, return_dtype=pl.Utf8).alias("code"),
                pl.col("name"),
            )
            .filter(pl.col("code").is_not_null())
            .unique("code", keep="first", maintain_order=True)
        )
        SINA[endpoint].validate(out, SOURCE, endpoint)
        return out

    def health_realtime(self, day: dt.date) -> dict[str, Any]:
        started = dt.datetime.now()
        endpoint = "flow_rank_stock"
        try:
            self.ctx.limiter.wait()
            data = self._json(
                "ssl_bkzj_ssggzj",
                {"page": 1, "num": 50, "sort": "r0_net", "asc": 0, "bankuai": "", "shichang": ""},
            )
            ok = isinstance(data, list) and len(data) > 0
            return {
                "source": "sina实时",
                "endpoint": endpoint,
                "ok": ok,
                "rows": len(data) if isinstance(data, list) else 0,
                "ms": _ms(started),
                **({} if ok else {"error": "空响应"}),
            }
        except (AdapterError, requests.RequestException) as exc:
            return {
                "source": "sina实时",
                "endpoint": endpoint,
                "ok": False,
                "error": str(exc)[:200],
                "ms": _ms(started),
            }

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
