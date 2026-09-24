---
name: backend-fastapi-duckdb
description: moneytool 后端开发约定。写 Python 包结构、FastAPI 路由、DuckDB 访问、Polars 计算、APScheduler 任务、配置与日志时使用；涵盖单进程启动流程、读写连接分离、口径元信息、错误处理。
paths: ["moneytool/**", "tests/**"]
---

# 后端开发约定

单进程应用：`python -m moneytool` 在一个进程里启动调度器、计算与 FastAPI。没有外部服务。

## 包结构

```text
moneytool/
  __main__.py       CLI 入口：init / run / fetch / recompute / params / doctor
  app.py            组装：读配置 → 打开 DuckDB → 建表迁移 → 启动 scheduler → 启动 uvicorn
  config.py         pydantic-settings，读取 ~/.moneytool/config.toml 与环境变量
  adapters/         见 data-adapter-akshare skill
  compute/
    flow.py         分段差分、日频整理、板块聚合、一字板剔除
    features.py     6.1 派生量
    stages.py       7.1 阶段判定编排（调用 rules/）
    roles.py        8.x 角色、标签、评分编排
    actions.py      名单、买卖点、跟踪闭环
    attribution.py  7.5 归因、7.3.1 主线轮动
    pipeline.py     六步串联，intraday / confirmed 两个入口
  rules/            见 rules-engine skill
  storage/          见 duckdb-schema skill
  api/
    deps.py         只读连接、trade_date 解析
    routers/        market.py sectors.py stocks.py actions.py watchlist.py replay.py outcomes.py glossary.py status.py
    schemas.py      pydantic 响应模型，含 Meta
  scheduler/
    jobs.py         每个任务一个函数
    schedule.py     APScheduler 配置与错过任务补跑
  params/           参数 YAML
  static/           前端产物
```

## 启动流程（`app.py`）

1. 读配置，确定数据目录，建目录。
2. 打开 DuckDB 写连接（全局唯一），跑迁移。
3. 加载当日参数版本。
4. 启动 APScheduler（`BackgroundScheduler`），注册任务；检查最近交易日 confirmed 是否存在，缺则排入补跑。
5. 启动回补任务（若 `init` 后未完成）。
6. 创建 FastAPI，挂 `/api` 路由与 `StaticFiles`，uvicorn 监听配置地址。
7. 若配置允许，打开浏览器。

关闭时：停调度器 → 等待正在跑的任务 → 关闭连接。

## FastAPI 约定

- 路由按需求页面分组，路径 `/api/{resource}`。查询参数统一 `trade_date: date | None`、`segment: str | None`。
- 每个响应模型包含 `meta: Meta`，字段：`trade_date`、`segment`、`status`（`confirmed | intraday | unreconciled | missing`）、`param_version`、`generated_at`。业务字段不再重复这些。
- 依赖 `get_ro_conn()` 提供只读 DuckDB 连接（`read_only=True` 打开同一文件）。路由里不写数据。
- 查询用 SQL 字串放在 `storage/queries/*.sql`，通过 `duckdb.sql(...).pl()` 转 Polars 再转 pydantic。不在路由里拼复杂 SQL。
- 错误：数据缺失返回 200 + `status = missing` 与原因，不返回 404；参数非法返回 422；内部异常统一 500 并记日志，响应不带堆栈。
- 无鉴权，只监听 `127.0.0.1`。若配置 `listen = "0.0.0.0"`，启用 `X-Token` 校验中间件。

## Polars 与计算

- 计算函数签名统一 `fn(df: pl.DataFrame, params: Params) -> pl.DataFrame`，不接连接、不读时钟。
- 从 DuckDB 读：`conn.sql(query).pl()`；写回：`conn.register("tmp", df)` 后 `INSERT ... SELECT`。
- 窗口计算用 `over("code")` / `rolling_*`，不写 Python 循环。
- 日期列类型 `pl.Date`，代码列 `pl.Utf8`，金额列 `pl.Float64`（单位：元），比例列 `pl.Float64`（0–1，展示层再乘 100）。

## 调度

- 任务函数无参数、幂等：重复跑同一交易日同一 segment 结果一致，写入用 `INSERT OR REPLACE`。
- `misfire_grace_time`：分段任务 5 分钟（错过则放弃并标「未采集」），收盘任务 6 小时。
- 任务内部捕获异常写 `data_quality`，不让调度器线程崩。
- 任务运行在调度器线程池，DuckDB 写连接通过 `threading.Lock` 串行使用。

## 配置

`~/.moneytool/config.toml`：

```toml
[server]
listen = "127.0.0.1"
port = 8000
open_browser = true

[data]
dir = "~/.moneytool"
backfill_years_flow = 2
backfill_years_bars = 5

[rate_limit.eastmoney]
min_interval_seconds = 2.0
per_stock_interval_seconds = 5.0

[calendar]
extra_closed = []

[notify]
desktop = true
webhook_url = ""
```

## 日志

- `structlog` JSON 行，写 `~/.moneytool/logs/moneytool-YYYY-MM-DD.log`，按日滚动保留 30 天。
- 每个任务开始 / 结束记一行，含行数与耗时。
- 不在日志里打印完整 DataFrame。

## 检查清单

新路由有响应模型且含 `meta`；新计算函数是纯函数且有单测；新任务幂等且有 misfire 策略；没有在 API 里写库；没有魔法数字。
