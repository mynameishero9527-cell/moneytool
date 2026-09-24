"""参数版本：`params/<version>.yaml` → `Params`。缺键即报错；按交易日取当日生效版本。"""

from __future__ import annotations

import datetime as dt
import shutil
from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Windows(_Strict):
    pulse: int
    short: int
    persist: int
    cycle: int
    long: int
    year: int
    min_valid_ratio: float


class Qualifiers(_Strict):
    significant_multiple: float
    clear_pp: float
    majority_ratio: float
    extreme_pct: float
    flat_abs_pct: float
    volume_multiple: float
    large_outflow_pct: float
    tail_share: float


class Universe(_Strict):
    min_listed_days: int
    exclude_st: bool
    exclude_bj: bool
    min_members: int
    one_word_amount_ratio: float


class StageFreeze(_Strict):
    outflow_streak_min: int
    outflow_slowing: bool
    breadth_max: float


class StageStart(_Strict):
    inflow_days_max: int
    ret_5d_max: float
    breadth_min: float


class StageSpread(_Strict):
    slope_5d_min: float
    breadth_min: float
    concentration_max: float


class StageClimax(_Strict):
    concentration_min: float
    breadth_drop_pp: float


class StageDiverge(_Strict):
    ret_5d_min: float
    breadth_max: float


class StageEbb(_Strict):
    outflow_streak_min: int
    rs_5d_max: float


class StageParams(_Strict):
    freeze: StageFreeze
    start: StageStart
    spread: StageSpread
    climax: StageClimax
    diverge: StageDiverge
    ebb: StageEbb
    debounce_days: int
    history_window: int


class GateParams(_Strict):
    eqw_drawdown_5d_pct: float
    min_l1_active: int
    outflow_days: int
    overheat_days: int
    release_days: int


class MarketParams(_Strict):
    mainline_top_n: int
    mainline_min_days: int
    clear_share_min: float
    scattered_share_max: float
    rotation_ratio_lo: float
    rotation_ratio_hi: float
    concept_min_members: int
    pressure_window: int
    pressure_lo_pct: float
    pressure_hi_pct: float
    pressure_overheat: int
    gate: GateParams


class RoleParams(_Strict):
    strong_ratio_top: float
    core_top: float
    invalid_ratio_top: float
    avoid_bottom: float
    bleed_outflow_days: int
    min_inflow_days_of_5: int
    # v2 起显式给出；v1 取默认值（与需求 8.1 / 8.3 一致）
    basis_levels: tuple[str, ...] = ("L2", "concept")
    new_member_days: int = 5
    diverge_ratio_side: float = 0.5


class BuyParams(_Strict):
    follow_top_share: float
    position_multiple: float
    hype_position_multiple: float
    drawdown_extreme_pct: float
    min_avg_amount_20d: float
    per_sector_max: int
    per_l1_max: int
    tracking_days: int
    invalid_keep_days: int


class SellParams(_Strict):
    climax_turnover_multiple: float
    tracking_expire_days: int


class PointParams(_Strict):
    start_ratio_top: float
    breakout_amount_multiple: float
    breakout_breadth_min: float


class LowbaseParams(_Strict):
    range_pos_max: float
    drawdown_min: float
    no_new_low_days: int
    amplitude_ratio_max: float
    amount_ratio_lo: float
    amount_ratio_hi: float
    inflow_days_of_10: int
    max_day_ret: float
    per_l1_max: int
    tracking_days: int


class ExcludeParams(_Strict):
    control_float_mv_max: float
    control_top10_share_min: float
    control_low_turnover: float
    control_big_move: float
    control_days_of_60: int
    crash_run_ret: float
    crash_run_days: int
    crash_drawdown: float
    crash_drawdown_days: int
    crash_events_min: int
    crash_years: int
    blowup_min: int
    blowup_drop: float
    holders_increase: float
    holders_drop: float
    reduction_after_ret: float
    reduction_min: int
    tradable_min_avg_amount_20d: float


class ScoreParams(_Strict):
    sector_top: float
    sector_bottom: float
    eqw_top: float
    streak_full: int
    normal_lo: float
    normal_hi: float
    penalty: int


