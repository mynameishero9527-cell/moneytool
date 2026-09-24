from __future__ import annotations

import datetime as dt
import json
import random

import polars as pl

from moneytool.compute.pipeline import run
from moneytool.params import Params
from moneytool.storage.conn import Database
from moneytool.storage.repo import upsert
from tests.conftest import trading_dates


def seed_market(
    db: Database, n_days: int = 70, n_sectors: int = 3, per_sector: int = 10
) -> list[dt.date]:
    """两级行业、成分、日历、日线与资金流：随机但有结构（板块 0 强、板块 2 弱）。"""
    rng = random.Random(7)
    dates = trading_dates(n_days)
    codes = [f"{i:06d}.SZ" for i in range(1, n_sectors * per_sector + 1)]
    with db.write() as conn:
        upsert(
            conn,
            "trade_calendar",
            pl.DataFrame({"trade_date": dates, "is_open": [True] * len(dates)}),
        )
        upsert(
            conn,
            "security",
            pl.DataFrame(
                {
                    "code": codes,
                    "name": [f"股{c[:6]}" for c in codes],
                    "exchange": ["SZ"] * len(codes),
                    "board": ["深市主板"] * len(codes),
                    "list_date": [dt.date(2015, 1, 1)] * len(codes),
                    "is_st": [False] * len(codes),
                    "is_delisting": [False] * len(codes),
                }
            ),
        )
        l1 = pl.DataFrame(
            {
                "sector_id": [f"sw:8010{i}0" for i in range(n_sectors)],
                "name": [f"一级{i}" for i in range(n_sectors)],
                "level": ["L1"] * n_sectors,
                "parent_id": [None] * n_sectors,
                "source": ["shenwan"] * n_sectors,
            },
            schema_overrides={"parent_id": pl.Utf8},
        )
        upsert(conn, "sector", l1)
        member_rows = []
        for s in range(n_sectors):
            for j in range(per_sector):
                member_rows.append((f"sw:8010{s}0", codes[s * per_sector + j], dates[0]))
        upsert(
            conn,
            "sector_member_snapshot",
            pl.DataFrame(member_rows, schema=["sector_id", "code", "snapshot_date"], orient="row"),
        )
        bars = []
        flows = []
        for s in range(n_sectors):
            drift = {0: 0.004, 1: 0.0, 2: -0.004}[s]
            flow_bias = {0: 2e6, 1: 0.0, 2: -2e6}[s]
            for j in range(per_sector):
                code = codes[s * per_sector + j]
                price = 10.0
                for d in dates:
                    r = drift + rng.gauss(0, 0.01)
                    pre = price
                    price = round(price * (1 + r), 2)
                    amount = 5e7 + rng.random() * 5e7
                    net = flow_bias + rng.gauss(0, 1e6)
                    bars.append(
                        {
                            "code": code,
                            "trade_date": d,
                            "open": pre,
                            "high": max(pre, price) * 1.01,
                            "low": min(pre, price) * 0.99,
                            "close": price,
                            "pre_close": pre,
                            "volume": amount / price,
                            "amount": amount,
                            "turnover": 0.02,
                            "pct_chg": price / pre - 1,
                            "adj_factor": 1.0,
                            "limit_up": round(pre * 1.1, 2),
                            "limit_down": round(pre * 0.9, 2),
                            "float_mv": 5e9,
                            "is_suspended": False,
                        }
                    )
                    flows.append(
                        {
                            "code": code,
                            "trade_date": d,
                            "net_main": net,
                            "net_super": net * 0.6,
                            "net_large": net * 0.4,
                            "net_medium": -net * 0.5,
                            "net_small": -net * 0.5,
                            "close": price,
                            "pct_chg": price / pre - 1,
                            "reconciled": True,
                        }
                    )
        upsert(conn, "bar_daily", pl.DataFrame(bars))
        upsert(conn, "flow_daily", pl.DataFrame(flows))
    return dates


def test_pipeline_confirmed_end_to_end(db: Database, params: Params) -> None:
    dates = seed_market(db)
    with db.write() as conn:
        for d in dates[-3:]:
            res = run(conn, params, d)
            assert res.data_status.value == "confirmed"
            assert res.stage_rows == 3
        stages = conn.execute(
            "SELECT sector_id, stage, days_in_stage, evidence FROM sector_stage_confirmed WHERE trade_date = ?",
            [dates[-1]],
        ).pl()
        assert stages.height == 3
        # 第 3 日：无迁移时停留天数递增
        assert stages["days_in_stage"].max() >= 1
        ev = json.loads(stages["evidence"][0])
        assert set(ev) >= {"ebb", "start", "half", "pulse"}
        market = conn.execute("SELECT * FROM market_daily WHERE trade_date = ?", [dates[-1]]).pl()
        assert market.height == 1
        assert market["risk_gate"][0] in (True, False)
        assert market["regime"][0] in ("clear", "scattered", "none")
        feats = conn.execute(
            "SELECT subject_type, count(*) FROM feature_daily WHERE trade_date = ? AND segment = 'close' GROUP BY 1",
            [dates[-1]],
        ).fetchall()
        assert dict(feats) == {"stock": 30, "sector": 3}
        # 幂等：重跑同一日结果行数不变
        run(conn, params, dates[-1])
        n = conn.execute(
            "SELECT count(*) FROM sector_stage_confirmed WHERE trade_date = ?", [dates[-1]]
        ).fetchone()
        assert n == (3,)


def test_pipeline_degraded_when_flow_missing(db: Database, params: Params) -> None:
    dates = seed_market(db, n_days=40)
    with db.write() as conn:
        conn.execute("DELETE FROM flow_daily WHERE trade_date = ?", [dates[-1]])
        res = run(conn, params, dates[-1])
        assert res.data_status.value == "degraded"
        row = conn.execute(
            "SELECT data_status FROM market_daily WHERE trade_date = ?", [dates[-1]]
        ).fetchone()
        assert row == ("degraded",)
