"""计算链 ⑤–⑦：角色 / 名单规则、上限、指数范围、提示禁用词、排除标签、跟踪重算安全。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import polars as pl

from moneytool.compute.actions import _cap_buy, build_buy
from moneytool.compute.extra import today_amount_peak, today_own_percentiles
from moneytool.compute.hints import hold_eval, sector_hint, stock_hint
from moneytool.compute.pipeline import run
from moneytool.compute.profile import crash_history
from moneytool.params import Params
from moneytool.rules.stock import avoid_rules, buy_conditions, rule_core, rule_invalid, rule_strong
from moneytool.storage.conn import Database
from moneytool.storage.repo import upsert
from moneytool.types import FORBIDDEN_WORDS
from tests.compute.test_pipeline import seed_market
from tests.conftest import trading_dates


def _strong_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "code": "000001.SZ",
        "sector_id": "sw:801010",
        "basis_level": "L2",
        "group": "industry",
        "upper_id": "sw:801000",
        "rs_5d_sector": 0.03,
        "rs_3d_sector": 0.01,
        "ratio_top": 0.05,
        "rs_top": 0.05,
        "retention_5d": 3e7,
        "inflow_days_5d": 4,
        "dd_vs_sector": 0.01,
        "sector_ret_5d": 0.02,
        "outflow_days_5d": 1,
        "turnover_vs_20d": 1.1,
        "ret_5d": 0.03,
        "net_main_5d": 4e7,
        "price_strong": True,
        "pulse_giveback": False,
        "stage_entry": True,
        "parent_ebb": False,
        "role_ok": True,
        "pos_limit": 0.04,
        "drawdown_20d_pct_20d": 0.5,
        "buy_tradable": True,
        "excluded": False,
        "limit_outflow": False,
        "is_strong": True,
        "role": "core",
        "stage": "start",
        "overdraft": False,
    }
    row.update(over)
    return row


def test_strong_core_and_invalid(params: Params) -> None:
    row = _strong_row()
    assert rule_strong(row, params).hit
    assert rule_core(row, params).hit
    assert not rule_invalid(row, params).hit
    weak = _strong_row(ratio_top=0.6)
    assert not rule_strong(weak, params).hit
    assert rule_invalid(weak, params).hit
    ev = rule_strong(_strong_row(retention_5d=None), params).evidence
    assert any(e.note.endswith("数据缺失）") and not e.hit for e in ev)


def test_avoid_types(params: Params) -> None:
    follow_down = _strong_row(rs_5d_sector=-0.02, ratio_top=0.9)
    assert avoid_rules(follow_down, params)["follow_down"].hit
    bleed = _strong_row(outflow_days_5d=4, retention_5d=-1e6)
    assert avoid_rules(bleed, params)["bleed"].hit
    diverge = _strong_row(net_main_5d=-1e7, ratio_top=0.8)
    hits = avoid_rules(diverge, params)
    assert hits["diverge_out"].hit
    assert not avoid_rules(_strong_row(), params)["diverge_out"].hit


def test_buy_conditions_and_gate(params: Params) -> None:
    conds = buy_conditions(_strong_row(), params)
    assert all(e.hit for e in conds.values())
    over = buy_conditions(_strong_row(ret_5d=0.2), params)
    assert not over["position"].hit

    df = pl.DataFrame([_strong_row(), _strong_row(code="000002.SZ", ret_5d=0.2)])
    out = build_buy(df, set(), False, {}, params)
    assert [e["code"] for e in out if e["list_type"] == "buy"] == ["000001.SZ"]
    # 风控开启：不新增；已有条目保留并标「风控期间」
    assert build_buy(df, set(), True, {}, params) == []
    kept = build_buy(df, {("000001.SZ", "sw:801010"), ("000002.SZ", "sw:801010")}, True, {}, params)
    buy = [e for e in kept if e["list_type"] == "buy"]
    assert len(buy) == 1 and "风控期间" in json.loads(buy[0]["tags"])
    invalid = [e for e in kept if e["list_type"] == "buy_invalid"]
    assert [e["code"] for e in invalid] == ["000002.SZ"]
    assert json.loads(invalid[0]["invalidation"])


def test_buy_caps_per_sector_and_l1(params: Params) -> None:
    def entry(code: str, sector: str, retention: float) -> dict[str, Any]:
        return {
            "code": code,
            "sector_id": sector,
            "tags": "[]",
            "_retention": retention,
            "_l1": "sw:801000",
            "_group": "industry",
            "_high_risk": False,
        }

    entries = [entry(f"{i:06d}.SZ", f"sw:80101{i % 3}", float(i)) for i in range(12)]
    out = _cap_buy(entries, params)
    per_sector: dict[str, int] = {}
    for e in out:
        per_sector[e["sector_id"]] = per_sector.get(e["sector_id"], 0) + 1
    assert max(per_sector.values()) <= params.buy.per_sector_max
    assert len(out) == params.buy.per_l1_max
    assert all("一级已截断" in json.loads(e["tags"]) for e in out)
    # 按留存降序截断
    assert out[0]["_retention"] == 11.0


def test_today_percentiles_and_peak() -> None:
    days = trading_dates(30)
    df = pl.DataFrame(
        {
            "code": ["A"] * 30,
            "trade_date": days,
            "x": [float(i) for i in range(30)],
            "amount": [1.0] * 25 + [10.0, 2.0, 2.0, 2.0, 2.0],
            "close_adj": [10.0] * 25 + [10.0, 9.5, 9.2, 9.0, 8.8],
        }
    )
    pct = today_own_percentiles(df, "code", days[-1], {"p": ("x", 20)}, 0.6)
    assert pct["p"][0] == 1.0
    too_short = today_own_percentiles(df, "code", days[-1], {"p": ("x", 100)}, 0.6)
    assert too_short["p"][0] is None
    peak = today_amount_peak(df, days[-1], 20)
    assert peak["amount_peak_days"][0] == 4
    assert abs(peak["amount_peak_drop"][0] - (8.8 / 10.0 - 1)) < 1e-9


def test_crash_history_counts_events(db: Database, params: Params) -> None:
    days = trading_dates(120)
    closes = [10.0] * 20 + [10.0 * (1.03**i) for i in range(1, 21)]
    peak = closes[-1]
    closes += [peak * (1 - 0.015 * i) for i in range(1, 41)]
    closes += [closes[-1]] * (120 - len(closes))
    bars = pl.DataFrame(
        {
            "code": ["000001.SZ"] * 120,
            "trade_date": days,
            "close": closes,
            "high": closes,
            "limit_up": [c * 1.1 for c in closes],
            "pct_chg": [0.0] * 120,
            "adj_factor": [1.0] * 120,
            "is_suspended": [False] * 120,
        }
    )
    with db.write() as conn:
        upsert(conn, "bar_daily", bars)
        out = crash_history(conn, days[-1], params)
    row = out.row(0, named=True)
    assert row["crash_events"] == 1
    assert row["blowups"] == 0


def test_hints_avoid_forbidden_words(params: Params) -> None:
    rows = [
        {
            "sector_id": "s1",
            "name": "板块",
            "stage": "start",
            "days_in_stage": 2,
            "risk_total": 30,
            "price_flow_agree": True,
        },
        {"sector_id": "s2", "name": "板块", "stage": "spread", "half": "second", "risk_total": 65},
        {
            "sector_id": "s3",
            "name": "板块",
            "stage": "ebb",
            "risk_total": 85,
            "hype_score": 3,
            "upper_id": "s1",
        },
    ]
    tiers = []
    for r in rows:
        h = sector_hint(r, params, False, {"s1": "一级"})
        tiers.append(h["tier"])
        text = "".join(s["text"] for s in h["sentences"])
        assert not any(w in text for w in FORBIDDEN_WORDS), text
        assert 1 <= len(h["sentences"]) <= 4
    assert tiers == ["focus", "watch", "avoid"]
    assert sector_hint(rows[0], params, True, {})["tier"] == "avoid"

    stock = stock_hint(
        {
            "code": "000001.SZ",
            "name": "某股",
            "sector_id": "s1",
            "stage": "start",
            "role": "core",
            "hold_eval": "intact",
            "actions": ["buy"],
            "points": ["start_confirm"],
            "risk_total": 45,
            "risk_items": {
                "position": 12,
                "divergence": 0,
                "volatility": 8,
                "liquidity": 5,
                "structure": 20,
            },
            "retail_total": 70,
            "control_risk": True,
            "amount_tier_zh": "微",
            "indices": ["指数外"],
        },
        {"s1": "一级"},
    )
    text = "".join(s["text"] for s in stock["sentences"])
    assert text.startswith("某股 在一级（启动）中为核心，持有结构完好。")
    assert len(stock["sentences"]) == 4
    assert not any(w in text for w in FORBIDDEN_WORDS)


def test_hold_eval_tiers(params: Params) -> None:
    base = {
        "role": "core",
        "stage": "spread",
        "half": "first",
        "risk_total": 30,
        "retail_total": 10,
    }
    assert hold_eval(base, params, False)[0] == "intact"
    assert hold_eval({**base, "risk_total": 65}, params, False)[0] == "review"
    assert hold_eval(base, params, True)[0] == "review"
    assert hold_eval({**base, "sell_points": ["diverge"]}, params, False) == ("broken", ["背离"])


def test_chain_end_to_end_and_recompute_is_stable(db: Database, params: Params) -> None:
    dates = seed_market(db, n_days=80)
    with db.write() as conn:
        # 自选一只股票，使卖出关注 / 持有评估有样本
        conn.execute("INSERT INTO watchlist (code) VALUES ('000001.SZ')")
        for d in dates[-5:]:
            res = run(conn, params, d)
            assert res.chain_counts["profiles"] == 30
        last = dates[-1]
        idx = conn.execute(
            "SELECT index_name, count(*), min(total), max(total) FROM index_daily WHERE trade_date = ? GROUP BY 1",
            [last],
        ).fetchall()
        by_name = {r[0]: r for r in idx}
        assert set(by_name) == {"sector_sentiment", "sector_risk", "stock_risk", "retail_pressure"}
        for _, n, lo, hi in idx:
            assert n > 0
            assert lo is None or 0 <= lo <= hi <= 100
        texts = [
            r[0]
            for r in conn.execute("SELECT text FROM hint WHERE trade_date = ?", [last]).fetchall()
        ]
        assert len(texts) == 33
        assert not any(w in t for t in texts for w in FORBIDDEN_WORDS)
        hold = conn.execute(
            "SELECT eval FROM hold_eval WHERE trade_date = ? AND code = '000001.SZ'", [last]
        ).fetchall()
        assert hold and hold[0][0] in ("intact", "review", "broken")
        att = conn.execute(
            "SELECT attribution FROM sector_stage_confirmed WHERE trade_date = ? LIMIT 1", [last]
        ).fetchone()
        assert att is not None and "hype" in json.loads(att[0])

        def snapshot() -> tuple[Any, ...]:
            return (
                conn.execute("SELECT count(*) FROM tracking").fetchone(),
                conn.execute(
                    "SELECT list_type, count(*) FROM action_list_confirmed WHERE trade_date = ? GROUP BY 1 ORDER BY 1",
                    [last],
                ).fetchall(),
                conn.execute(
                    "SELECT count(*) FROM stock_role_confirmed WHERE trade_date = ?", [last]
                ).fetchone(),
            )

        before = snapshot()
        run(conn, params, last)
        assert snapshot() == before


def test_intraday_chain_writes_intraday_tables(db: Database, params: Params) -> None:
    dates = seed_market(db, n_days=60)
    day = dates[-1]
    with db.write() as conn:
        for d in dates[-3:-1]:
            run(conn, params, d)
        conn.execute(
            """
            INSERT INTO flow_snapshot
            SELECT 'stock', code, trade_date, '0930_1030', now(), net_main * 0.3, net_super * 0.3,
                   net_large * 0.3, net_medium * 0.3, net_small * 0.3, pct_chg, close, 3e7
            FROM flow_daily WHERE trade_date = ?
            """,
            [day],
        )
        conn.execute(
            """
            INSERT INTO flow_intraday
            SELECT code, trade_date, '0930_1030', net_main * 0.3, net_super * 0.3, net_large * 0.3,
                   net_medium * 0.3, net_small * 0.3, FALSE
            FROM flow_daily WHERE trade_date = ?
            """,
            [day],
        )
        res = run(conn, params, day, segment="0930_1030")
        assert res.data_status.value == "intraday"
        roles = conn.execute(
            "SELECT count(*) FROM stock_role_intraday WHERE trade_date = ? AND segment = '0930_1030'",
            [day],
        ).fetchone()
        assert roles is not None and roles[0] == 30
        tags = conn.execute(
            "SELECT tags FROM stock_role_intraday WHERE trade_date = ? LIMIT 1", [day]
        ).fetchone()
        assert tags is not None and "盘中" in json.loads(tags[0])
        confirmed = conn.execute(
            "SELECT count(*) FROM stock_role_confirmed WHERE trade_date = ?", [day]
        ).fetchone()
        assert confirmed == (0,)
        assert conn.execute(
            "SELECT count(*) FROM tracking WHERE entered_date = ?", [day]
        ).fetchone() == (0,)


def test_dates_helper_is_weekdays() -> None:
    assert all(d.weekday() < 5 for d in trading_dates(10))
    assert isinstance(trading_dates(1)[0], dt.date)