class TagParams(_Strict):
    amount_tiers: tuple[float, float, float, float]
    float_mv_tiers: tuple[float, float, float, float]
    index_ids: tuple[str, ...]


class AttributionParams(_Strict):
    seasonal_years: int
    seasonal_positive_years_min: int
    seasonal_median_pp: float
    external_corr_min: float
    external_move_min: float
    gap_multiple: float
    hype_limit_pct: float
    hype_concentration_min: float
    hype_turnover_multiple: float
    hype_small_share_min: float
    min_score: int


class IndexParams(_Strict):
    window: int
    lo_pct: float
    hi_pct: float
    stage_risk: dict[str, int]
    review_risk_min: int
    broken_risk_min: int
    retail_review_min: int
    # v2 起显式给出；v1 取默认值（需求 7.7 / 8.11）
    diverge_full_ratio: float = 0.02
    hype_risk: dict[int, int] = Field(default_factory=lambda: {3: 20, 2: 12, 1: 6})
    basis_risk_reduce: int = 4
    overdraft_position_min: int = 16
    liquidity_dry: float = 0.5
    liquidity_hot: float = 1.5
    micro_extra: int = 5
    sector_risk_extra: int = 5
    retail_peak_days: int = 10
    retail_peak_drop: float = 0.1
    retail_vwap_gap: float = 0.05
    focus_risk_max: int = 60


class LabelParams(_Strict):
    """需求 9.7.1 事后统计。"""

    horizons: tuple[int, ...] = (5, 10, 20)
    min_samples: int = 20


class Params(_Strict):
    """一个参数版本的全部阈值。代码只通过 `p.<section>.<key>` 引用，不写数字。"""

    version: str
    effective_from: dt.date
    note: str
    windows: Windows
    qualifiers: Qualifiers
    universe: Universe
    stage: StageParams
    market: MarketParams
    role: RoleParams
    buy: BuyParams
    sell: SellParams
    point: PointParams
    lowbase: LowbaseParams
    exclude: ExcludeParams
    score: ScoreParams
    tags: TagParams
    attribution: AttributionParams
    indices: IndexParams
    labels: LabelParams = Field(default_factory=LabelParams)


def builtin_params_dir() -> Path:
    return Path(str(resources.files("moneytool.params")))


def load_params_file(path: Path) -> Params:
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Params.model_validate(raw)


def list_versions(params_dir: Path) -> list[Params]:
    """目录下全部版本，按生效日升序。"""
    versions = [load_params_file(p) for p in sorted(params_dir.glob("v*.yaml"))]
    versions.sort(key=lambda p: (p.effective_from, p.version))
    return versions


def params_for_date(params_dir: Path, trade_date: dt.date) -> Params:
    """当日生效版本 = effective_from ≤ 当日 的最新一个；都不满足时用最早版本。"""
    versions = list_versions(params_dir)
    if not versions:
        raise FileNotFoundError(f"参数目录为空: {params_dir}")
    chosen = versions[0]
    for v in versions:
        if v.effective_from <= trade_date:
            chosen = v
    return chosen


def latest_params(params_dir: Path) -> Params:
    versions = list_versions(params_dir)
    if not versions:
        raise FileNotFoundError(f"参数目录为空: {params_dir}")
    return versions[-1]


def install_builtin_params(params_dir: Path) -> list[str]:
    """`init` 时把包内版本拷到数据目录；已存在的不覆盖（版本只增不改）。"""
    params_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for src in sorted(builtin_params_dir().glob("v*.yaml")):
        dst = params_dir / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)
            copied.append(src.name)
    return copied


def new_version(params_dir: Path, effective_from: dt.date, note: str) -> Path:
    """`params new`：复制最新版本为 v{n+1}，改 version / effective_from / note。"""
    latest = latest_params(params_dir)
    n = int(latest.version.lstrip("v")) + 1
    src = params_dir / f"{latest.version}.yaml"
    dst = params_dir / f"v{n}.yaml"
    with src.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw["version"] = f"v{n}"
    raw["effective_from"] = effective_from
    raw["note"] = note
    with dst.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
    return dst
