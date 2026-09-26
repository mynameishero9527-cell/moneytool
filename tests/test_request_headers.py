from __future__ import annotations

from typing import Any

import pytest
import requests

from moneytool.network import BROWSER_UA, install_request_headers


def test_eastmoney_requests_get_referer(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def fake_send(self: requests.Session, req: requests.PreparedRequest, **kw: Any) -> Any:
        sent.append(dict(req.headers))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"{}"
        return resp

    monkeypatch.setattr(requests.Session, "send", fake_send)
    install_request_headers()
    install_request_headers()

    requests.get("https://push2his.eastmoney.com/api/x", headers={"User-Agent": "akshare-ua"})
    requests.get("https://push2.eastmoney.com/api/qt/clist/get")
    requests.get("https://example.org/")

    assert sent[0]["Referer"] == "https://data.eastmoney.com/"
    assert sent[0]["User-Agent"] == "akshare-ua"
    assert sent[1]["Referer"] == "https://quote.eastmoney.com/"
    assert sent[1]["User-Agent"] == BROWSER_UA
    assert "Referer" not in sent[2]
