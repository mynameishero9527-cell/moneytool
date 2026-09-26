"""命令行入口 `python -m moneytool`（架构 4.1.3 / 9 节）。

子命令：init / run / fetch / recompute / params / doctor / explain / backfill / status / export。
主进程运行时，`recompute` 与 `fetch` 优先通过本机 `/api/admin/*` 排队，避免双写。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer

from moneytool import __version__

app = typer.Typer(
    add_completion=False,
    help="A 股板块资金周期分析工具（本地单进程）。",
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
params_app = typer.Typer(help="参数版本：list / new / explain", no_args_is_help=True)
app.add_typer(params_app, name="params")

DataDirOpt = Annotated[
    Path | None,
    typer.Option("--data-dir", help="数据目录（默认项目目录下 data\\，或 MONEYTOOL_DATA__DIR）"),
]


def _echo_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _parse_date(s: str | None) -> dt.date | None:
    return None if s is None else dt.date.fromisoformat(s)


def _params_dir(data_dir: Path | None) -> Path:
    from moneytool.config import load_settings  # noqa: PLC0415
    from moneytool.params import install_builtin_params  # noqa: PLC0415

    settings = load_settings(data_dir)
    install_builtin_params(settings.params_dir)
    return settings.params_dir


def _local_api(settings: Any) -> str | None:
    """主进程在跑则返回其地址；否则 None。"""
    import httpx  # noqa: PLC0415

    url = f"http://127.0.0.1:{settings.server.port}"
    try:
        r = httpx.get(f"{url}/api/health", timeout=1.5)
    except httpx.HTTPError:
        return None
    return url if r.status_code == 200 else None


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", help="显示版本后退出", is_eager=True)
    ] = False,
) -> None:
    if version:
        typer.echo(f"moneytool {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@app.command()
def init(data_dir: DataDirOpt = None) -> None:
    """建数据目录、默认配置、参数版本与数据库结构。可重复执行。"""
    from moneytool.app import LegacyDataBusyError, init_data_dir  # noqa: PLC0415

    try:
        settings = init_data_dir(data_dir)
    except LegacyDataBusyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"数据目录: {settings.data_dir}")
    typer.echo(f"配置文件: {settings.config_path}")
    typer.echo(f"数据库:   {settings.db_path}")
    typer.echo("下一步: python -m moneytool run")


@app.command()
def run(
    data_dir: DataDirOpt = None,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="不自动打开浏览器")] = False,
    no_scheduler: Annotated[
        bool, typer.Option("--no-scheduler", help="只起 Web，不跑调度与回补（调试用）")
    ] = False,
    host: Annotated[str | None, typer.Option(help="覆盖监听地址")] = None,
    port: Annotated[int | None, typer.Option(help="覆盖端口")] = None,
) -> None:
    """启动主进程：调度 + 回补线程 + Web。"""
    import uvicorn  # noqa: PLC0415

    from moneytool.api.server import create_app  # noqa: PLC0415
    from moneytool.app import build_context  # noqa: PLC0415
    from moneytool.scheduler.jobs import JobRunner  # noqa: PLC0415
    from moneytool.scheduler.schedule import build_scheduler, start_backfill_thread  # noqa: PLC0415

    ctx = build_context(data_dir)
    listen = host or ctx.settings.server.listen
    listen_port = port or ctx.settings.server.port
    token = ctx.settings.server.token
    if listen not in ("127.0.0.1", "localhost", "::1") and not token:
        typer.echo("监听非回环地址必须在 config.toml 的 [server] 设置 token", err=True)
        raise typer.Exit(code=2)

    runner = JobRunner(ctx)
    scheduler = None
    if not no_scheduler:
        scheduler = build_scheduler(runner)
        scheduler.start()
        start_backfill_thread(runner)

    fastapi_app = create_app(ctx.db, ctx=ctx, runner=runner, token=token)
    url = f"http://{listen}:{listen_port}"
    typer.echo(f"moneytool {__version__} 运行中: {url}  (Ctrl+C 退出)")
    if not no_browser and ctx.settings.server.open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(fastapi_app, host=listen, port=listen_port, log_level="warning")
    finally:
        runner.stop_event.set()
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        ctx.close()


@app.command()
def status(data_dir: DataDirOpt = None) -> None:
    """数据状态：最近 confirmed 日、当日分段、回补进度、质量记录。"""
    from moneytool.api.server import create_app  # noqa: PLC0415
    from moneytool.app import build_context  # noqa: PLC0415

    ctx = build_context(data_dir, log_to_file=False)
    try:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        with TestClient(create_app(ctx.db, ctx=ctx, static_dir=None)) as client:
            _echo_json(client.get("/api/status").json()["data"])
    finally:
        ctx.close()


@app.command()
def doctor(
    data_dir: DataDirOpt = None,
    out: Annotated[Path | None, typer.Option("--out", help="同时把报告写入该文件")] = None,
    log_lines: Annotated[int, typer.Option("--log-lines", help="附带最新日志末尾行数")] = 60,
    offline: Annotated[bool, typer.Option("--offline", help="跳过数据源连通检查")] = False,
) -> None:
    """诊断：环境、锁、数据库与回补进度、失败任务、质量记录、数据源连通、日志错误。主程序运行中也可用。"""
    from moneytool.config import load_settings  # noqa: PLC0415
    from moneytool.diagnose import build_report  # noqa: PLC0415
    from moneytool.logging import setup_logging  # noqa: PLC0415

    setup_logging(None, level="WARNING")
    settings = load_settings(data_dir)
    text, ok = build_report(settings, log_lines=log_lines, check_sources=not offline)
    typer.echo(text)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig：Windows 记事本可正确识别中文
        out.write_text(text, encoding="utf-8-sig")
        typer.echo(f"报告已保存: {out.resolve()}")
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def fetch(
    data_dir: DataDirOpt = None,
    what: Annotated[
        str, typer.Option(help="reference / segment:<seg> / close / nightly")
    ] = "reference",
) -> None:
    """手动触发一次采集任务（主进程未运行时）。"""
    from moneytool.app import build_context  # noqa: PLC0415
    from moneytool.scheduler.jobs import JobRunner  # noqa: PLC0415

    ctx = build_context(data_dir)
    try:
        runner = JobRunner(ctx)
        if what == "reference":
            runner.job_reference_sync()
        elif what.startswith("segment:"):
            res = runner.job_segment(what.split(":", 1)[1])
            _echo_json(res.__dict__ if res else {"result": "skipped"})
        elif what == "close":
            res = runner.job_close_confirm(force=True)
            _echo_json(res.__dict__ if res else {"result": "skipped"})
        elif what == "nightly":
            runner.job_nightly()
        else:
            typer.echo(f"未知任务 {what}", err=True)
            raise typer.Exit(code=2)
    finally:
        ctx.close()


@app.command()
def recompute(
    date: Annotated[str, typer.Option("--date", help="YYYY-MM-DD；或 start:end 区间")],
    data_dir: DataDirOpt = None,
    segment: Annotated[str | None, typer.Option(help="盘中分段；缺省收盘全量")] = None,
) -> None:
    """按当日生效参数版本重算并存档。主进程运行时改为向其排队。"""
    from moneytool.app import build_context  # noqa: PLC0415

    if ":" in date:
        a, b = date.split(":", 1)
        start, end = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    else:
        start = end = dt.date.fromisoformat(date)

    ctx = build_context(data_dir, log_to_file=False)
    try:
        local = _local_api(ctx.settings)
        with ctx.db.read() as conn:
            days = [
                r[0]
                for r in conn.execute(
                    "SELECT trade_date FROM trade_calendar WHERE is_open AND trade_date BETWEEN ? AND ? ORDER BY 1",
                    [start, end],
                ).fetchall()
            ]
        if not days:
            days = [start] if start == end else []
        if local:
            import httpx  # noqa: PLC0415

            for d in days:
                r = httpx.post(
                    f"{local}/api/admin/recompute",
                    json={"trade_date": d.isoformat(), "segment": segment},
                    headers={"Authorization": f"Bearer {ctx.settings.server.token}"},
                    timeout=600,
                )
                typer.echo(f"{d} -> {r.status_code} {r.text[:200]}")
            return
        from moneytool.compute.pipeline import run as run_pipeline  # noqa: PLC0415
        from moneytool.lock import write_lock  # noqa: PLC0415

        with write_lock(ctx.settings.lock_path, owner="recompute"), ctx.db.write() as conn:
            for d in days:
                res = run_pipeline(conn, ctx.params_for(d), d, segment=segment)
                typer.echo(
                    f"{d} {res.data_status.value} stocks={res.stock_rows} sectors={res.sector_rows} "
                    f"stages={res.stage_rows} {'; '.join(res.notes)}"
                )
    finally:
        ctx.close()


@app.command()
def backfill(
    data_dir: DataDirOpt = None,
    batch: Annotated[int | None, typer.Option(help="每批标的数（默认 [backfill] batch）")] = None,
    once: Annotated[bool, typer.Option("--once", help="只跑一批")] = False,
    reset_flow: Annotated[
        bool,
        typer.Option(
            "--reset-flow", help="清空资金流历史与进度后重新回补（改了 [data] flow_source 后用）"
        ),
    ] = False,
) -> None:
    """前台回补日线与日频资金流（主进程未运行时）。"""
    from moneytool.app import build_context  # noqa: PLC0415
    from moneytool.ingest.flow import reset_flow_history  # noqa: PLC0415
    from moneytool.scheduler.jobs import JobRunner  # noqa: PLC0415

    ctx = build_context(data_dir)
    try:
        if reset_flow:
            with ctx.db.write() as conn:
                n = reset_flow_history(conn)
            typer.echo(f"已清空 {n} 行日频资金流，按 {ctx.settings.data.flow_source} 重新回补")
        runner = JobRunner(ctx)
        while True:
            b, f = runner.job_backfill_batch(batch)
            typer.echo(f"bars={b} flow={f}")
            if once or (b == 0 and f == 0):
                break
    except KeyboardInterrupt:
        typer.echo("已中断，进度已保存")
    finally:
        ctx.close()


@app.command()
def explain(
    sector: Annotated[str | None, typer.Option(help="板块 sector_id，如 sw:801010")] = None,
    stock: Annotated[str | None, typer.Option(help="个股代码，如 600000.SH")] = None,
    date: Annotated[str | None, typer.Option("--date")] = None,
    data_dir: DataDirOpt = None,
) -> None:
    """打印某日某板块 / 个股的判定证据（规则、指标值、阈值、是否命中）。"""
    from moneytool.api.deps import resolve_trade_date  # noqa: PLC0415
    from moneytool.app import build_context  # noqa: PLC0415

    if not sector and not stock:
        typer.echo("需要 --sector 或 --stock", err=True)
        raise typer.Exit(code=2)
    ctx = build_context(data_dir, log_to_file=False)
    try:
        with ctx.db.read() as conn:
            day = resolve_trade_date(conn, _parse_date(date))
            if day is None:
                typer.echo("尚无任何计算结果")
                raise typer.Exit(code=1)
            if sector:
                row = conn.execute(
                    "SELECT stage, half, candidate, days_in_stage, entered_from, suppressed, evidence, param_version "
                    "FROM sector_stage_confirmed WHERE sector_id = ? AND trade_date = ? ORDER BY param_version DESC LIMIT 1",
                    [sector, day],
                ).fetchone()
                if row is None:
                    typer.echo(f"{day} {sector} 无阶段结果")
                    raise typer.Exit(code=1)
                typer.echo(
                    f"{day} {sector} 阶段={row[0]} half={row[1]} 候选={row[2]} 停留={row[3]} 来自={row[4]} 参数={row[7]}"
                )
                _print_evidence(json.loads(row[6]))
            if stock:
                row = conn.execute(
                    "SELECT features FROM feature_daily WHERE subject_type = 'stock' AND subject_id = ? AND trade_date = ? AND segment = 'close' "
                    "ORDER BY param_version DESC LIMIT 1",
                    [stock, day],
                ).fetchone()
                if row is None:
                    typer.echo(f"{day} {stock} 无派生量")
                    raise typer.Exit(code=1)
                _echo_json(json.loads(row[0]))
    finally:
        ctx.close()


def _print_evidence(ev: dict[str, Any]) -> None:
    for rule, items in ev.items():
        if isinstance(items, list):
            for e in items:
                if isinstance(e, dict) and "metric" in e:
                    mark = "✓" if e.get("hit") else "·"
                    typer.echo(
                        f"  {mark} {rule:8s} {e.get('metric'):26s} {e.get('value')!s:>14} {e.get('op', '')} {e.get('threshold')!s:<10} {e.get('note') or ''}"
                    )
        else:
            typer.echo(f"  {rule}: {items}")


@params_app.command("list")
def params_list(data_dir: DataDirOpt = None) -> None:
    from moneytool.params import list_versions  # noqa: PLC0415

    for p in list_versions(_params_dir(data_dir)):
        typer.echo(f"{p.version}  生效 {p.effective_from}  {p.note}")


@params_app.command("new")
def params_new(
    effective_from: Annotated[str, typer.Option("--effective-from", help="YYYY-MM-DD")],
    note: Annotated[str, typer.Option(help="修改说明")] = "",
    data_dir: DataDirOpt = None,
) -> None:
    """复制最新版本为新版本文件（旧版本永不修改）。"""
    from moneytool.params import new_version  # noqa: PLC0415

    path = new_version(_params_dir(data_dir), dt.date.fromisoformat(effective_from), note)
    typer.echo(f"已创建 {path}，编辑后用 recompute --date 区间 重算")


@params_app.command("explain")
def params_explain(
    date: Annotated[str | None, typer.Option("--date")] = None,
    data_dir: DataDirOpt = None,
) -> None:
    """打印某日生效的参数版本全部阈值。"""
    from moneytool.params import latest_params, params_for_date  # noqa: PLC0415

    pdir = _params_dir(data_dir)
    p = params_for_date(pdir, dt.date.fromisoformat(date)) if date else latest_params(pdir)
    _echo_json(p.model_dump(mode="json"))


@app.command()
def export(
    out: Annotated[Path, typer.Option(help="导出目录")],
    date: Annotated[
        str | None, typer.Option("--date", help="YYYY-MM-DD，缺省最近 confirmed 日")
    ] = None,
    data_dir: DataDirOpt = None,
) -> None:
    """导出某日阶段、市场、派生量为 CSV（需求 9.7）。"""
    from moneytool.api.deps import resolve_trade_date  # noqa: PLC0415
    from moneytool.app import build_context  # noqa: PLC0415

    ctx = build_context(data_dir, log_to_file=False)
    try:
        out.mkdir(parents=True, exist_ok=True)
        with ctx.db.read() as conn:
            day = resolve_trade_date(conn, _parse_date(date))
            if day is None:
                typer.echo("尚无任何计算结果")
                raise typer.Exit(code=1)
            for table in (
                "sector_stage_confirmed",
                "market_daily",
                "feature_daily",
                "data_quality",
            ):
                df = conn.execute(f"SELECT * FROM {table} WHERE trade_date = ?", [day]).pl()
                df.write_csv(out / f"{table}_{day.isoformat()}.csv")
                typer.echo(f"{table}: {df.height} 行")
    finally:
        ctx.close()


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
