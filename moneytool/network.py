"""数据源代理设置。AkShare（requests）与 httpx 都读 HTTP(S)_PROXY / NO_PROXY 环境变量，
Windows 下还会读系统代理（注册表）；这里在进程启动时按 `[network] proxy` 统一设置。"""

from __future__ import annotations

import os
import urllib.request

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


def apply_network(settings: Settings) -> str:
    """返回当前生效方式的说明，供 doctor 展示。"""
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
