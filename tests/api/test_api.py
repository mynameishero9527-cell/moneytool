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
                   pct_chg, close
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
        "/api/tracking",
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
    assert r["data"]["roles"] == []
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
