"""全项目共享的枚举与常量。名称与需求附录 A 术语表一致。"""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    """需求 7.1 六阶段。值为存库字符串。"""

    FREEZE = "freeze"  # 冰点
    START = "start"  # 启动
    SPREAD = "spread"  # 扩散加速
    CLIMAX = "climax"  # 高潮拥挤
    DIVERGE = "diverge"  # 分歧背离
    EBB = "ebb"  # 退潮


# 需求 7.1 判定优先级：否决态在前。
STAGE_PRIORITY: tuple[Stage, ...] = (
    Stage.EBB,
    Stage.DIVERGE,
    Stage.CLIMAX,
    Stage.SPREAD,
    Stage.START,
    Stage.FREEZE,
)

# 需求 7.1 允许的迁移路径：主路径环 + 任一 → 退潮 / 分歧背离。
ALLOWED_TRANSITIONS: frozenset[tuple[Stage, Stage]] = frozenset(
    {
        (Stage.FREEZE, Stage.START),
        (Stage.START, Stage.SPREAD),
        (Stage.SPREAD, Stage.CLIMAX),
        (Stage.CLIMAX, Stage.DIVERGE),
        (Stage.DIVERGE, Stage.EBB),
        (Stage.EBB, Stage.FREEZE),
    }
    | {(s, Stage.EBB) for s in Stage if s is not Stage.EBB}
    | {(s, Stage.DIVERGE) for s in Stage if s is not Stage.DIVERGE}
)

STAGE_LABEL_ZH: dict[Stage, str] = {
    Stage.FREEZE: "冰点",
    Stage.START: "启动",
    Stage.SPREAD: "扩散加速",
    Stage.CLIMAX: "高潮拥挤",
    Stage.DIVERGE: "分歧背离",
    Stage.EBB: "退潮",
}


class SpreadHalf(StrEnum):
    """需求 7.1 扩散加速前后半段。"""

    FIRST = "first"
    SECOND = "second"


class Role(StrEnum):
    """需求 8.1 / 8.2 个股角色。"""

    CORE = "core"
    FOLLOW = "follow"
    AVOID = "avoid"
    OTHER = "other"


class ListType(StrEnum):
    """需求 8.3–8.5、8.9 名单类型。"""

    BUY = "buy"
    SELL = "sell"
    HOLD_WATCH = "hold_watch"
    TRACKING = "tracking"
    LOW_BASE = "low_base"


class Segment(StrEnum):
    """需求 11.2 盘中分段。值为存库字符串，顺序即时间顺序。"""

    AUCTION = "auction"  # 9:25 竞价
    S1 = "0930_1030"
    S2 = "1030_1130"
    S3 = "1300_1400"
    S4 = "1400_1430"
    S5 = "1430_1500"


INTRADAY_SEGMENTS: tuple[Segment, ...] = (
    Segment.S1,
    Segment.S2,
    Segment.S3,
    Segment.S4,
    Segment.S5,
)


class DataStatus(StrEnum):
    """需求附录 A `DataStatus`，API `meta.status` 取值。"""

    INTRADAY = "intraday"
    CONFIRMED = "confirmed"
    UNRECONCILED = "unreconciled"
    MISSING = "missing"
    DEGRADED = "degraded"


class SectorLevel(StrEnum):
    """板块层级。申万一级 / 二级 / 东财概念。"""

    L1 = "L1"
    L2 = "L2"
    CONCEPT = "concept"


class QualityStatus(StrEnum):
    """`data_quality.status` 取值（duckdb-schema skill）。"""

    MISSING = "missing"
    CAPTCHA = "captcha"
    CONTRACT_ERROR = "contract_error"
    RECONCILE = "reconcile"
    STALE = "stale"
    DEGRADED = "degraded"


class MarketRegime(StrEnum):
    """需求 7.3.1 市场宽度三态。"""

    CLEAR = "clear"  # 主线清晰
    SCATTERED = "scattered"  # 分散
    NONE = "none"  # 无主线


# 需求 12 节禁用词。模板与页面文案测试用。
FORBIDDEN_WORDS: tuple[str, ...] = (
    "稳赚",
    "必涨",
    "清仓",
    "满仓",
    "最佳",
    "杀散户",
    "套牢",
    "值得",
    "建议持有",
    "安全",
    "危险",
)
