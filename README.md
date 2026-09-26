# moneytool

A 股板块资金周期分析工具：本地单进程（Python + DuckDB + FastAPI + Vue），只用免费数据源，按「冰点 → 启动 → 扩散加速 → 高潮拥挤 → 分歧背离 → 退潮」六阶段给板块定位，并把每个判定的规则、当日值与阈值一起展示。

需求见 `docs/prd-a-share-sector-capital.md`，架构见 `docs/tech-architecture.md`，开发约定见 `AGENTS.md` 与 `.cursor/skills/`。

## 安装与运行

要求 Python 3.11+；构建前端另需 Node.js 22+（仓库已带构建产物 `moneytool/static/`，不改前端可不装）。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m moneytool init          # 建项目目录下 data/：配置、参数版本、数据库
python -m moneytool run           # 调度 + 回补 + Web，默认 http://127.0.0.1:8000
```

Windows 可直接双击仓库根目录的 `start.bat`：首次运行自动创建虚拟环境、安装依赖、初始化数据目录，然后启动；额外参数原样传给 `run`（如 `start.bat --port 8765`）。`update.bat` 先拉取 `develop` 最新代码再启动，依赖有变化时自动重装。`doctor.bat` 做一次诊断（主程序运行中也可用），报告存为 `reports\doctor-latest.txt` 并用记事本打开，遇到问题把它发给开发者。

手动步骤（PowerShell）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1       # 提示禁止运行脚本时先执行：Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e ".[dev]"

python -m moneytool init           # 数据目录为项目目录下 data\
python -m moneytool run
```

Windows 没有 `make`，`make check` 对应的命令为 `ruff check . ; ruff format --check . ; mypy moneytool ; pytest -q -m "not network"`，重新构建前端用 `python scripts/build_frontend.py`。

数据目录默认在项目目录下 `data/`（配置、数据库、日志、原始缓存、备份都在这里，已在 `.gitignore` 中），可用 `--data-dir` 或环境变量 `MONEYTOOL_DATA__DIR` 改到别处。旧版默认目录 `~/.moneytool` 里有数据且新目录还没有数据库时，`init` / `run` 会把它整体搬过来并删除旧目录；旧程序仍在运行时会提示先关闭。首次运行会先同步证券列表、交易日历与申万 / 中证成分，再在后台回补 5 年日线与日频资金流，进度见「数据状态」页。日线（Baostock）与日频资金流（默认新浪财经）两路各自循环、按各自数据源的节奏拉取，互不等待（一路变慢或暂停不拖慢另一路），网络请求不占写库锁；新浪默认 2 线程、每秒约一只，2 年资金流全市场约 1.5 小时（见 `[backfill]`）；回补期间页面暂无结果。回补完成后自动补算最近 60 个交易日（`[schedule] catchup_days`），之后每个交易日收盘后自动更新。

常用命令：

| 命令 | 作用 |
| --- | --- |
| `python -m moneytool status` | 最近确认日、当日分段、回补进度、质量记录 |
| `python -m moneytool doctor [--out 报告.txt] [--offline] [--log-lines 200]` | 诊断报告：环境、锁、数据库与回补进度、失败任务、质量记录、数据源连通、日志错误摘要与结论；主程序运行中也可用 |
| `python -m moneytool fetch --what reference\|segment:1030_1130\|close\|nightly` | 手动触发一次采集 |
| `python -m moneytool recompute --date 2026-05-08` 或 `--date 2026-01-01:2026-05-08` | 按当日生效参数重算并存档 |
| `python -m moneytool explain --sector sw:801010 [--date ...]` | 打印阶段判定证据 |
| `python -m moneytool params list\|new --effective-from ...\|explain` | 参数版本（只增不改） |
| `python -m moneytool backfill [--once]` | 前台回补 |
| `python -m moneytool export --out ./out [--date ...]` | 导出某日结果 CSV |

## 页面

| 页面 | 内容 |
| --- | --- |
| 总览 / 看板 | 市场宽度与主线、情绪压力、风控开关；六阶段看板 |
| 板块详情 | 阶段与判定证据、模板提示、资金归因、板块风险 / 情绪指数、成分角色（核心 / 跟随 / 规避） |
| 个股 | 身份标签、排除项（控盘 / 历史暴涨暴跌 / 不可交易）、结构分、各板块角色、今日动作、风险 / 散户压力指数、持有结构评估、跟踪记录、标记 |
| 行动 | 买入关注、低位企稳、买卖点、卖出关注、持有观察、买入关注失效，按行业 / 概念分组；跟踪中（至今收益、相对全 A 等权与板块的超额） |
| 自选 | 板块与个股自选，个股带持有结构评估 |
| 回看 | 标签事后统计（按窗口 × 风控状态，样本不足标注）；收盘 / 盘前复盘简报 |

