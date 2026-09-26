"""运行配置：`<数据目录>/config.toml` + 环境变量 `MONEYTOOL_*`。架构 9 节。"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LEGACY_DATA_DIR = Path("~/.moneytool")
DATA_DIR_ENV = "MONEYTOOL_DATA__DIR"


def project_root() -> Path | None:
    """源码目录运行（`pip install -e .`）时返回仓库根目录，装进 site-packages 时返回 None。"""
    root = Path(__file__).resolve().parent.parent
    return root if (root / "pyproject.toml").exists() else None


def _default_data_dir() -> Path:
    root = project_root()
    return root / "data" if root else LEGACY_DATA_DIR


DEFAULT_DATA_DIR = _default_data_dir()


class ServerConfig(BaseModel):
    listen: str = "127.0.0.1"
    port: int = 8000
    open_browser: bool = True
    token: str = ""  # 监听非回环地址时必填


class DataConfig(BaseModel):
    dir: Path = DEFAULT_DATA_DIR
    # 日频资金流来源：sina（个股历史约 8 年）或 eastmoney（只有最近约 120 个交易日）。
    # 两家大小单口径不同，整段历史只用一个来源；改了之后需重建资金流历史（见 README）
    flow_source: Literal["sina", "eastmoney"] = "sina"
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
    sina: SourceRateLimit = SourceRateLimit(
        min_interval_seconds=1.0, per_stock_interval_seconds=1.0, backoff_seconds=(5.0, 15.0)
    )


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
    catchup_days: int = 60  # 启动 / 回补完成后补算最近 N 个交易日缺失的收盘结果
    catchup_min_coverage: float = 0.8  # 当日日线覆盖在市证券比例不足则不补算，避免用残缺数据出结论


FLOW_BACKFILL_DEFAULTS: dict[str, tuple[int, float]] = {"sina": (2, 1.0), "eastmoney": (1, 3.0)}


class BackfillConfig(BaseModel):
    batch: int = 100  # 每批标的数，每批写库一次
    # 个股资金流：多线程共享一个自适应限速器，成功时间隔逐步降到下限，失败翻倍直到上限。
    # 不填时按来源取默认值：新浪 2 线程 / 1 秒，东财 1 线程 / 3 秒（东财易封 IP）
    flow_workers: int | None = None
    flow_interval_seconds: float | None = None
    flow_max_interval_seconds: float = 30.0

    def flow_pace(self, source: str) -> tuple[int, float]:
        workers, interval = FLOW_BACKFILL_DEFAULTS.get(source, (1, 3.0))
        return (self.flow_workers or workers, self.flow_interval_seconds or interval)


class NetworkConfig(BaseModel):
    # direct：数据源直连，不走系统 / 环境代理（数据源都在境内，代理常导致 ProxyError）
    # system：沿用系统代理；也可直接填代理地址，如 "http://127.0.0.1:7890"
    proxy: str = "direct"


class Settings(BaseSettings):
    """全部配置。TOML 文件缺失时用默认值；环境变量优先级高于文件。"""

    model_config = SettingsConfigDict(env_prefix="MONEYTOOL_", env_nested_delimiter="__")

    network: NetworkConfig = NetworkConfig()
    backfill: BackfillConfig = BackfillConfig()
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
    """读取配置。数据目录：`data_dir` 参数 > 环境变量 `MONEYTOOL_DATA__DIR` > 项目目录下 `data/`。"""
    explicit = data_dir or (
        Path(os.environ[DATA_DIR_ENV]) if os.environ.get(DATA_DIR_ENV) else None
    )
    base_dir = (explicit or DEFAULT_DATA_DIR).expanduser()
    file_values = _read_toml(base_dir / "config.toml")
    file_values.setdefault("data", {})["dir"] = str(base_dir)
    return Settings(**file_values)


DEFAULT_CONFIG_TOML = """# moneytool 配置。改完重启程序生效。
[server]
listen = "127.0.0.1"
port = 8000
open_browser = true

[network]
# direct：数据源直连，不走系统代理（默认）；system：沿用系统代理；或填代理地址如 "http://127.0.0.1:7890"
proxy = "direct"

[data]
# 日频资金流来源：sina（默认，历史约 8 年）或 eastmoney（约 120 个交易日）；两家口径不同，不混用
flow_source = "sina"
backfill_years_flow = 2
backfill_years_bars = 5

[backfill]
# 个股资金流回补的线程数与最小请求间隔（秒），不填按来源默认：新浪 2 线程 / 1 秒，东财 1 线程 / 3 秒。
# 失败时间隔自动翻倍，连续失败暂停 30 分钟起、逐次翻倍到 4 小时；东财间隔过小会被封 IP
# flow_workers = 2
# flow_interval_seconds = 1.0
flow_max_interval_seconds = 30.0

[rate_limit.eastmoney]
min_interval_seconds = 2.0
per_stock_interval_seconds = 5.0

[calendar]
extra_closed = []

[notify]
desktop = true
webhook_url = ""
"""
