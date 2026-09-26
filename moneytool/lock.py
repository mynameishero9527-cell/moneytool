"""写库任务互斥：文件锁 `<数据目录>/.lock`。架构 4.1.3。"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout


class LockBusyError(RuntimeError):
    def __init__(self, holder: dict[str, str]) -> None:
        who = holder.get("owner", "未知")
        since = holder.get("since", "未知")
        super().__init__(
            f"写库锁被占用：{who}，自 {since}。若持有进程已退出，运行 moneytool doctor --unlock"
        )
        self.holder = holder


def _holder_path(lock_path: Path) -> Path:
    return lock_path.with_suffix(".holder.json")


def read_holder(lock_path: Path) -> dict[str, str]:
    p = _holder_path(lock_path)
    if not p.exists():
        return {}
    try:
        return dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


class _FairGate:
    """进程内按到达顺序排队。文件锁本身不保证公平：连续逐日补算这类“放锁即再抢”的任务
    会让其他线程等满超时也抢不到。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._queue: deque[object] = deque()
        self._held = False

    def acquire(self, timeout: float) -> bool:
        token = object()
        deadline = time.monotonic() + timeout
        with self._cond:
            self._queue.append(token)
            while self._held or self._queue[0] is not token:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._queue.remove(token)
                    self._cond.notify_all()
                    return False
                self._cond.wait(remaining)
            self._queue.popleft()
            self._held = True
            return True

    def release(self) -> None:
        with self._cond:
            self._held = False
            self._cond.notify_all()


_gates: dict[str, _FairGate] = {}
_gates_guard = threading.Lock()


def _gate(lock_path: Path) -> _FairGate:
    key = str(lock_path.resolve())
    with _gates_guard:
        return _gates.setdefault(key, _FairGate())


@contextmanager
def write_lock(lock_path: Path, owner: str, timeout: float = 60.0) -> Iterator[None]:
    """持锁执行写库任务。进程内先按到达顺序排队，再用文件锁与其他进程互斥；
    超时抛 `LockBusyError` 并带持有者信息。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    gate = _gate(lock_path)
    deadline = time.monotonic() + timeout
    if not gate.acquire(timeout):
        raise LockBusyError(read_holder(lock_path))
    lock = FileLock(str(lock_path))
    try:
        lock.acquire(timeout=max(deadline - time.monotonic(), 0.05))
    except Timeout as exc:
        gate.release()
        raise LockBusyError(read_holder(lock_path)) from exc
    holder = _holder_path(lock_path)
    try:
        holder.write_text(
            json.dumps(
                {
                    "owner": owner,
                    "pid": str(os.getpid()),
                    "since": dt.datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        yield
    finally:
        holder.unlink(missing_ok=True)
        lock.release()
        gate.release()


def force_unlock(lock_path: Path) -> bool:
    """`doctor --unlock`：清理残留锁文件。持有进程仍在时拒绝。"""
    holder = read_holder(lock_path)
    pid = holder.get("pid")
    if pid and pid_alive(int(pid)):
        return False
    _holder_path(lock_path).unlink(missing_ok=True)
    lock_path.unlink(missing_ok=True)
    return True


def pid_alive(pid: int) -> bool:
    """进程是否仍在运行。Windows 上 `os.kill(pid, 0)` 会直接结束目标进程，必须走 WinAPI。"""
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    import ctypes  # noqa: PLC0415

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return bool(getattr(ctypes, "get_last_error")() == error_access_denied)  # noqa: B009
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
