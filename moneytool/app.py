"""进程组装：配置 → 数据目录 → DuckDB 迁移 → 参数 → 适配器。调度与 Web 由各自模块在此之上启动。"""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

from moneytool.adapters.registry import Adapters, build_adapters
from moneytool.config import (
    DEFAULT_CONFIG_TOML,
    DEFAULT_DATA_DIR,
    LEGACY_DATA_DIR,
    Settings,
    load_settings,
)
from moneytool.logging import get_logger, setup_logging
from moneytool.network import apply_network
from moneytool.params import Params, install_builtin_params, latest_params, params_for_date
from moneytool.storage.conn import Database
from moneytool.storage.migrate import migrate

TZ = ZoneInfo("Asia/Shanghai")
log = get_logger(__name__)


def now_sh() -> dt.datetime:
    return dt.datetime.now(TZ)


def today_sh() -> dt.date:
    return now_sh().date()


@dataclass
class AppContext:
    settings: Settings
    db: Database
    adapters: Adapters

    @property
    def params_dir(self) -> Path:
        return self.settings.params_dir

    def params_for(self, day: dt.date) -> Params:
        return params_for_date(self.params_dir, day)

    def latest_params(self) -> Params:
        return latest_params(self.params_dir)

    def close(self) -> None:
        self.adapters.close()
        self.db.close()


class LegacyDataBusyError(RuntimeError):
    pass


def move_legacy_data(target: Path, legacy: Path = LEGACY_DATA_DIR) -> bool:
    """旧版默认数据目录 `~/.moneytool` 整体搬到新目录。新目录已有数据库时不动，返回是否搬迁。"""
    target = target.expanduser().resolve()
    legacy = legacy.expanduser().resolve()
    if legacy == target or not (legacy / "moneytool.duckdb").exists():
        return False
    if (target / "moneytool.duckdb").exists():
        return False
    try:
        with FileLock(str(legacy / ".lock"), timeout=0):
            staging = target.with_name(target.name + ".moving")
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(legacy, staging, ignore=shutil.ignore_patterns(".lock"))
    except (Timeout, OSError) as exc:
        raise LegacyDataBusyError(
            f"无法从 {legacy} 搬迁数据到 {target}：{exc}。请关闭所有 moneytool 窗口后重试"
        ) from exc
    target.mkdir(parents=True, exist_ok=True)
    for item in staging.iterdir():
        dest = target / item.name
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists():
            dest.unlink()
        item.rename(dest)
    staging.rmdir()
    shutil.rmtree(legacy, ignore_errors=True)
    log.info("legacy_data_moved", source=str(legacy), target=str(target), left=legacy.exists())
    return True


def _relocate_default(settings: Settings, data_dir: Path | None) -> None:
    if data_dir is None and settings.data_dir == DEFAULT_DATA_DIR.expanduser().resolve():
        move_legacy_data(settings.data_dir)


def init_data_dir(data_dir: Path | None) -> Settings:
    """`moneytool init`：建目录、写默认配置、拷参数版本、建表。可重复执行。"""
    settings = load_settings(data_dir)
    _relocate_default(settings, data_dir)
    settings.ensure_dirs()
    if not settings.config_path.exists():
        settings.config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    install_builtin_params(settings.params_dir)
    db = Database(settings.db_path)
    try:
        applied = migrate(db.rw)
        log.info("init_done", data_dir=str(settings.data_dir), migrations=applied)
    finally:
        db.close()
    return settings


def build_context(data_dir: Path | None, *, log_to_file: bool = True) -> AppContext:
    settings = load_settings(data_dir)
    _relocate_default(settings, data_dir)
    settings.ensure_dirs()
    apply_network(settings)
    setup_logging(
        settings.logs_dir if log_to_file else None, retention_days=settings.data.log_retention_days
    )
    install_builtin_params(settings.params_dir)
    db = Database(settings.db_path)
    migrate(db.rw)
    return AppContext(settings=settings, db=db, adapters=build_adapters(settings))
