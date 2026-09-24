"""证据结构与比较助手。需求 2 节：每个标签都能展开看命中了哪几条、当日值与阈值。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

FeatureRow = Mapping[str, Any]


@dataclass(frozen=True)
class Evidence:
    rule: str
    metric: str
    value: float | None
    threshold: float | None
    op: str
    hit: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "op": self.op,
            "hit": self.hit,
            "note": self.note,
        }


@dataclass(frozen=True)
class RuleResult:
    hit: bool
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.evidence]


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def compare(
    rule: str,
    row: FeatureRow,
    metric: str,
    op: str,
    threshold: float | None,
    note: str = "",
) -> Evidence:
    """对 `row[metric]` 做一次比较并返回证据。值缺失时 `hit=False` 并在 note 标「数据缺失」。"""
    value = _as_float(row.get(metric))
    if value is None or (threshold is None and op not in ("is_true", "is_false")):
        return Evidence(
            rule, metric, value, threshold, op, False, f"{note}（数据缺失）" if note else "数据缺失"
        )
    hit = _apply(op, value, threshold)
    return Evidence(rule, metric, value, threshold, op, hit, note)


def _apply(op: str, value: float, threshold: float | None) -> bool:
    if op == "is_true":
        return value == 1.0
    if op == "is_false":
        return value == 0.0
    assert threshold is not None
    if op == ">=":
        return value >= threshold
    if op == ">":
        return value > threshold
    if op == "<=":
        return value <= threshold
    if op == "<":
        return value < threshold
    if op == "==":
        return value == threshold
    if op == "abs<=":
        return abs(value) <= threshold
    if op == "abs>=":
        return abs(value) >= threshold
    raise ValueError(f"未知比较运算: {op}")


def all_hit(*evidence: Evidence) -> RuleResult:
    return RuleResult(all(e.hit for e in evidence), tuple(evidence))


def any_hit(*evidence: Evidence) -> RuleResult:
    return RuleResult(any(e.hit for e in evidence), tuple(evidence))
