"""事后统计、简报、提醒与夜间维护。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb

from moneytool.compute.labels import compute_labels
from moneytool.compute.pipeline import run
from moneytool.config import NotifyConfig
from moneytool.notify import Event, _group, dispatch, in_quiet_hours, pending_quiet
from moneytool.params import Params
from moneytool.report import close_brief, premarket_brief, save_brief
from moneytool.storage.conn import Database
from moneytool.storage.maintenance import backup_database, prune_intraday
from moneytool.storage.migrate import migrate
from moneytool.types import FORBIDDEN_WORDS
from tests.compute.test_pipeline import seed_market


def _seed_runs(db: Database, params: Params, n_runs: int = 12) -> list[dt.date]:
    dates = seed_market(db, n_days=70)
    with db.write() as conn:
        for d in dates[-n_runs:]:
            run(conn, params, d)
    return dates


def test_labels_fill_completed_windows_once(db: Database, params: Params) -> None:
    dates = _seed_runs(db, params)
    entered = dates[-12]
    with db.write() as conn:
        conn.execute(
            "INSERT INTO tracking (list_type, code, sector_id, entered_date, param_version, entered_price) "
            "VALUES ('buy', '000001.SZ', 'sw:801000', ?, ?, 10.0)",
            [entered, params.version],
        )
        n = compute_labels(conn, dates[-1], params)
        rows = conn.execute(
            "SELECT horizon, ret, excess_vs_eqw, excess_vs_sector, max_drawdown FROM label_outcome "
            "WHERE list_type = 'buy' AND code = '000001.SZ' ORDER BY horizon"
        ).fetchall()
        assert [r[0] for r in rows] == [3, 5, 10]
        assert all(r[1] is not None and r[2] is not None and r[3] is not None for r in rows)
        assert all(r[4] <= 0.0 or r[4] is not None for r in rows)
        assert n >= 3
        # 已算过的不重复写
        before = conn.execute("SELECT count(*) FROM label_outcome").fetchone()
        compute_labels(conn, dates[-1], params)
        assert conn.execute("SELECT count(*) FROM label_outcome").fetchone() == before


def test_close_and_premarket_brief(db: Database, params: Params, tmp_path: Path) -> None:
    dates = _seed_runs(db, params, n_runs=4)
    with db.write() as conn:
        md = close_brief(conn, dates[-1], params.version)
        for section in (
            "## 市场",
            "## 主线板块",
            "## 阶段迁移",
            "## 买入关注",
            "## 卖出关注",
            "## 数据质量",
        ):
            assert section in md
        assert not any(w in md for w in FORBIDDEN_WORDS)
        save_brief(conn, dates[-1], "close", md, tmp_path)
        assert (tmp_path / f"{dates[-1].isoformat()}-close.md").read_text(encoding="utf-8") == md
        pre = premarket_brief(conn, dates[-1] + dt.timedelta(days=1), params.version, ["某提醒"])
        assert "某提醒" in pre and "## 需复核" in pre


def test_alerts_dedup_and_quiet(db: Database, params: Params) -> None:
    dates = _seed_runs(db, params, n_runs=3)
    day = dates[-1]
    cfg = NotifyConfig()
    with db.write() as conn:
        codes = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT code FROM action_list_confirmed WHERE trade_date = ? AND list_type IN ('buy', 'sell')",
                [day],
            ).fetchall()
        ]
        conn.execute("DELETE FROM action_list_confirmed WHERE trade_date < ?", [day])
        for c in codes:
            conn.execute("INSERT INTO watchlist (code) VALUES (?)", [c])
        noon = dt.datetime.combine(day, dt.time(15, 40))
        assert codes, "测试数据应在最后一日产出卖出关注"
        sent = dispatch(conn, day, None, params.version, cfg, noon, True)
        assert any("新进入卖出关注" in t for t in sent)
        assert not any(w in t for t in sent for w in FORBIDDEN_WORDS)
        # 同一标的同一事件当日只提醒一次
        assert dispatch(conn, day, None, params.version, cfg, noon, True) == []
        # 静默期只记录、不推送，进盘前简报
        conn.execute("DELETE FROM alert_log")
        night = dt.datetime.combine(day, dt.time(22, 0))
        assert dispatch(conn, day, None, params.version, cfg, night, True) == []
        assert any("新进入卖出关注" in t for t in pending_quiet(conn, day))


def test_alert_grouping_and_quiet_window() -> None:
    evs = [
        Event(f"00000{i}.SZ", "watch_buy_new", f"股{i} 新进入买入关注（一级0）", "sw:801000")
        for i in range(3)
    ]
    grouped = _group([*evs, Event("market", "gate_on", "市场风控开启")])
    assert len(grouped) == 2
    assert any(e.text == "一级0 3 只新进入买入关注" for e in grouped)
    cfg = NotifyConfig()
    assert in_quiet_hours(dt.datetime(2026, 1, 5, 21, 0), cfg)
    assert in_quiet_hours(dt.datetime(2026, 1, 5, 8, 0), cfg)
    assert not in_quiet_hours(dt.datetime(2026, 1, 5, 10, 0), cfg)


def test_prune_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "m.duckdb"
    db = Database(path)
    migrate(db.rw)
    old, new = dt.date(2026, 1, 5), dt.date(2026, 3, 2)
    with db.write() as conn:
        for d in (old, new):
            conn.execute(
                "INSERT INTO flow_intraday VALUES ('000001.SZ', ?, '0930_1030', 1, 1, 1, 1, 1, FALSE)",
                [d],
            )
            conn.execute(
                "INSERT INTO market_daily (trade_date, segment, param_version, regime, risk_gate, data_status) "
                "VALUES (?, '0930_1030', 'v2', 'none', FALSE, 'intraday'), (?, 'close', 'v2', 'none', FALSE, 'confirmed')",
                [d, d],
            )
        out = prune_intraday(conn, new, 30)
        assert out["flow_intraday"] == 1 and out["market_daily"] == 1
        segs = conn.execute("SELECT trade_date, segment FROM market_daily ORDER BY 1, 2").fetchall()
        assert (old, "close") in segs and (old, "0930_1030") not in segs
        backups = tmp_path / "backups"
        for i in range(4):
            backup_database(conn, path, backups, new + dt.timedelta(days=i), keep=2)
    names = sorted(p.name for p in backups.iterdir())
    assert names == ["m-20260304.duckdb", "m-20260305.duckdb"]
    restored = duckdb.connect(str(backups / names[-1]), read_only=True)
    assert restored.execute("SELECT count(*) FROM flow_intraday").fetchone() == (1,)
    restored.close()
    db.close()
