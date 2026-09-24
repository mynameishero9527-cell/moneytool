# moneytool 开发规范（始终生效）

A 股板块资金周期分析工具。需求在 `docs/prd-a-share-sector-capital.md`，架构在 `docs/tech-architecture.md`。改代码前先对照这两份文档；行为与文档冲突时，以文档为准并在 PR 里说明，不要静默改口径。

## 项目形态

- 单进程本地应用：`python -m moneytool` 启动调度、计算、FastAPI 与静态前端。不引入 PostgreSQL、Redis、消息队列、Node 运行时依赖。
- 后端 Python 3.11+，Polars + DuckDB + FastAPI + APScheduler。前端 Vue 3 + TypeScript + Vite + ECharts，构建产物拷入 `moneytool/static/`。
- 资金流数据只用东方财富一个口径。不把其他来源的资金流混入计算或页面。

## 目录

```text
moneytool/          Python 包
  adapters/         数据源适配器（每个源一个模块，带契约）
  compute/          资金流整理 → 指标 → 阶段 → 角色 → 名单 → 存档
  rules/            规则函数，每条规则一个纯函数，输出证据
  storage/          DuckDB 连接、建表、迁移
  api/              FastAPI 路由，按需求页面分组
  scheduler/        APScheduler 任务定义
  static/           前端构建产物（生成文件，不手改）
  params/           参数版本 YAML（只增不改）
frontend/           Vue 源码
tests/              pytest；tests/acceptance/ 对应需求 14 节
docs/               需求与架构
scripts/            构建、回补、诊断脚本
```

## 硬性约定

- 规则是纯函数：输入指标行与参数，输出 `RuleResult(hit, evidence)`。不读全局状态，不读时钟。
- 所有阈值来自 `params/*.yaml`，代码里不出现魔法数字。新阈值先加进 YAML 再用。
- 参数版本只新增文件，不修改已发布版本。
- 结果表按 `trade_date` 快照；回看只读快照，不重算。
- 盘中结果写 `*_intraday`，收盘写 `*_confirmed`，两类表不互相覆盖。
- 数据缺失就标 `data_quality`，不用昨日值或估算值填补。
- DuckDB 写连接只归调度线程；API 用只读连接。
- 每个对外数字带口径元信息（估算 / 已确认 / 盘中 / 未经对账 / 降级、参数版本）。
- 页面文案与提示模板遵守需求 12 节：不出现「稳赚」「必涨」「清仓」「满仓」「最佳」「杀散户」「套牢」「值得」「建议持有」「安全」「危险」。
- 指数（市场情绪压力、板块情绪、板块风险、个股风险、散户承接压力）只描述不决策：规则引擎只读 `risk_gate` 一个布尔量，其余指数不进入任何名单规则。
- 分析提示由 `hints/templates/*.yaml` 模板拼装，不接生成式模型；每条提示必须带可点击的证据链接。
- 写库任务（调度、`recompute`、`backfill`）持文件锁 `~/.moneytool/.lock`；命令行在主进程运行时改走 `/api/admin/*`。

## 口径与文档同步

- 口径的权威来源是 `params/*.yaml`（阈值）与 `.cursor/skills/capital-flow-indicators`（公式与列名）。需求文档与架构文档只引用章节号与列名，不复制公式与数字。
- 改口径的顺序：先改 YAML 或 skill，再改代码与测试，最后回头检查文档里的引用是否仍成立。三处不一致时以 YAML / skill 为准并在 PR 说明。
- 需求文档新增验收条时，同步更新 `tests/acceptance/` 映射表与需求 14.1 索引。

## 性能

- 收盘全量整链目标 1 分钟内；`scripts/bench.py` 在金样本目录上跑，CI 记录各步耗时，任一步超上次 1.5 倍则失败。
- 全市场列运算用 Polars 表达式，不写 Python 逐行循环；按板块分组用 `group_by`，不用循环切片。
- 前端表格超过 200 行用虚拟滚动；ECharts 按需引入组件，不整包引入。

## 代码质量

- Python：ruff（lint + format）、mypy strict、pytest。提交前 `make check` 全绿。
- TypeScript：strict，eslint + prettier，`vue-tsc --noEmit` 通过。
- 函数有类型标注；公共函数有一行 docstring 说明意图，不写重复代码逻辑的注释。
- 测试与实现同 PR。改规则必须跑 `tests/acceptance/` 与金样本。
- 提交信息：一行祈使句说明做了什么，正文说明为什么。一个逻辑变更一个提交。

## 需要按需加载的 skills

`.cursor/skills/` 下有十三个 skill。动手前先读相关的那一个：

- 工程：`python-code-standards`、`backend-fastapi-duckdb`、`frontend-vue-echarts`、`ui-design-system`、`duckdb-schema`、`data-adapter-akshare`、`rules-engine`、`acceptance-testing`、`release-packaging`、`data-incident-runbook`（数据异常排查）
- 领域：`a-share-market-knowledge`（交易规则与数据口径）、`capital-flow-indicators`（全部指标公式）、`quant-algorithms`（统计算法与偏差防范）

写任何指标或规则前，先读 `capital-flow-indicators` 与 `quant-algorithms`；涉及涨跌停、停复牌、成分、北向时读 `a-share-market-knowledge`。
