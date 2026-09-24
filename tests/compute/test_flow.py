from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from moneytool.compute.flow import intraday_pattern, reconcile, segment_diff
from moneytool.params import Params

D = dt.date(2026, 3, 2)


def snaps(values: dict[str, float]) -> pl.DataFrame:
    segs = list(values)
    return pl.DataFrame(
        {
            "subject_id": ["000001.SZ"] * len(segs),
            "trade_date": [D] * len(segs),
            "segment": segs,
            "net_main": list(values.values()),
            "net_super": [0.0] * len(segs),
            "net_large": [0.0] * len(segs),
            "net_medium": [0.0] * len(segs),
            "net_small": [0.0] * len(segs),
        }
    )


def test_segment_diff_basic() -> None:
    df = segment_diff(snaps({"0930_1030": 100.0, "1030_1130": 150.0, "1300_1400": 120.0}))
    assert df["net_main"].to_list() == [100.0, 50.0, -30.0]
    assert df["spans_missing"].to_list() == [False, False, False]


def test_segment_diff_spans_missing() -> None:
    """验收 23 的一部分：缺段时跨段差分并标记。"""
    df = segment_diff(snaps({"0930_1030": 100.0, "1300_1400": 160.0}))
    assert df["net_main"].to_list() == [100.0, 60.0]
    assert df["spans_missing"].to_list() == [False, True]


def test_segment_diff_first_missing_marks_span() -> None:
    df = segment_diff(snaps({"1030_1130": 80.0}))
    assert df["net_main"].to_list() == [80.0]
    assert df["spans_missing"].to_list() == [True]


def test_reconcile_ratio() -> None:
    segs = segment_diff(snaps({"0930_1030": 100.0, "1030_1130": 150.0}))
    daily = pl.DataFrame({"subject_id": ["000001.SZ"], "trade_date": [D], "net_main": [200.0]})
    out = reconcile(segs, daily)
    assert out["diff_ratio"][0] == pytest.approx(0.25)


def test_intraday_pattern(params: Params) -> None:
    def seg_frame(vals: list[float]) -> pl.DataFrame:
        return segment_diff(
            snaps(
                dict(
                    zip(
                        ["0930_1030", "1030_1130", "1300_1400", "1400_1430", "1430_1500"],
                        [sum(vals[: i + 1]) for i in range(5)],
                        strict=True,
                    )
                )
            )
        )

    assert (
        intraday_pattern(seg_frame([100, 100, -80, -60, -30]), params)["pattern"][0] == "冲高回吐"
    )
    assert intraday_pattern(seg_frame([10, 10, 10, 10, 200]), params)["pattern"][0] == "尾盘异动"
    assert intraday_pattern(seg_frame([100, 100, 20, 20, 20]), params)["pattern"][0] == "早盘加强"
    assert intraday_pattern(seg_frame([20, 20, 20, 20, 20]), params)["pattern"][0] == "全天稳步"
    partial = segment_diff(snaps({"0930_1030": 10.0, "1030_1130": 20.0}))
    assert intraday_pattern(partial, params)["pattern"][0] is None
