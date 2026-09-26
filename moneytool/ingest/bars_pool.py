"""日线多进程回补。Baostock 全进程只有一个 socket、不支持并发，所以用进程池：
每个子进程各自登录一次、复用会话，按各自的限速拉取；主进程只收结果、写库。

Windows 用 spawn 启动子进程，子进程重新导入本模块，只做数据拉取，不碰数据库。
"""

from __future__ import annotations

import datetime as dt
import multiprocessing as mp
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any

from moneytool.adapters.base import AdapterError
from moneytool.ingest.bars import ADJ_FROM, BarsResult, Ranges
from moneytool.logging import get_logger

log = get_logger(__name__)

_worker_adapter: Any = None


def _init_worker(data_dir: str) -> None:
    global _worker_adapter  # noqa: PLW0603
    from moneytool.adapters.registry import build_adapters  # noqa: PLC0415
    from moneytool.config import load_settings  # noqa: PLC0415
    from moneytool.logging import setup_logging  # noqa: PLC0415
    from moneytool.network import apply_network  # noqa: PLC0415

    settings = load_settings(Path(data_dir))
    setup_logging(settings.logs_dir, retention_days=settings.data.log_retention_days)
    apply_network(settings)
    _worker_adapter = build_adapters(settings).baostock


def _fetch_one(code: str, start: dt.date, end: dt.date, day: dt.date) -> BarsResult:
    try:
        k = _worker_adapter.kdata(code, start, end, day)
        adj = _worker_adapter.adjust_factor(code, ADJ_FROM, day, day)
    except AdapterError as exc:
        return code, exc
    except Exception as exc:  # 子进程里的意外错误也按单只失败处理，不拖垮整批
        return code, AdapterError("baostock", "kdata", f"{type(exc).__name__}: {exc}")
    return code, (k, adj)


class BarsPool:
    """常驻进程池；首次使用时启动，`close()` 时结束子进程。"""

    def __init__(self, data_dir: Path, workers: int) -> None:
        self.data_dir = data_dir
        self.workers = workers
        self._pool: ProcessPoolExecutor | None = None

    def _ensure(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                self.workers,
                mp_context=mp.get_context("spawn"),
                initializer=_init_worker,
                initargs=(str(self.data_dir),),
            )
            log.info("bars_pool_started", workers=self.workers)
        return self._pool

    def fetch(
        self,
        codes: list[str],
        *,
        start: dt.date,
        end: dt.date,
        day: dt.date,
        ranges: Ranges | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_item: Callable[[str], None] | None = None,
    ) -> list[BarsResult]:
        """并发拉一批；同时在途的任务数等于进程数，停止时不再提交新任务。
        `ranges` 给出各股自己的区间（分层回补），缺省用 [start, end]。"""
        pool = self._ensure()
        out: list[BarsResult] = []
        todo = list(codes)
        running: set[Future[BarsResult]] = set()
        try:
            while todo or running:
                while todo and len(running) < self.workers and not (should_stop and should_stop()):
                    code = todo.pop(0)
                    a, b = (ranges or {}).get(code, (start, end))
                    running.add(pool.submit(_fetch_one, code, a, b, day))
                if not running:
                    break
                finished, running = wait(running, timeout=5, return_when=FIRST_COMPLETED)
                for fut in finished:
                    done_code, res = fut.result()
                    out.append((done_code, res))
                    if on_item:
                        on_item(done_code)
        except (
            Exception
        ) as exc:  # 子进程崩溃（BrokenProcessPool 等）：重建进程池，本批未完成的留给下一批
            log.error("bars_pool_failed", error=f"{type(exc).__name__}: {exc}")
            self.close()
        return out

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
