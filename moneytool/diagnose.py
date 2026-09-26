"""诊断报告（`moneytool doctor`）：环境、进程与锁、数据库状态、回补进度、失败任务、数据质量、
数据源连通、日志错误摘要与日志末尾，末尾汇总发现的问题。

主进程运行时 DuckDB 文件被其独占，此时改从本机 `/api/diagnose` 取数据库部分。
"""

from __future__ import annotations

import datetime as dt
import json
import platform
import sys
from pathlib import Path
from typing import Any

import duckdb

from moneytool import __version__
from moneytool.config import Settings
from moneytool.network import apply_network, system_proxies

LOG_ERROR_LEVELS = ("error", "critical", "exception")
TEXT_ERROR_MARKERS = ("Traceback", "ERROR", "Exception")


def db_summary(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """数据库部分；直接读库与 `/api/diagnose` 共用。"""

    def one(sql: str, args: list[Any] | None = None) -> Any:
        row = conn.execute(sql, args or []).fetchone()
        return None if row is None else row[0]

    total = int(one("SELECT count(*) FROM security WHERE NOT is_delisting") or 0)
    backfill: dict[str, dict[str, int]] = {}
    for task, status, n in conn.execute(
        "SELECT task, status, count(*) FROM backfill_progress GROUP BY task, status"
    ).fetchall():
        backfill.setdefault(str(task), {})[str(status)] = int(n)
    for task in ("bars", "flow_daily"):
        backfill.setdefault(task, {})["total"] = total

    def records(sql: str) -> list[dict[str, Any]]:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        return [
            {
                c: (str(v) if isinstance(v, dt.date | dt.datetime) else v)
                for c, v in zip(cols, r, strict=True)
            }
            for r in cur.fetchall()
        ]

    # TIMESTAMPTZ 转 Python 需要 pytz，这里一律在 SQL 里转成文本
    return {
        "counts": {
            "security": int(one("SELECT count(*) FROM security") or 0),
            "sector": int(one("SELECT count(*) FROM sector") or 0),
            "concept": int(one("SELECT count(*) FROM sector WHERE level = 'concept'") or 0),
            "calendar_max": str(one("SELECT max(trade_date) FROM trade_calendar")),
            "bar_days": int(one("SELECT count(DISTINCT trade_date) FROM bar_daily") or 0),
            "flow_days": int(one("SELECT count(DISTINCT trade_date) FROM flow_daily") or 0),
            "confirmed_days": int(
                one("SELECT count(DISTINCT trade_date) FROM market_daily WHERE segment = 'close'")
                or 0
            ),
            "latest_confirmed": str(
                one("SELECT max(trade_date) FROM market_daily WHERE segment = 'close'")
            ),
        },
        "backfill": backfill,
        "last_jobs": records(
            "SELECT job, CAST(max(started_at) AS VARCHAR) AS started_at, arg_max(ok, started_at) AS ok, "
            "arg_max(error, started_at) AS error FROM job_run GROUP BY job ORDER BY max(started_at) DESC"
        ),
        "failed_jobs": records(
            "SELECT job, trade_date, segment, CAST(started_at AS VARCHAR) AS started_at, error "
            "FROM job_run WHERE NOT ok ORDER BY job_run.started_at DESC LIMIT 15"
        ),
        "quality": records(
            "SELECT source, endpoint, trade_date, segment, status, reason, "
            "CAST(created_at AS VARCHAR) AS created_at FROM data_quality "
            "ORDER BY data_quality.created_at DESC LIMIT 20"
        ),
    }


def _open_db(settings: Settings) -> tuple[duckdb.DuckDBPyConnection | None, str | None]:
    if not settings.db_path.exists():
        return None, "missing"
    try:
        return duckdb.connect(str(settings.db_path), read_only=True), None
    except duckdb.Error as exc:
        return None, "locked" if "lock" in str(exc).lower() else f"error: {exc}"


def stale_code(running_stamp: float | None) -> bool:
    """主程序记录的代码时间早于当前代码（旧版主程序不报时间，也按旧代码处理）。"""
    from moneytool.app import code_stamp  # noqa: PLC0415

    return running_stamp is None or code_stamp() > float(running_stamp) + 1


def _http_summary(settings: Settings) -> dict[str, Any]:
    import httpx  # noqa: PLC0415

    host = settings.server.listen
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    headers = {"Authorization": f"Bearer {settings.server.token}"} if settings.server.token else {}
    r = httpx.get(
        f"http://{host}:{settings.server.port}/api/diagnose",
        headers=headers,
        timeout=httpx.Timeout(10, connect=2),
        trust_env=False,
    )
    r.raise_for_status()
    data: dict[str, Any] = r.json()["data"]
    return data


def _log_files(settings: Settings, n: int = 3) -> list[Path]:
    return sorted(settings.logs_dir.glob("moneytool-*.log"))[-n:]


def _format_log_line(line: str) -> str:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line.rstrip()[:400]
    if not isinstance(obj, dict):
        return line.rstrip()[:400]
    ts = str(obj.pop("timestamp", ""))[:19]
    level = str(obj.pop("level", "")).upper()
    event = str(obj.pop("event", ""))
    tb = obj.pop("tb", None)
    rest = " ".join(f"{k}={v}" for k, v in obj.items())
    out = f"{ts} {level} {event} {rest}".strip()[:600]
    if tb:
        tail = [t for t in str(tb).strip().splitlines() if t.strip()][-3:]
        out += "\n    " + "\n    ".join(tail)
    return out


def _is_error(line: str) -> bool:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return any(m in line for m in TEXT_ERROR_MARKERS)
    return isinstance(obj, dict) and str(obj.get("level", "")).lower() in (
        *LOG_ERROR_LEVELS,
        "warning",
    )


def build_report(
    settings: Settings, *, log_lines: int = 60, check_sources: bool = True
) -> tuple[str, bool]:
    """返回 (报告文本, 是否无问题)。"""
    from moneytool.app import now_sh  # noqa: PLC0415
    from moneytool.lock import pid_alive, read_holder  # noqa: PLC0415
    from moneytool.params import list_versions  # noqa: PLC0415

    out: list[str] = []
    flow_source = settings.data.flow_source
    problems: list[str] = []

    def section(title: str) -> None:
        out.extend(["", f"==================== {title} ===================="])

    out.append(f"moneytool 诊断报告  生成于 {now_sh().isoformat(timespec='seconds')}")

    section("环境")
    out.append(f"程序版本: {__version__}")
    out.append(f"Python:   {sys.version.split()[0]} ({sys.executable})")
    out.append(f"系统:     {platform.platform()}")
    out.append(f"数据目录: {settings.data_dir}")
    out.append(
        f"配置文件: {settings.config_path}{'' if settings.config_path.exists() else '（不存在）'}"
    )
    size = settings.db_path.stat().st_size / 1e6 if settings.db_path.exists() else 0
    out.append(f"数据库:   {settings.db_path}（{size:.1f} MB）")
    try:
        versions = [p.version for p in list_versions(settings.params_dir)]
    except Exception as exc:
        versions = []
        problems.append(f"参数版本读取失败：{exc}")
    out.append(f"参数版本: {', '.join(versions) or '无'}")
    if not versions:
        problems.append("没有参数版本：请先运行 python -m moneytool init（或 start.bat）")
    out.append(f"监听:     {settings.server.listen}:{settings.server.port}")
    sys_proxy = system_proxies()
    out.append(f"系统代理: {sys_proxy or '无'}")
    out.append(
        f'数据源:   {apply_network(settings)}（config.toml [network] proxy = "{settings.network.proxy}"）'
    )
    out.append(
        f"资金流:   日频来源 {flow_source}，盘中分段来源 eastmoney（config.toml [data] flow_source）"
    )

    section("进程与锁")
    holder = read_holder(settings.lock_path)
    holder_pid = int(holder["pid"]) if holder.get("pid", "").isdigit() else None
    holder_alive = holder_pid is not None and pid_alive(holder_pid)
    if holder:
        state = (
            "进程仍在运行"
            if holder_alive
            else "进程已退出（上次关闭窗口时任务未结束，留下的记录，无影响）"
        )
        out.append(f"写锁持有者: {holder}，{state}")
    else:
        out.append("写锁持有者: 无")

    section("数据库状态")
    summary: dict[str, Any] | None = None
    web = f"http://127.0.0.1:{settings.server.port}"
    http_error = ""
    try:
        summary = _http_summary(settings)
        out.append(f"主程序正在运行（网页与接口 {web} 正常），通过本机接口读取。")
        if stale_code(summary.get("code_stamp")):
            problems.append(
                "主程序运行的还是旧代码（启动后代码已更新，新的数据源与限速设置未生效）："
                "关闭 moneytool 窗口后重新运行 start.bat"
            )
    except Exception as exc:
        summary = None
        http_error = f"{type(exc).__name__}: {exc}"[:200]
    if summary is None:
        if holder_alive:
            problems.append(
                f"主程序在运行（进程 {holder_pid}）但网页 {web} 10 秒内无响应（{http_error}）："
                "重新运行一次 doctor；仍无响应请关闭 moneytool 窗口后重新 start.bat"
            )
        conn, db_state = _open_db(settings)
        if conn is not None:
            out.append(
                "主程序在运行但本机接口无响应，直接读取数据库。"
                if holder_alive
                else f"主程序未运行（{web} 无响应），直接读取数据库。启动请双击 start.bat。"
            )
            try:
                summary = db_summary(conn)
            finally:
                conn.close()
        elif db_state == "missing":
            problems.append("数据库不存在：请先运行 python -m moneytool init（或 start.bat）")
            out.append("数据库不存在。")
        elif db_state == "locked":
            problems.append(
                "数据库被其他进程占用但本机接口无响应：可能有残留的 moneytool 进程，"
                "关闭所有 moneytool 窗口（或在任务管理器结束 python.exe）后重试"
            )
        else:
            problems.append(f"数据库打开失败：{db_state}")

    if summary is not None:
        c = summary["counts"]
        out.append(
            f"证券 {c['security']} · 板块 {c['sector']}（概念 {c['concept']}）· 日历至 {c['calendar_max']}"
        )
        out.append(
            f"日线 {c['bar_days']} 天 · 资金流 {c['flow_days']} 天 · 已出结果 {c['confirmed_days']} 天"
            f" · 最近结果日 {c['latest_confirmed']}"
        )
        if c["security"] == 0:
            problems.append(
                "证券列表为空：参考数据未同步。请看下方「数据源连通」与「日志错误」中 baostock 相关错误"
            )
        if c["security"] > 0 and c["calendar_max"] < now_sh().date().isoformat():
            problems.append(
                "交易日历未覆盖今天：参考数据未同步，重启程序会自动同步；仍不行请看「数据源连通」"
            )
        if c["sector"] == 0:
            problems.append("板块为空：申万成分未同步，检查 shenwan 数据源")
        if c["confirmed_days"] == 0 and c["security"] > 0:
            problems.append("尚无计算结果：回补未完成时属正常，完成后自动计算；回补进度见下")

        section("回补进度")
        for task, label in (("bars", "日线"), ("flow_daily", "资金流")):
            p = summary["backfill"].get(task, {})
            total = p.get("total", 0)
            done = p.get("done", 0)
            failed = p.get("failed", 0)
            ratio = f"{done / total:.0%}" if total else "—"
            out.append(f"{label}: 完成 {done}/{total}（{ratio}）· 失败 {failed}")
            if total and failed > total * 0.05:
                problems.append(f"{label}回补失败 {failed} 只，超过 5%：多为数据源限流或连不上")

        section("各任务最近一次运行")
        for j in summary["last_jobs"]:
            flag = "成功" if j["ok"] else "失败"
            err = f" · {str(j['error'])[:200]}" if j.get("error") else ""
            out.append(f"{j['job']:<18} {str(j['started_at'])[:19]} {flag}{err}")
        if not summary["last_jobs"]:
            out.append("（尚无任务记录）")

        section("最近失败任务")
        for j in summary["failed_jobs"]:
            out.append(
                f"{str(j['started_at'])[:19]} {j['job']} {j['segment'] or ''} · {str(j['error'])[:300]}"
            )
        if summary["failed_jobs"]:
            problems.append(
                f"有 {len(summary['failed_jobs'])} 条失败任务记录，详见「最近失败任务」"
            )
        else:
            out.append("无")

        section("最近数据质量记录")
        for q in summary["quality"]:
            out.append(
                f"{str(q['created_at'])[:19]} {q['source']}/{q['endpoint']} {q['trade_date']} "
                f"{q['segment'] or ''} [{q['status']}] {q['reason'] or ''}"[:400]
            )
        if not summary["quality"]:
            out.append("无")

    section("数据源连通")
    if check_sources:
        from moneytool.adapters.registry import build_adapters  # noqa: PLC0415
        from moneytool.scheduler.jobs import clock_drift_seconds  # noqa: PLC0415

        probe = settings.model_copy(deep=True)
        for name in ("sina", "eastmoney", "baostock", "shenwan", "csindex"):
            limit = getattr(probe.rate_limit, name)
            limit.backoff_seconds = ()
            limit.timeout_seconds = min(limit.timeout_seconds, 15.0)
        ad = build_adapters(probe)
        for h in ad.health():
            if h.get("ok"):
                out.append(f"{h['source']:<10} 正常 · {h.get('rows', '')} 行 · {h.get('ms')} ms")
            else:
                err = str(h.get("error"))
                out.append(f"{h['source']:<10} 失败 · {err[:300]}")
                if "ProxyError" in err or "proxy" in err.lower():
                    problems.append(
                        f'数据源 {h["source"]} 被代理拦截：把 config.toml 的 [network] proxy 设为 "direct"'
                        "（默认值），或关闭 VPN / 代理软件的系统代理后重启程序"
                    )
                elif h["source"].startswith("eastmoney") and (
                    "RemoteDisconnected" in err or "502" in err
                ):
                    impact = (
                        "日频资金流由新浪提供不受影响，只缺盘中分段近似值"
                        if flow_source == "sina"
                        else "程序会自动暂停并逐次延长间隔；若反复出现，"
                        "调大 [backfill] flow_interval_seconds 或把 [data] flow_source 改为 sina"
                    )
                    problems.append(
                        f"数据源 {h['source']} 断开连接：通常是请求过于频繁、本机 IP 被东财临时封禁"
                        f"（可持续数十分钟到数小时）。{impact}"
                    )
                elif h["source"] == "eastmoney实时":
                    problems.append(
                        "东财实时排名接口（push2.eastmoney.com）不可用：盘中分段与收盘快照会缺失，"
                        f"历史日频回补不受影响。错误：{err[:120]}"
                    )
                else:
                    problems.append(f"数据源 {h['source']} 连不上：{err[:120]}")
        drift = clock_drift_seconds(ad)
        out.append(f"时钟偏差: {'未知' if drift is None else f'{drift:.0f} 秒'}")
        if drift is not None and abs(drift) > settings.schedule.clock_drift_warn_seconds:
            problems.append("本机时钟与数据源偏差过大，分段采集时点可能错位：请同步系统时间")
    else:
        out.append("已跳过（--offline）")

    files = _log_files(settings)
    section("日志错误与警告（最近 3 天，最多 40 条）")
    errors: list[str] = []
    for f in files:
        with f.open(encoding="utf-8", errors="replace") as fh:
            errors.extend(_format_log_line(line) for line in fh if _is_error(line))
    for e in errors[-40:]:
        out.append(e)
    if not errors:
        out.append("无" if files else f"没有日志文件（{settings.logs_dir}）")

    section(f"最新日志末尾 {log_lines} 行")
    if files:
        out.append(f"文件: {files[-1]}")
        with files[-1].open(encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-log_lines:] if log_lines > 0 else []
        out.extend(_format_log_line(line) for line in tail)
    else:
        out.append("无")

    section("结论")
    if problems:
        out.append(f"发现 {len(problems)} 个问题：")
        out.extend(f"{i}. {p}" for i, p in enumerate(problems, 1))
    else:
        out.append("未发现问题。")
    return "\n".join(out) + "\n", not problems
