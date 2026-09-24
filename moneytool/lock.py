"""写库任务互斥：文件锁 `~/.moneytool/.lock`。架构 4.1.3。"""

from __future__ import annotations

import datetime as dt
import json
import os
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


@contextmanager
def write_lock(lock_path: Path, owner: str, timeout: float = 60.0) -> Iterator[None]:
    """持锁执行写库任务。超时抛 `LockBusyError` 并带持有者信息。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path))
    try:
        lock.acquire(timeout=timeout)
    except Timeout as exc:
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


def force_unlock(lock_path: Path) -> bool:
    """`doctor --unlock`：清理残留锁文件。持有进程仍在时拒绝。"""
    holder = read_holder(lock_path)
    pid = holder.get("pid")
    if pid and _pid_alive(int(pid)):
        return False
    _holder_path(lock_path).unlink(missing_ok=True)
    lock_path.unlink(missing_ok=True)
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
