"""数据源网络设置。

- 代理：AkShare（requests）与 httpx 都读 HTTP(S)_PROXY / NO_PROXY 环境变量，Windows 下还会读系统代理
  （注册表）；这里在进程启动时按 `[network] proxy` 统一设置。
- 请求头：东方财富接口不带 Referer 时直接断开连接（RemoteDisconnected），而 AkShare 只发 User-Agent，
  这里给所有发往 eastmoney.com 的 requests 请求补上浏览器请求头。
- 超时：AkShare 多数接口调用 requests 时不传 timeout，连接卡住会永远等下去（曾卡死启动时的参考数据同步），
  这里给没传 timeout 的请求补上默认值（连接 / 读取秒数）。
"""

from __future__ import annotations

import os
import urllib.request
from typing import Any
from urllib.parse import urlsplit

import requests

from moneytool.config import Settings

# 各数据源实际访问的域名（含 AkShare 内部调用），按后缀匹配子域名
SOURCE_DOMAINS = (
    "eastmoney.com",
    "csindex.com.cn",
    "swsresearch.com",
    "baostock.com",
    "legulegu.com",
    "sina.com.cn",
    "joinquant.com",
)

DEFAULT_TIMEOUT = (10.0, 30.0)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def eastmoney_headers(host: str) -> dict[str, str]:
    referer = (
        "https://quote.eastmoney.com/"
        if host.startswith("push2.") or ".push2." in host
        else "https://data.eastmoney.com/"
    )
    return {"User-Agent": BROWSER_UA, "Referer": referer, "Accept-Language": "zh-CN,zh;q=0.9"}


def install_request_headers() -> None:
    """包装 `requests.Session.request`：发往 eastmoney.com 的请求缺哪个头补哪个，已有的不动；
    未指定超时的请求一律补默认超时。可重复调用。"""
    if getattr(requests.Session.request, "_moneytool_headers", False):
        return
    original = requests.Session.request

    def request(self: requests.Session, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        host = urlsplit(str(url)).hostname or ""
        if host == "eastmoney.com" or host.endswith(".eastmoney.com"):
            headers = dict(kwargs.get("headers") or {})
            present = {k.lower() for k in headers}
            for k, v in eastmoney_headers(host).items():
                if k.lower() not in present:
                    headers[k] = v
            kwargs["headers"] = headers
        if (
            kwargs.get("timeout") is None and len(args) < 7
        ):  # method、url 之后第 7 个位置参数是 timeout
            kwargs["timeout"] = DEFAULT_TIMEOUT
        return original(self, method, url, *args, **kwargs)

    request._moneytool_headers = True  # type: ignore[attr-defined]
    requests.Session.request = request  # type: ignore[method-assign,assignment]


def apply_network(settings: Settings) -> str:
    """返回当前生效方式的说明，供 doctor 展示。"""
    install_request_headers()
    mode = settings.network.proxy.strip()
    if mode == "system":
        return "沿用系统代理"
    if mode == "direct" or not mode:
        existing = [h for h in os.environ.get("NO_PROXY", "").split(",") if h.strip()]
        merged = ",".join(dict.fromkeys([*existing, *SOURCE_DOMAINS]))
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged
        return "数据源直连（不走代理）"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[key] = mode
    return f"使用代理 {mode}"


def system_proxies() -> dict[str, str]:
    """系统 / 环境里配置的代理（Windows 含注册表 Internet 设置）。"""
    return {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")}
