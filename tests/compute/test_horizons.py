"""多周期资金分析：周期累计、趋势倾向、历史统计无前视、资金动向信号、落表与接口。"""

from __future__ import annotations

import json

import polars as pl
from fastapi.testclient import TestClient

from moneytool.api.server import create_app
from moneytool.compute.horizons import (
    FWD_DAYS,
    HORIZONS,
    backtest_frame,
    compute_horizons,
    horizon_wide,
    trend_frame,
)
from moneytool.compute.pipeline import run
from moneytool.notify import collect_events
from moneytool.params import Params
from moneytool.storage.conn import Database
from moneytool.types import FORBIDDEN_WORDS
from tests.compute.test_pipeline import seed_market
from tests.conftest import trading_dates

BIAS = {"A": 3e7, "B": 0.0, "C": -3e7}
DRIFT = {"A": 0.004, "B": 0.0, "C": -0.004}


def _frames(
    n_days: int = 300, today_net_a: float | None = None
) -> tuple[pl.DataFrame, pl.DataFrame]:
    dates = trading_dates(n_days)
    recs = []
    for sid, bias in BIAS.items():
        for i, d in enumerate(dates):
            wobble = 1e7 if i % 2 else -1e7
            net = bias + wobble
            if today_net_a is not None and sid == "A" and i == n_days - 1:
                net = today_net_a
            recs.append(
                {
                    "sector_id": sid,
                    "trade_date": d,
                    "level": "L1",
                    "small_sample": False,
                    "sector_net_main": net,
                    "sector_amount": 1e9,
                    "sector_pct_chg": DRIFT[sid] + (0.001 if i % 2 else -0.001),
                }
            )
    feat = pl.DataFrame(recs)
    market = (
        feat.group_by("trade_date")
        .agg(
            net_main_all=pl.col("sector_net_main").sum(),
            amount_all=pl.col("sector_amount").sum(),
            eqw_ret=pl.col("sector_pct_chg").mean(),
        )
        .sort("trade_date")
    )
    return feat, market


def test_horizon_sums_and_ranks(params: Params) -> None:
    feat, market = _frames()
    wide = horizon_wide(feat, market, params)
    day = feat["trade_date"].max()
    a = wide.filter((pl.col("sector_id") == "A") & (pl.col("trade_date") == day)).row(0, named=True)
    raw = feat.filter(pl.col("sector_id") == "A").sort("trade_date")["sector_net_main"]
    for key, n, _ in HORIZONS:
        assert abs(a[f"net_{key}"] - raw.tail(n).sum()) < 1e-3
        assert abs(a[f"mr_{key}"] - raw.tail(n).sum() / (1e9 * n)) < 1e-9
    assert a["fr_20d"] == 100.0 and a["fr_250d"] == 100.0
    c = wide.filter((pl.col("sector_id") == "C") & (pl.col("trade_date") == day)).row(0, named=True)
    assert c["fr_20d"] == 0.0
    m = wide.filter((pl.col("sector_id") == "market") & (pl.col("trade_date") == day)).row(
        0, named=True
    )
    assert m["fr_5d"] is None
    assert abs(m["net_5d"] - market["net_main_all"].tail(5).sum()) < 1e-3


def test_trend_direction_and_consistency(params: Params) -> None:
    feat, market = _frames()
    trend = trend_frame(horizon_wide(feat, market, params))
    last = trend.filter(pl.col("trade_date") == feat["trade_date"].max())
    by = {r["sector_id"]: r for r in last.iter_rows(named=True)}
    assert by["A"]["direction"] == "up" and by["A"]["strength"] == "strong"
    assert by["C"]["direction"] == "down"
    assert by["B"]["direction"] == "flat"
    assert by["A"]["consistency"] == "aligned"
    assert by["market"]["direction"] is None
    assert -100 <= by["C"]["score"] <= by["B"]["score"] <= by["A"]["score"] <= 100


def test_backtest_uses_only_completed_windows(params: Params) -> None:
    feat, market = _frames(n_days=120)
    trend = trend_frame(horizon_wide(feat, market, params))
    bt = backtest_frame(trend)
    scored_dates = trend.filter(pl.col("direction").is_not_null() & (pl.col("sector_id") == "A"))
    for n in FWD_DAYS:
        sub = bt.filter(pl.col("fwd_days") == n)
        # 最后 n 天没有走完的前瞻窗口，不进统计
        assert sub["samples"].sum() == 3 * (scored_dates.height - n)
    up5 = bt.filter((pl.col("direction") == "up") & (pl.col("fwd_days") == 5)).row(0, named=True)
    assert up5["avg_ret"] > 0 and up5["beat_ratio"] == 1.0


def test_surge_signal_and_turn(params: Params) -> None:
    feat, market = _frames(today_net_a=5e8)
    day = feat["trade_date"].max()
    res = compute_horizons(feat, market, params, day, intraday=False, names={"A": "甲行业"})
    kinds = {(r["sector_id"], r["kind"]) for r in res.signals.iter_rows(named=True)}
    assert ("A", "surge_in") in kinds
    text = res.signals.filter(pl.col("kind") == "surge_in")["text"][0]
    assert text.startswith("甲行业 当天主力净流入 +5.00 亿")
    # 稳定上升的板块不是「转向」
    assert ("A", "turn_up") not in kinds
    assert res.trends.height == 3 and set(res.horizons["sector_id"]) == {"A", "B", "C", "market"}


