"""API 契约：每个响应带 meta；状态头；只读连接；名单为空时返回空列表而非报错。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from moneytool.api.server import create_app
from moneytool.compute.pipeline import run
from moneytool.params import Params
from moneytool.storage.conn import Database
from tests.compute.test_pipeline import seed_market


@pytest.fixture
def seeded(db: Database, params: Params) -> tuple[Database, list[dt.date]]:
    dates = seed_market(db, n_days=60)
    with db.write() as conn:
        for d in dates[-3:]:
            run(conn, params, d)
        # 盘中：用当日正式值的一半充当 10:30–11:30 累计快照，拆成两段增量
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
        for seg in ("0930_1030", "1030_1130"):
            conn.execute(
                """
                INSERT OR REPLACE INTO flow_intraday
                SELECT code, trade_date, ?, net_main * 0.25, net_super * 0.25, net_large * 0.25,
                       net_medium * 0.25, net_small * 0.25, FALSE
                FROM flow_daily WHERE trade_date = ?
                """,
                [seg, dates[-1]],
            )
        res = run(conn, params, dates[-1], segment="1030_1130")
        assert res.data_status.value == "intraday", res.notes
    return db, dates


@pytest.fixture
def client(seeded: tuple[Database, list[dt.date]]) -> Iterator[TestClient]:
    db, _ = seeded
    with TestClient(create_app(db, static_dir=None)) as c:
        yield c


def test_health_and_no_frontend(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/").json()["docs"] == "/api/docs"


def test_every_response_has_meta(
    client: TestClient, seeded: tuple[Database, list[dt.date]]
) -> None:
    _, dates = seeded
    last = dates[-1].isoformat()
    paths = [
        "/api/status",
        "/api/quality",
        "/api/search?q=股",
        "/api/params",
        "/api/market/overview",
        "/api/market/history?days=10",
        "/api/market/gate",
        "/api/sectors",
        "/api/sectors?level=L1&stage=start",
        "/api/sectors/sw:801000",
        "/api/sectors/sw:801000/history?days=20",
        "/api/sectors/compare?ids=sw:801000,sw:801010",
        "/api/stocks/000001.SZ",
        "/api/stocks/000001.SZ/history?days=20",
        "/api/stocks/000001.SZ/intraday",
        "/api/actions?list_type=buy",
        "/api/actions?list_type=buy_invalid",
        "/api/actions?list_type=hold_watch",
        f"/api/actions?list_type=point&trade_date={last}&segment=1030_1130",
        "/api/tracking",
        "/api/tracking?active_only=false",
        "/api/stats",
        "/api/briefs",
        "/api/marks",
        "/api/watchlist",
        f"/api/market/overview?trade_date={last}&segment=1030_1130",
        f"/api/sectors?trade_date={last}&segment=1030_1130",
    ]
    for path in paths:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        body = r.json()
        assert "meta" in body and "status" in body["meta"], path
        assert r.headers.get("X-Data-Status") == body["meta"]["status"], path


def test_overview_and_board(client: TestClient, seeded: tuple[Database, list[dt.date]]) -> None:
    _, dates = seeded
    r = client.get("/api/market/overview").json()
    assert r["meta"]["trade_date"] == dates[-1].isoformat()
    assert r["meta"]["status"] == "confirmed"
    assert r["data"]["regime"] in ("clear", "scattered", "none")
    assert set(r["data"]["stage_counts"]["L1"]) == {
        "freeze",
        "start",
        "spread",
        "climax",
        "diverge",
        "ebb",
    }
    board = client.get("/api/sectors").json()
    assert len(board["data"]) == 3
    row = board["data"][0]
    assert {"sector_id", "name", "stage", "evidence", "metrics"} <= set(row)
    assert isinstance(row["evidence"], dict)
    assert "market_share" in row["metrics"]


def test_intraday_segment_status(
    client: TestClient, seeded: tuple[Database, list[dt.date]]
) -> None:
    _, dates = seeded
    r = client.get(f"/api/sectors?trade_date={dates[-1].isoformat()}&segment=1030_1130").json()
    assert r["meta"]["status"] == "intraday"
    assert r["meta"]["segment"] == "1030_1130"
    bad = client.get("/api/sectors?segment=nope")
    assert bad.status_code == 422


def test_sector_detail(client: TestClient) -> None:
    r = client.get("/api/sectors/sw:801000").json()
    data = r["data"]
    assert data["sector"]["level"] == "L1"
    assert data["stage"]["stage"] in ("freeze", "start", "spread", "climax", "diverge", "ebb")
    assert "market_share" in data["stage"]["metrics"]
    assert "sector_net_main" in data["features"]
    assert len(data["members"]) == 10
    assert data["members"][0]["metrics"].keys() >= {"ret_5d", "is_limit_up"}
    assert client.get("/api/sectors/sw:999999").status_code == 404


def test_stock_detail_and_missing(client: TestClient) -> None:
    r = client.get("/api/stocks/000001.SZ").json()
    assert r["data"]["security"]["code"] == "000001.SZ"
    assert r["data"]["sectors"][0]["sector_id"] == "sw:801000"
    assert r["data"]["features"]["main_ratio"] is not None
    roles = r["data"]["roles"]
    # 测试数据只有一级：行业依据板块回退一级，每只股票都有一行行业角色
    assert [x["sector_id"] for x in roles] == ["sw:801000"]
    assert roles[0]["role"] in ("core", "follow", "avoid", "other")
    prof = r["data"]["profile"]
    assert prof["basis_sector"] == "sw:801000"
    assert prof["identity"]["indices"] == ["指数外"]
    assert set(prof["exclusions"]) == {"control", "crash", "untradable"}
    assert prof["score"] is None or 0 <= prof["score"] <= 100
    names = {i["index_name"] for i in r["data"]["indices"]}
    assert names == {"stock_risk", "retail_pressure"}
    assert r["data"]["hints"][0]["text"].startswith("股000001 在一级0")
    assert client.get("/api/stocks/999999.SZ").status_code == 404


def test_missing_day_reports_missing_status(client: TestClient) -> None:
    r = client.get("/api/market/overview?trade_date=2030-01-01").json()
    assert r["meta"]["status"] == "missing"
    assert r["meta"]["reason"]
    assert r["data"] is None


def test_actions_empty_lists(client: TestClient) -> None:
    for lt in ("buy", "sell", "point", "lowbase"):
        r = client.get(f"/api/actions?list_type={lt}").json()
        assert r["data"] == []
    assert client.get("/api/actions?list_type=hold").status_code == 422


def test_marks_and_watchlist_roundtrip(client: TestClient) -> None:
    r = client.post("/api/marks", json={"code": "000001.SZ", "mark": "bought", "note": "试"})
    assert r.status_code == 201
    assert client.get("/api/marks?code=000001.SZ").json()["data"][0]["mark"] == "bought"
    assert client.post("/api/marks", json={"code": "000001.SZ", "mark": "hold"}).status_code == 422

    assert client.post("/api/watchlist/000002.SZ").status_code == 201
    assert client.post("/api/watchlist/999999.SZ").status_code == 404
    wl = client.get("/api/watchlist").json()["data"]
    assert [w["code"] for w in wl] == ["000002.SZ"]
    assert client.delete("/api/watchlist/000002.SZ").status_code == 200
    assert client.get("/api/watchlist").json()["data"] == []


def test_watchlist_sector_metrics_come_from_features(
    client: TestClient, seeded: tuple[Database, list[dt.date]]
) -> None:
    """板块自选不能去 flow_daily 按代码找，涨跌和资金在板块特征里。"""
    db, dates = seeded
    day = dates[-1]
    with db.write() as conn:
        expected = conn.execute(
            """
            SELECT TRY_CAST(json_extract(features, '$.sector_pct_chg') AS DOUBLE),
                   TRY_CAST(json_extract(features, '$.sector_net_main') AS DOUBLE),
                   TRY_CAST(json_extract(features, '$.sector_main_ratio') AS DOUBLE)
            FROM feature_daily
            WHERE subject_type = 'sector' AND subject_id = 'sw:801000'
              AND trade_date = ? AND segment = 'close'
            """,
            [day],
        ).fetchone()
        stock = conn.execute(
            """
            SELECT b.pct_chg, b.close, f.net_main,
                   TRY_CAST(json_extract(fd.features, '$.main_ratio') AS DOUBLE)
            FROM bar_daily b
            JOIN flow_daily f ON f.code = b.code AND f.trade_date = b.trade_date
            LEFT JOIN feature_daily fd ON fd.subject_type = 'stock' AND fd.subject_id = b.code
                 AND fd.trade_date = b.trade_date AND fd.segment = 'close'
            WHERE b.code = '000002.SZ' AND b.trade_date = ?
            """,
            [day],
        ).fetchone()
    assert expected is not None and stock is not None
    assert client.post("/api/watchlist/sw:801000").status_code == 201
    assert client.post("/api/watchlist/000002.SZ").status_code == 201
    rows = {w["code"]: w for w in client.get("/api/watchlist").json()["data"]}
    sector = rows["sw:801000"]
    assert sector["kind"] == "sector"
    assert sector["pct_chg"] == pytest.approx(expected[0])
    assert sector["net_main"] == pytest.approx(expected[1])
    assert sector["main_ratio"] == pytest.approx(expected[2])
    assert rows["000002.SZ"]["kind"] == "stock"
    assert rows["000002.SZ"]["pct_chg"] == pytest.approx(stock[0])
    assert rows["000002.SZ"]["close"] == pytest.approx(stock[1])
    assert rows["000002.SZ"]["net_main"] == pytest.approx(stock[2])
    assert rows["000002.SZ"]["main_ratio"] == pytest.approx(stock[3])


def test_tracking_return_uses_adjust_factor(
    client: TestClient, seeded: tuple[Database, list[dt.date]]
) -> None:
    """除权后原始收盘会看起来下跌；至今收益按复权价，与事后统计一致。"""
    db, dates = seeded
    entered, last = dates[-3], dates[-1]
    with db.write() as conn:
        version = conn.execute(
            "SELECT param_version FROM market_daily WHERE trade_date = ? AND segment = 'close'",
            [last],
        ).fetchone()
        assert version is not None
        conn.execute(
            "UPDATE bar_daily SET close = 10, adj_factor = 1 WHERE code = '000001.SZ' AND trade_date = ?",
            [entered],
        )
        conn.execute(
            "UPDATE bar_daily SET close = 6, adj_factor = 2 WHERE code = '000001.SZ' AND trade_date = ?",
            [last],
        )
        conn.execute(
            "INSERT INTO tracking (list_type, code, sector_id, entered_date, param_version, entered_price) "
            "VALUES ('buy', '000001.SZ', 'sw:801000', ?, ?, 10)",
            [entered, version[0]],
        )
    row = client.get("/api/tracking").json()["data"][0]
    assert row["code"] == "000001.SZ"
    assert row["ret_since"] == pytest.approx(0.2)
    assert row["excess_vs_eqw"] is not None
    assert row["excess_vs_sector"] is not None


def test_search(client: TestClient) -> None:
    r = client.get("/api/search?q=000001").json()["data"]
    assert r["stocks"][0]["code"] == "000001.SZ"
    r = client.get("/api/search?q=一级").json()["data"]
    assert len(r["sectors"]) == 3


def test_token_required_when_set(seeded: tuple[Database, list[dt.date]]) -> None:
    db, _ = seeded
    with TestClient(create_app(db, token="secret", static_dir=None)) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers={"Authorization": "Bearer secret"}).status_code == 200
        assert c.get("/api/status?token=secret").status_code == 200


def test_admin_recompute_requires_runner(client: TestClient) -> None:
    r = client.post("/api/admin/recompute", json={"trade_date": "2026-01-05"})
    assert r.status_code == 503
