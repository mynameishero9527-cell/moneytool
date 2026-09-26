"""诊断报告：新装、已有数据、主程序占用数据库、日志错误摘要。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moneytool.api.server import create_app
from moneytool.app import build_context, init_data_dir
from moneytool.config import load_settings
from moneytool.diagnose import _format_log_line, _is_error, build_report
from tests.compute.test_pipeline import seed_market


def test_report_before_init_flags_missing_db(tmp_data_dir: Path) -> None:
    text, ok = build_report(load_settings(tmp_data_dir), check_sources=False)
    assert not ok
    assert "数据库不存在" in text
    assert "== 结论 ==" in text


def test_report_on_seeded_db_and_logs(tmp_data_dir: Path) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    seed_market(ctx.db, n_days=30)
    with ctx.db.write() as conn:
        conn.execute(
            "INSERT INTO job_run VALUES ('backfill', DATE '2026-02-02', '', now(), now(), FALSE, NULL, '接口挂了')"
        )
    ctx.close()
    settings = load_settings(tmp_data_dir)
    log = settings.logs_dir / "moneytool-2026-02-02.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps(
            {
                "event": "job_failed",
                "level": "error",
                "timestamp": "2026-02-02T10:00:00",
                "tb": "Traceback\n  File x\nValueError: 坏了",
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {"event": "backfill_idle", "level": "info", "timestamp": "2026-02-02T10:01:00"}
        )
        + "\n",
        encoding="utf-8",
    )
    text, ok = build_report(settings, check_sources=False, log_lines=5)
    assert not ok
    assert "主程序未运行" in text
    assert "证券 30" in text
    assert "尚无计算结果" in text
    assert "接口挂了" in text
    assert "ValueError: 坏了" in text
    assert "backfill_idle" in text


def test_report_via_http_when_db_locked(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_data_dir(tmp_data_dir)
    ctx = build_context(tmp_data_dir, log_to_file=False)
    seed_market(ctx.db, n_days=30)
    with TestClient(create_app(ctx.db, static_dir=None)) as client:
        summary = client.get("/api/diagnose").json()["data"]
    assert summary["counts"]["security"] == 30
    monkeypatch.setattr("moneytool.diagnose._open_db", lambda s: (None, "locked"))
    monkeypatch.setattr("moneytool.diagnose._http_summary", lambda s: summary)
    text, _ = build_report(load_settings(tmp_data_dir), check_sources=False)
    ctx.close()
    assert "主程序正在运行" in text
    assert "证券 30" in text


def test_report_tells_stale_lock_from_hung_app(tmp_data_dir: Path) -> None:
    import os

    init_data_dir(tmp_data_dir)
    settings = load_settings(tmp_data_dir)
    settings.server.port = 1  # 保证本机接口连不上
    holder = settings.lock_path.with_suffix(".holder.json")

    holder.write_text(json.dumps({"owner": "backfill", "pid": "999999999"}), encoding="utf-8")
    text, _ = build_report(settings, check_sources=False)
    assert "进程已退出" in text
    assert "主程序未运行" in text

    holder.write_text(json.dumps({"owner": "backfill", "pid": str(os.getpid())}), encoding="utf-8")
    text, ok = build_report(settings, check_sources=False)
    assert "进程仍在运行" in text
    assert "10 秒内无响应" in text
    assert not ok


def test_pid_alive() -> None:
    import os

    from moneytool.lock import pid_alive

    assert pid_alive(os.getpid())
    assert not pid_alive(999999999)


def test_log_line_helpers() -> None:
    assert _is_error(json.dumps({"level": "warning", "event": "x"}))
    assert not _is_error(json.dumps({"level": "info", "event": "x"}))
    assert _is_error("Traceback (most recent call last):")
    assert _format_log_line("plain text\n") == "plain text"
