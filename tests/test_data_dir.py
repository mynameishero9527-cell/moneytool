from __future__ import annotations

from pathlib import Path

import pytest
from filelock import FileLock

from moneytool.app import LegacyDataBusyError, init_data_dir, move_legacy_data
from moneytool.config import DEFAULT_DATA_DIR, load_settings, project_root


def test_default_data_dir_is_inside_project() -> None:
    root = project_root()
    assert root is not None
    assert root / "data" == DEFAULT_DATA_DIR


def test_env_var_decides_where_config_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "custom"
    d.mkdir()
    (d / "config.toml").write_text("[server]\nport = 9123\n", encoding="utf-8")
    monkeypatch.setenv("MONEYTOOL_DATA__DIR", str(d))
    s = load_settings()
    assert s.data_dir == d.resolve()
    assert s.server.port == 9123


def test_move_legacy_data(tmp_path: Path) -> None:
    legacy = tmp_path / "home" / ".moneytool"
    init_data_dir(legacy)
    (legacy / "logs" / "moneytool-2026-09-25.log").write_text("x", encoding="utf-8")
    target = tmp_path / "proj" / "data"
    target.mkdir(parents=True)
    (target / "logs").mkdir()

    assert move_legacy_data(target, legacy)
    assert (target / "moneytool.duckdb").exists()
    assert (target / "config.toml").exists()
    assert (target / "logs" / "moneytool-2026-09-25.log").exists()
    assert not legacy.exists()
    assert not (tmp_path / "proj" / "data.moving").exists()
    assert not move_legacy_data(target, legacy)


def test_move_skipped_when_target_has_db(tmp_path: Path) -> None:
    legacy = tmp_path / "old"
    target = tmp_path / "new"
    init_data_dir(legacy)
    init_data_dir(target)
    assert not move_legacy_data(target, legacy)
    assert (legacy / "moneytool.duckdb").exists()


def test_move_refused_while_legacy_app_runs(tmp_path: Path) -> None:
    legacy = tmp_path / "old"
    init_data_dir(legacy)
    with FileLock(str(legacy / ".lock")), pytest.raises(LegacyDataBusyError):
        move_legacy_data(tmp_path / "new", legacy)
    assert (legacy / "moneytool.duckdb").exists()
    assert not (tmp_path / "new" / "moneytool.duckdb").exists()
