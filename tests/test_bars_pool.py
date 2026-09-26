"""日线多进程回补：真实 Baostock，多个子进程各自登录并发拉取。"""

from __future__ import annotations

import datetime as dt
import multiprocessing as mp
import pickle
from pathlib import Path

import pytest

from moneytool.adapters.base import AdapterError, CaptchaError
from moneytool.config import load_settings
from moneytool.ingest.bars_pool import BarsPool


def test_shared_pace_spaces_requests() -> None:
    import threading
    import time

    from moneytool.ingest.bars_pool import SharedPace

    ctx = mp.get_context("spawn")
    pace = SharedPace(ctx.Lock(), ctx.Value("d", 0.0), 0.05)
    starts: list[float] = []
    guard = threading.Lock()

    def once() -> None:
        pace.wait()
        with guard:
            starts.append(time.monotonic())

    threads = [threading.Thread(target=once) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    gaps = [b - a for a, b in zip(sorted(starts), sorted(starts)[1:], strict=False)]
    assert min(gaps) >= 0.04


def test_adapter_errors_survive_pickle() -> None:
    for exc in (AdapterError("baostock", "kdata", "超时"), CaptchaError("eastmoney", "x", "验证")):
        back = pickle.loads(pickle.dumps(exc))
        assert type(back) is type(exc)
        assert (back.source, back.endpoint, back.reason) == (exc.source, exc.endpoint, exc.reason)


@pytest.mark.network
def test_pool_fetches_in_parallel(tmp_path: Path) -> None:
    load_settings(tmp_path).ensure_dirs()
    pool = BarsPool(tmp_path, 2)
    ticks: list[str] = []
    day = dt.date(2026, 9, 26)
    try:
        out = pool.fetch(
            ["600000.SH", "000001.SZ", "600036.SH"],
            start=day - dt.timedelta(days=30),
            end=day,
            day=day,
            on_item=ticks.append,
        )
    finally:
        pool.close()
    assert sorted(c for c, _ in out) == ["000001.SZ", "600000.SH", "600036.SH"]
    assert sorted(ticks) == sorted(c for c, _ in out)
    ok = [r for _, r in out if not isinstance(r, AdapterError)]
    assert ok and all(k.height > 0 for k, _ in ok)
