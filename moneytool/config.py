"""运行配置：`~/.moneytool/config.toml` + 环境变量 `MONEYTOOL_*`。架构 9 节。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATA_DIR = Path("~/.moneytool")


class ServerConfig(BaseModel):
    listen: str = "127.0.0.1"
    port: int = 8000
    open_browser: bool = True
    token: str = ""  # 监听非回环地址时必填


class DataConfig(BaseModel):
    dir: Path = DEFAULT_DATA_DIR
    backfill_years_flow: int = 2
    backfill_years_bars: int = 5
    raw_retention_days: int = 90
    log_retention_days: int = 30
    backup_keep: int = 5
    intraday_retention_days: int = 30  # 盘中分段中间结果保留天数（收盘确认结果永久保留）


class SourceRateLimit(BaseModel):
    min_interval_seconds: float = 2.0
    per_stock_interval_seconds: float = 5.0
    timeout_seconds: float = 30.0
    backoff_seconds: tuple[float, ...] = (10.0, 30.0, 90.0)


class RateLimitConfig(BaseModel):
    eastmoney: SourceRateLimit = SourceRateLimit()
    baostock: SourceRateLimit = SourceRateLimit(
        min_interval_seconds=0.2, per_stock_interval_seconds=0.2
    )
    shenwan: SourceRateLimit = SourceRateLimit(min_interval_seconds=1.0)
    csindex: SourceRateLimit = SourceRateLimit(min_interval_seconds=1.0)


class CalendarConfig(BaseModel):
    extra_closed: list[str] = Field(default_factory=list)  # 临时休市 YYYY-MM-DD


class NotifyConfig(BaseModel):
    desktop: bool = True
    webhook_url: str = ""
    quiet_start: str = "20:30"
    quiet_end: str = "08:30"


class ScheduleConfig(BaseModel):
    confirm_deadline: str = "20:00"  # 最晚用实时累计值代替
    confirm_probe_minutes: int = 30
    clock_drift_warn_seconds: int = 120


class Settings(BaseSettings):
    """全部配置。TOML 文件缺失时用默认值；环境变量优先级高于文件。"""

    model_config = SettingsConfigDict(env_prefix="MONEYTOOL_", env_nested_delimiter="__")

    server: ServerConfig = ServerConfig()
    data: DataConfig = DataConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    calendar: CalendarConfig = CalendarConfig()
    notify: NotifyConfig = NotifyConfig()
    schedule: ScheduleConfig = ScheduleConfig()

    @property
    def data_dir(self) -> Path:
        return self.data.dir.expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "moneytool.duckdb"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def params_dir(self) -> Path:
        return self.data_dir / "params"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def briefs_dir(self) -> Path:
        return self.data_dir / "briefs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".lock"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.toml"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.raw_dir,
            self.params_dir,
            self.logs_dir,
            self.briefs_dir,
            self.backups_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings(data_dir: Path | None = None) -> Settings:
    """读取配置。`data_dir` 显式给出时优先，其次 TOML 里的 `[data].dir`，最后默认。"""
    base_dir = (data_dir or DEFAULT_DATA_DIR).expanduser()
    file_values = _read_toml(base_dir / "config.toml")
    if data_dir is not None:
        file_values.setdefault("data", {})["dir"] = str(base_dir)
    return Settings(**file_values)


DEFAULT_CONFIG_TOML = """# moneytool 配置。改完重启程序生效。
[server]
listen = "127.0.0.1"
port = 8000
open_browser = true

[data]
backfill_years_flow = 2
backfill_years_bars = 5

[rate_limit.eastmoney]
min_interval_seconds = 2.0
per_stock_interval_seconds = 5.0

[calendar]
extra_closed = []

[notify]
desktop = true
webhook_url = ""
"""
