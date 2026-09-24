"""DuckDB 连接管理：进程内唯一写连接（调度线程持有）+ 只读连接（API 用）。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb


class Database:
    """一个 DuckDB 文件的读写入口。

    写连接全进程唯一，所有写操作通过 `write()` 上下文串行；读连接每次新开 `read_only=True`。
    `:memory:` 路径用于测试，此时读写共用同一连接（内存库无法只读再开）。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._is_memory = self.path == ":memory:"
        self._rw = duckdb.connect(self.path)
        self._write_lock = threading.RLock()

    @property
    def rw(self) -> duckdb.DuckDBPyConnection:
        return self._rw

    @contextmanager
    def write(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """串行化写操作。任务函数在此上下文内写库。"""
        with self._write_lock:
            yield self._rw

    @contextmanager
    def read(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """读连接：写连接的独立 cursor（各自事务、可跨线程；MVCC 下读不阻塞写）。

        DuckDB 不允许同一进程对同一文件再开 `read_only=True` 连接，因此"只读"由约定保证：
        API 层只经这里取连接，且不执行写语句（写一律走 `write()`）。
        """
        cur = self._rw.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def checkpoint(self) -> None:
        with self._write_lock:
            self._rw.execute("CHECKPOINT")

    def close(self) -> None:
        with self._write_lock:
            if not self._is_memory:
                self._rw.execute("CHECKPOINT")
            self._rw.close()
