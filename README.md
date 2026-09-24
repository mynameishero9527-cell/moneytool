# moneytool

A 股板块资金周期分析工具：本地单进程（Python + DuckDB + FastAPI + Vue），只用免费数据源，按「冰点 → 启动 → 扩散加速 → 高潮拥挤 → 分歧背离 → 退潮」六阶段给板块定位，并把每个判定的规则、当日值与阈值一起展示。

需求见 `docs/prd-a-share-sector-capital.md`，架构见 `docs/tech-architecture.md`，开发约定见 `AGENTS.md` 与 `.cursor/skills/`。

## 安装与运行

要求 Python 3.11+；构建前端另需 Node.js 22+（仓库已带构建产物 `moneytool/static/`，不改前端可不装）。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m moneytool init          # 建 ~/.moneytool：配置、参数版本、数据库
python -m moneytool run           # 调度 + 回补 + Web，默认 http://127.0.0.1:8000
```

数据目录可用 `--data-dir` 或环境变量 `MONEYTOOL_DATA__DIR` 覆盖。首次运行会在后台回补 5 年日线与 2 年日频资金流，进度见「数据状态」页。

常用命令：

| 命令 | 作用 |
| --- | --- |
| `python -m moneytool status` | 最近确认日、当日分段、回补进度、质量记录 |
| `python -m moneytool doctor` | 数据源连通、时钟偏差、锁、参数版本自检 |
| `python -m moneytool fetch --what reference\|segment:1030_1130\|close\|nightly` | 手动触发一次采集 |
| `python -m moneytool recompute --date 2026-05-08` 或 `--date 2026-01-01:2026-05-08` | 按当日生效参数重算并存档 |
| `python -m moneytool explain --sector sw:801010 [--date ...]` | 打印阶段判定证据 |
| `python -m moneytool params list\|new --effective-from ...\|explain` | 参数版本（只增不改） |
| `python -m moneytool backfill [--once]` | 前台回补 |
| `python -m moneytool export --out ./out [--date ...]` | 导出某日结果 CSV |

## 开发

```bash
make check                        # ruff + ruff format --check + mypy + pytest
make frontend                     # cd frontend && npm ci && npm run build → moneytool/static/
make dev                          # python -m moneytool run --no-browser
```

前端开发：`cd frontend && npm install && npm run dev`（Vite 代理 `/api` 到 `127.0.0.1:8000`）。

## 结构

```text
moneytool/
  adapters/    数据源适配器（东财资金流、Baostock 日线日历、申万/中证成分）+ 契约校验、限速、原始缓存
  ingest/      参考数据同步、日线回补、分段资金流采集与收盘确认
  compute/     派生量、板块聚合、市场层、六阶段判定、计算链编排
  rules/       纯函数规则（阶段、风控），输出 Evidence
  params/      参数版本 v*.yaml（阈值只在这里）
  storage/     DuckDB 连接、迁移、写入助手
  scheduler/   APScheduler 时间表与任务壳（持锁、记 job_run、异常落 data_quality）
  api/         FastAPI：每个响应带 meta（trade_date / segment / status / param_version）
  static/      前端构建产物
frontend/      Vue 3 + TypeScript + Vite + ECharts + TanStack Query
tests/         pytest（内存 DuckDB + 合成行情）
```

分析工具，不构成投资建议；资金流为行情商估算；历史统计不代表未来结果。
