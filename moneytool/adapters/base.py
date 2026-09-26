"""适配器公共设施：错误类型、契约、原始缓存、令牌桶、重试、代码标准化（data-adapter-akshare skill）。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from moneytool.config import SourceRateLimit
from moneytool.logging import get_logger

log = get_logger(__name__)


class AdapterError(RuntimeError):
    """网络 / 解析失败，已重试仍失败。调用方写 data_quality(status=missing)。"""

    def __init__(self, source: str, endpoint: str, reason: str) -> None:
        super().__init__(f"{source}.{endpoint}: {reason}")
        self.source = source
        self.endpoint = endpoint
        self.reason = reason

    def __reduce__(self) -> tuple[Any, tuple[str, str, str]]:
        # 多进程回补时跨进程传回；默认的 pickle 只带消息文本，还原会缺参数
        return (self.__class__, (self.source, self.endpoint, self.reason))


class CaptchaError(AdapterError):
    """东财滑块验证 / 反爬：响应非 JSON 或为空。当日停止该源个股级请求。"""


class SourceBlockedError(CaptchaError):
    """数据源疑似封禁本机 IP，冷却期内不发请求（不重试，回补遇到即暂停）。"""


BLOCK_MARKERS = ("RemoteDisconnected", "Connection aborted", "502", "Bad Gateway")


def looks_blocked(exc: BaseException) -> bool:
    """连接被对端直接断开 / 网关 502：东财封 IP 时的典型表现。"""
    text = f"{type(exc).__name__}: {exc}"
    return any(m in text for m in BLOCK_MARKERS)


class CircuitBreaker:
    """连续 `threshold` 次疑似封禁即熔断 `cooldown` 秒，再次熔断时长翻倍到 `max_cooldown`；
    成功一次复位。进程内所有线程共享，冷却期内请求直接失败、不触网，避免越封越久。"""

    def __init__(
        self,
        threshold: int = 2,
        cooldown: float = 900.0,
        max_cooldown: float = 7200.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.max_cooldown = max_cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._trips = 0
        self._open_until = 0.0

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - self._clock())

    def succeeded(self) -> None:
        with self._lock:
            self._failures = 0
            self._trips = 0

    def failed(self) -> float:
        """记一次疑似封禁；熔断时返回冷却秒数，否则 0。"""
        with self._lock:
            self._failures += 1
            if self._failures < self.threshold:
                return 0.0
            self._failures = 0
            span = float(min(self.cooldown * 2**self._trips, self.max_cooldown))
            self._trips += 1
            self._open_until = self._clock() + span
            return span


class ContractError(AdapterError):
    """响应列名或类型与契约不符。原始响应已保留。"""


@dataclass(frozen=True)
class Contract:
    columns: dict[str, pl.DataType]
    required_non_null: tuple[str, ...] = ()
    min_rows: int = 1
    unit_notes: str = ""

    def validate(self, df: pl.DataFrame, source: str, endpoint: str) -> None:
        missing = [c for c in self.columns if c not in df.columns]
        if missing:
            raise ContractError(source, endpoint, f"缺列 {missing}；实际 {df.columns}")
        if df.height < self.min_rows:
            raise ContractError(source, endpoint, f"行数 {df.height} < 下限 {self.min_rows}")
        for c in self.required_non_null:
            if df[c].null_count() > 0:
                raise ContractError(source, endpoint, f"列 {c} 含 null")
        for c, dtype in self.columns.items():
            if df.schema[c] != dtype:
                raise ContractError(source, endpoint, f"列 {c} 类型 {df.schema[c]} ≠ {dtype}")


class RateLimiter:
    """单源令牌桶：两次请求间隔 ≥ min_interval。线程安全。"""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            gap = self._last + self.min_interval - now
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


class AdaptiveLimiter(RateLimiter):
    """多线程共享的限速器：成功一次间隔缩 10%（不低于 floor），失败一次翻倍（不高于 ceiling）。"""

    def __init__(self, floor: float, ceiling: float) -> None:
        super().__init__(floor)
        self.floor = floor
        self.ceiling = max(ceiling, floor)

    def succeeded(self) -> None:
        with self._lock:
            self.min_interval = max(self.floor, self.min_interval * 0.9)

    def failed(self) -> None:
        with self._lock:
            self.min_interval = min(self.ceiling, max(self.min_interval, 0.5) * 2)


@dataclass
class RawCache:
    """原始响应只追加落盘：raw/{source}/{endpoint}/{date}/{params_hash}.parquet + _meta.json。"""

    root: Path

    def path(self, source: str, endpoint: str, day: dt.date, params: dict[str, Any]) -> Path:
        h = hashlib.sha1(
            json.dumps(params, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:12]
        return self.root / source / endpoint / day.isoformat() / f"{h}.parquet"

    def get(
        self, source: str, endpoint: str, day: dt.date, params: dict[str, Any]
    ) -> pl.DataFrame | None:
        p = self.path(source, endpoint, day, params)
        if not p.exists():
            return None
        return pl.read_parquet(p)

    def put(
        self,
        source: str,
        endpoint: str,
        day: dt.date,
        params: dict[str, Any],
        df: pl.DataFrame,
        meta: dict[str, Any],
    ) -> Path:
        p = self.path(source, endpoint, day, params)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(p)
        p.with_name(p.stem + "_meta.json").write_text(
            json.dumps(
                {
                    **meta,
                    "params": params,
                    "rows": df.height,
                    "saved_at": dt.datetime.now().isoformat(),
                },
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        return p

    def prune(self, retention_days: int, today: dt.date | None = None) -> int:
        """删除超过保留期的日期目录，返回删除的文件数。"""
        today = today or dt.date.today()
        cutoff = today - dt.timedelta(days=retention_days)
        removed = 0
        for source_dir in self.root.iterdir() if self.root.exists() else []:
            for endpoint_dir in source_dir.iterdir():
                for day_dir in endpoint_dir.iterdir():
                    try:
                        day = dt.date.fromisoformat(day_dir.name)
                    except ValueError:
                        continue
                    if day < cutoff:
                        for f in day_dir.iterdir():
                            f.unlink()
                            removed += 1
                        day_dir.rmdir()
        return removed


@dataclass
class FetchContext:
    """一次拉取需要的运行时依赖，由 app 组装后注入适配器。"""

    cache: RawCache
    rate: SourceRateLimit
    limiter: RateLimiter
    per_stock_limiter: RateLimiter
    captcha_tripped: dict[str, dt.date] = field(default_factory=dict)

    def captcha_blocked(self, source: str, today: dt.date) -> bool:
        return self.captcha_tripped.get(source) == today


def retry(
    fn: Callable[[], pl.DataFrame],
    *,
    source: str,
    endpoint: str,
    backoff: tuple[float, ...],
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    """按 backoff 退避重试；CaptchaError 不重试直接抛。"""
    last: Exception | None = None
    for attempt, wait in enumerate((*backoff, None)):
        try:
            return fn()
        except CaptchaError:
            raise
        except AdapterError as exc:
            last = exc
        except Exception as exc:  # AkShare 抛的多是 requests / json 异常
            last = AdapterError(source, endpoint, f"{type(exc).__name__}: {exc}")
        if wait is None:
            break
        log.warning(
            "adapter_retry",
            source=source,
            endpoint=endpoint,
            attempt=attempt + 1,
            wait=wait,
            error=str(last),
        )
        sleep(wait)
    assert last is not None
    raise last


def normalize_code(raw: str | int) -> str:
    """6 位代码 → `600000.SH` / `000001.SZ` / `430047.BJ`。已带后缀或 baostock 前缀的也接受。"""
    s = str(raw).strip().upper()
    if "." in s:
        a, b = s.split(".", 1)
        if a in ("SH", "SZ", "BJ"):
            return f"{b}.{a}"
        return f"{a}.{b}"
    s = s.zfill(6)
    if s.startswith(("6", "9")):
        return f"{s}.SH"
    if s.startswith(("0", "2", "3")):
        return f"{s}.SZ"
    if s.startswith(("4", "8")):
        return f"{s}.BJ"
    raise ValueError(f"无法识别的代码: {raw}")


def code_to_baostock(code: str) -> str:
    num, ex = code.split(".")
    return f"{ex.lower()}.{num}"


def code_to_em_market(code: str) -> str:
    return code.split(".")[1].lower()


def is_captcha_like(exc: Exception) -> bool:
    """东财被拦截时 AkShare 常抛 JSONDecodeError 或空响应 KeyError。"""
    name = type(exc).__name__
    return name in ("JSONDecodeError", "KeyError") or "Expecting value" in str(exc)