def test_texts_have_no_forbidden_words(params: Params) -> None:
    feat, market = _frames(today_net_a=-5e8)
    day = feat["trade_date"].max()
    res = compute_horizons(feat, market, params, day, intraday=False)
    texts = list(res.signals["text"]) + [t for r in res.trends["reasons"] for t in json.loads(r)]
    assert texts
    for t in texts:
        assert not any(w in t for w in FORBIDDEN_WORDS), t


def test_pipeline_persists_and_api(db: Database, params: Params) -> None:
    dates = seed_market(db, n_days=90)
    with db.write() as conn:
        for d in dates[-2:]:
            run(conn, params, d)
        n = conn.execute(
            "SELECT count(*) FROM sector_horizon WHERE trade_date = ? AND segment = 'close'",
            [dates[-1]],
        ).fetchone()
        assert n is not None and n[0] == 4 * len(HORIZONS)
        trend = conn.execute(
            "SELECT count(*) FROM sector_trend WHERE trade_date = ?", [dates[-1]]
        ).fetchone()
        assert trend is not None and trend[0] == 3
        conn.execute(
            "INSERT INTO flow_signal (trade_date, segment, sector_id, kind, param_version, score, text, payload) "
            "VALUES (?, 'close', 'sw:801010', 'surge_in', ?, 4.0, '测试板块 当天主力净流入', '{\"level\": \"L1\"}')",
            [dates[-1], params.version],
        )
        conn.execute("INSERT INTO watchlist (code) VALUES ('sw:801020')")
        events = [
            e
            for e in collect_events(conn, dates[-1], None, params.version)
            if e.event.startswith("flow_")
        ]
        assert [e.subject_id for e in events if e.event == "flow_surge_in"] == ["sw:801010"]

    with TestClient(create_app(db, static_dir=None)) as c:
        data = c.get("/api/flow/horizons").json()["data"]
        assert [h["key"] for h in data["horizons"]] == [k for k, _, _ in HORIZONS]
        assert len(data["items"]) == 3
        assert set(data["items"][0]["h"]) == {k for k, _, _ in HORIZONS}
        assert data["market"]["5d"]["net_main"] is not None
        scores = [i["score"] for i in data["items"] if i["score"] is not None]
        assert scores == sorted(scores, reverse=True)

        detail = c.get("/api/flow/sectors/sw:801000").json()["data"]
        assert detail["trend"]["direction"] in ("up", "flat", "down")
        assert isinstance(detail["trend"]["reasons"], list)
        assert len(detail["history"]) == 2

        sig = c.get("/api/flow/signals").json()["data"]["items"]
        assert sig[0]["kind_zh"] == "资金异动流入"
        assert c.get("/api/flow/signals?kind=bogus").status_code == 422

        bt = c.get("/api/flow/backtest").json()["data"]["items"]
        assert bt and {r["fwd_days"] for r in bt} <= set(FWD_DAYS)


def test_intraday_default_picks_latest_segment(db: Database, params: Params) -> None:
    dates = seed_market(db, n_days=60)
    with db.write() as conn:
        run(conn, params, dates[-2])
        conn.execute(
            """
            INSERT OR REPLACE INTO flow_snapshot
            SELECT 'stock', code, trade_date, '1030_1130', now(),
                   net_main * 0.5, net_super * 0.5, net_large * 0.5, net_medium * 0.5, net_small * 0.5,
                   pct_chg, close, NULL
            FROM flow_daily WHERE trade_date = ?
            """,
            [dates[-1]],
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO flow_intraday
            SELECT code, trade_date, '1030_1130', net_main * 0.5, net_super * 0.5, net_large * 0.5,
                   net_medium * 0.5, net_small * 0.5, FALSE
            FROM flow_daily WHERE trade_date = ?
            """,
            [dates[-1]],
        )
        run(conn, params, dates[-1], segment="1030_1130")
    with TestClient(create_app(db, static_dir=None)) as c:
        body = c.get("/api/flow/horizons").json()
        assert body["meta"]["trade_date"] == dates[-1].isoformat()
        assert body["meta"]["segment"] == "1030_1130"
        assert len(body["data"]["items"]) == 3
        assert c.get("/api/flow/horizons?segment=close").json()["data"]["items"] == []
        prev = c.get(f"/api/flow/horizons?trade_date={dates[-2].isoformat()}").json()
        assert prev["meta"]["segment"] is None and len(prev["data"]["items"]) == 3


def test_empty_database(db: Database) -> None:
    with TestClient(create_app(db, static_dir=None)) as c:
        assert c.get("/api/flow/horizons").json()["data"]["items"] == []
        assert c.get("/api/flow/signals").json()["data"]["items"] == []
        assert c.get("/api/flow/sectors/x").json()["data"]["trend"] is None
        assert c.get("/api/flow/backtest").json()["data"]["items"] == []
