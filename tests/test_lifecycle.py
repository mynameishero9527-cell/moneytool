"""启停记录与 doctor 的上次运行判断。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from moneytool import lifecycle
from moneytool.diagnose import describe_last_run


def _logs(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("moneytool-*.log"))


def test_no_records(tmp_path: Path) -> None:
    (tmp_path / "moneytool-2026-09-26.log").write_text("plain line\n", encoding="utf-8")
    text, problem = describe_last_run(_logs(tmp_path))
    assert "没有启停记录" in text and problem == ""


def test_normal_stop(tmp_path: Path) -> None:
    lifecycle.record(tmp_path, lifecycle.STARTED, version="0.1.0")
    lifecycle.record(tmp_path, lifecycle.STOPPED)
    text, problem = describe_last_run(_logs(tmp_path))
    assert "正常退出" in text and problem == ""


def test_started_without_exit(tmp_path: Path) -> None:
    lifecycle.record(tmp_path, lifecycle.STARTED)
    text, problem = describe_last_run(_logs(tmp_path))
    assert "没有退出记录" in text and problem == ""


def test_startup_crash_is_reported(tmp_path: Path) -> None:
    lifecycle.record(tmp_path, lifecycle.STARTED)
    lifecycle.record(tmp_path, lifecycle.STOPPED)
    try:
        raise ValueError("config.toml 第 3 行格式错误")
    except ValueError as exc:
        lifecycle.record_crash(tmp_path, exc, "startup")
    text, problem = describe_last_run(_logs(tmp_path))
    assert "启动时崩溃" in text and "config.toml 第 3 行格式错误" in problem
    assert "正常退出" not in text


def test_run_command_records_startup_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from moneytool.__main__ import app

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("数据库文件损坏")

    monkeypatch.setattr("moneytool.app.build_context", boom)
    result = CliRunner().invoke(app, ["run", "--data-dir", str(tmp_path), "--no-browser"])
    assert result.exit_code != 0
    _, problem = describe_last_run(_logs(tmp_path / "logs"))
    assert "数据库文件损坏" in problem


def test_events_pair_by_process(tmp_path: Path) -> None:
    import json

    lines = [
        {"event": "app_started", "pid": 1, "timestamp": "2026-09-26T06:00:00Z"},
        {
            "event": "app_crashed",
            "pid": 2,
            "stage": "startup",
            "timestamp": "2026-09-26T06:00:05Z",
            "error": 'IOException: Could not set lock on file "x.duckdb"',
        },
    ]
    (tmp_path / "moneytool-2026-09-26.log").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )
    text, problem = describe_last_run(_logs(tmp_path))
    assert text.startswith("2026-09-26 14:00:05 启动时崩溃")
    assert "另一个 moneytool 进程占用" in problem
    with (tmp_path / "moneytool-2026-09-26.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "app_stopped", "pid": 1, "timestamp": "2026-09-26T07:00:00Z"}))
    text, problem = describe_last_run(_logs(tmp_path))
    assert text == "2026-09-26 14:00:00 启动（进程 1），2026-09-26 15:00:00 正常退出"
    assert problem == ""
