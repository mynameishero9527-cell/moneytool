"""写锁进程内公平排队：逐日补算“放锁即再抢”时，其他线程不应等满超时。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from moneytool.lock import LockBusyError, write_lock


def test_waiter_not_starved_by_tight_reacquire(tmp_path: Path) -> None:
    lock_path = tmp_path / ".lock"
    stop = threading.Event()

    def hog() -> None:
        while not stop.is_set():
            with write_lock(lock_path, owner="catchup", timeout=5):
                time.sleep(0.02)

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    time.sleep(0.05)
    started = time.monotonic()
    try:
        with write_lock(lock_path, owner="backfill", timeout=1):
            waited = time.monotonic() - started
    finally:
        stop.set()
        t.join(timeout=5)
    assert waited < 0.5


def test_timeout_leaves_queue_usable(tmp_path: Path) -> None:
    lock_path = tmp_path / ".lock"
    with (
        write_lock(lock_path, owner="other", timeout=1),
        pytest.raises(LockBusyError, match="other"),
        write_lock(lock_path, owner="me", timeout=0.1),
    ):
        pass
    with write_lock(lock_path, owner="after", timeout=0.5):
        pass