指数只做描述，不参与判定；只有市场风控开关会暂停新增买入关注与低位企稳。

## 定时任务

交易日按时间表采集分段资金流并重算；收盘确认后生成收盘简报（数据目录 `briefs/`）并推送提醒，08:30 生成盘前简报；夜间补算到期标签的事后统计、清理过期原始缓存与盘中中间结果、轮换数据库备份。

相关配置（`data/config.toml`）：

```toml
[data]
intraday_retention_days = 30   # 盘中分段结果保留天数，收盘确认结果永久保留
backup_keep = 5                # 数据库备份保留份数

[notify]
webhook_url = ""               # 留空则只写日志；免打扰时段内的提醒并入次日盘前简报一起推送
quiet_start = "20:30"
quiet_end = "08:30"

[network]
proxy = "direct"               # direct：数据源直连，忽略 VPN / 代理软件设置的系统代理；system：沿用系统代理；或填代理地址如 "http://127.0.0.1:7890"

[data]
flow_source = "sina"           # 日频资金流来源：sina（默认）或 eastmoney
backfill_years_flow = 2        # 资金流回补年数；新浪最多约 8 年（2018 年起）

[backfill]
batch = 100                    # 每批标的数，每批写库一次
# flow_workers = 2             # 资金流并发线程数；不填按来源默认：新浪 2，东财 1
# flow_interval_seconds = 1.0  # 最小请求间隔；不填按来源默认：新浪 1 秒，东财 3 秒（东财过快会封 IP）
flow_max_interval_seconds = 30.0
```

### 资金流数据源

| 用途 | 来源 | 说明 |
| --- | --- | --- |
| 日频资金流（历史回补 + 每日正式值） | 新浪财经（默认） | 个股历史约 8 年；特大 / 大 / 中 / 小单净额，主力 = 特大单 + 大单 |
| 盘中分段、收盘快照 | 东方财富 | 实时全市场排名；只作近似值，收盘后以日频正式值为准 |

新浪来源下，回补线程每天 18 点后给每只股票增量拉取最新几天（每只每 6 小时最多试一次），某天的结果若当时用的是东财收盘快照，拿到新浪正式值后会自动重算。东财被封 IP 时只缺盘中分段，日频结果不受影响。

两家大小单的划分与统计方法不同（东财各档净额合计约为零，新浪按主动买卖统计、合计不为零），同一段历史不能混用。改 `flow_source` 后，关掉程序运行 `python -m moneytool backfill --reset-flow` 清空资金流历史并按新来源重补，受影响交易日的结果会在数据补齐后重算。

东财连接连续 2 次被直接断开（封 IP 的典型表现）时，进程内所有东财请求熔断 15 分钟、不再触网，再次熔断时长翻倍到 2 小时，成功一次复位。资金流请求失败时间隔自动翻倍（最多到上限），成功后逐步恢复；连续 3 只个股失败（多为代理拦截或被东财封 IP，日志表现为 `RemoteDisconnected`）时，资金流回补暂停 30 分钟再试，再失败则暂停时长逐次翻倍到 4 小时，期间不发任何请求；回补的个股请求失败不重试，留给下一批；日线回补不受影响。发往东方财富的请求会自动补上浏览器 Referer 请求头。Baostock 查询带超时（`[rate_limit.baostock] timeout_seconds`），连接卡住或被断开时自动重新登录，不会让回补线程一直挂住。其余经 AkShare 发出、未指定超时的请求一律补上默认超时（连接 10 秒 / 读取 30 秒），不会卡住启动时的参考数据同步。`doctor.bat` 发现主程序运行的还是修改前的旧代码时会提示重启。`doctor.bat` 的「数据源连通」一节会指出被代理拦截的数据源。

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
  adapters/    数据源适配器（新浪日频资金流、东财实时资金流、Baostock 日线日历、申万/中证成分）+ 契约校验、限速、原始缓存
  ingest/      参考数据同步、日线回补、分段资金流采集与收盘确认
  compute/     派生量、板块聚合、市场层、六阶段判定、角色与结构分、指数、名单、提示、事后统计、计算链编排
  report.py    收盘 / 盘前简报
  notify.py    提醒汇总、去重、免打扰
  rules/       纯函数规则（阶段、风控、个股角色 / 名单 / 买卖点），输出 Evidence
  params/      参数版本 v*.yaml（阈值只在这里）
  storage/     DuckDB 连接、迁移、写入助手
  scheduler/   APScheduler 时间表与任务壳（持锁、记 job_run、异常落 data_quality）
  api/         FastAPI：每个响应带 meta（trade_date / segment / status / param_version）
  static/      前端构建产物
frontend/      Vue 3 + TypeScript + Vite + ECharts + TanStack Query
tests/         pytest（内存 DuckDB + 合成行情）
```

分析工具，不构成投资建议；资金流为行情商估算；历史统计不代表未来结果。
