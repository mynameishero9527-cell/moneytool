---
name: release-packaging
description: 构建、打包、发布 moneytool 或排查 `python -m moneytool` 启动问题时使用。涵盖前端构建拷贝、pyproject 与 wheel、版本号、CLI 入口、首次运行流程、跨平台注意事项、发布检查。
---

# 构建与发布

用户只需要 Python：`pip install moneytool && moneytool run`。前端产物在打包前构建好放进包里。

## 构建前端

```bash
python scripts/build_frontend.py
```

脚本做：`cd frontend && npm ci && npm run build` → 清空 `moneytool/static/` → 拷贝 `dist/` → 写 `moneytool/static/BUILD_INFO`（git sha、时间、node 版本）。`moneytool/static/` 提交进仓库；CI 重新构建并 `git diff --exit-code moneytool/static` 保证一致。

## pyproject

- 构建后端 `hatchling`；`[tool.hatch.build.targets.wheel]` 包含 `moneytool/static/**` 与 `moneytool/params/*.yaml`。
- `[project.scripts] moneytool = "moneytool.__main__:main"`；同时支持 `python -m moneytool`。
- 依赖钉版本范围：`akshare==x.y.z`（精确，见 data-adapter skill）、`polars>=1.0,<2`、`duckdb>=1.0,<2`、`fastapi`、`uvicorn[standard]`、`apscheduler>=3.10,<4`、`pydantic>=2`、`pydantic-settings`、`structlog`、`httpx`、`baostock`、`pyyaml`、`plyer`（可选 extra `desktop`）。
- Python `>=3.11`。
- 版本号在 `moneytool/__init__.py` 的 `__version__`，`hatch` 动态读取。语义化版本；参数版本与软件版本独立。

## CLI

| 命令 | 作用 |
| --- | --- |
| `moneytool init [--data-dir]` | 建目录、写默认 `config.toml` 与 `params/v1.yaml`、建库、启动回补 |
| `moneytool run [--no-browser] [--port]` | 启动调度 + Web，默认打开浏览器 |
| `moneytool fetch --date --source` | 手工拉取 |
| `moneytool recompute --date [--all-segments]` | 用当日参数重算并覆盖快照（先备份旧快照到 `backup/`） |
| `moneytool params new --note` | 新建参数版本 |
| `moneytool explain sector|stock ...` | 打印证据 |
| `moneytool doctor` | 数据源与契约检查 |
| `moneytool backfill status|resume` | 回补进度与续跑 |

`run` 在数据目录不存在时自动执行 `init`。

## 首次运行

1. `init` 建 `~/.moneytool/{moneytool.duckdb, config.toml, params/, raw/, logs/, backup/}`。
2. 启动回补任务：日线 5 年（Baostock）→ 板块资金流 5 年 → 自选与候选个股资金流 2 年 → 全市场个股资金流 2 年。进度写 `backfill_progress` 表。
3. Web 立即可用，「数据状态」页显示进度；依赖长历史的功能显示「历史不足」。
4. 回补可中断，`run` 时自动续跑。

## 跨平台

- 路径用 `pathlib`，数据目录默认 `Path.home() / ".moneytool"`，Windows 下即 `C:\Users\<u>\.moneytool`。
- 打开浏览器用 `webbrowser.open`。
- 桌面通知用 `plyer`，失败静默降级为不通知并记日志。
- 时区固定 `Asia/Shanghai`，不用系统时区。
- Windows 下 uvicorn 用默认 loop；不依赖 `uvloop`。

## 发布检查

1. `make check` 全绿；`pytest -m golden` 通过。
2. `python scripts/build_frontend.py` 后 `git status` 干净。
3. `python -m build` 生成 wheel；在干净虚拟环境 `pip install dist/*.whl`。
4. `moneytool init --data-dir /tmp/mt && moneytool doctor`。
5. `moneytool run --no-browser --port 8123`，`curl localhost:8123/api/status` 返回 200，`/` 返回前端。
6. 更新 `CHANGELOG.md`，打 tag `vX.Y.Z`。
7. 若 AkShare 版本变更，发布说明里写明并附 doctor 输出。

## 常见启动问题

| 现象 | 处理 |
| --- | --- |
| `/` 404 | `moneytool/static/index.html` 缺失，重跑构建脚本 |
| DuckDB 锁 | 已有进程在跑；`moneytool run` 检测 pid 文件并提示 |
| 东财全部失败 | 跑 `doctor`；看是否 captcha；浏览器过一次验证 |
| 端口占用 | `--port` 换端口 |
| 首页全是「历史不足」 | 回补未完成，看「数据状态」 |
