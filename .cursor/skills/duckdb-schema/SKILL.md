---
name: duckdb-schema
description: 在 moneytool 里建表、改表、写迁移、设计快照表或查询 DuckDB 时使用。涵盖表命名、主键与快照约定、intraday 与 confirmed 分离、生效区间成分表、迁移脚本、读写连接分离、Parquet 直读。
paths: ["moneytool/storage/**"]
---

# DuckDB 存储约定

单文件 `~/.moneytool/moneytool.duckdb`。全部结构化数据在这里；原始响应在 `raw/` Parquet；参数在 `params/` YAML。

## 连接

- 写连接：进程内唯一，`storage/conn.py: get_rw_conn()`，由调度线程持有，写操作用 `threading.Lock` 串行。
- 读连接：`get_ro_conn()` 以 `read_only=True` 打开同一文件，API 每请求取一个（连接池 4 个）。
- 不在 API 路由里拿写连接。
- 关闭前 `CHECKPOINT`。

## 命名

- 表名、列名 `snake_case`，单数名词：`security`、`sector`、`bar_daily`。
- 时间粒度后缀：`_daily`、`_intraday`、`_snapshot`。
- 结果类表按确认状态后缀：`*_confirmed`、`*_intraday`，两者结构相同，`_intraday` 多一列 `segment`。
- 主键显式声明；结果表主键含 `trade_date`。

## 关键约定

### 快照而非覆盖
结果表（`sector_stage_*`、`stock_role_*`、`action_list_*`）每交易日一份，写入用 `INSERT OR REPLACE`，重跑同一日幂等。回看只 `WHERE trade_date = ?`，不重算。每条带 `param_version`。

### 成分用快照 + 生效区间
- 申万、概念：`sector_member_snapshot(sector_id, code, snapshot_date)` 每日一份。查某日成分：`snapshot_date = (SELECT max(snapshot_date) WHERE snapshot_date <= ?)`。
- 指数：`index_member(index_id, code, effective_from, effective_to)`，`effective_to` 为 NULL 表示当前。
- 新纳入判定（需求 8.1）：首次出现的 `snapshot_date` 距目标日 < 5 个交易日。

### 数据质量表
`data_quality(source, endpoint, trade_date, segment, status, reason, value, created_at)`，`status in ('missing','captcha','contract_error','reconcile','stale')`。计算层读它决定「数据缺失」标记，API 读它给 `meta.status`。

### 单位
金额 `DOUBLE` 单位元；比例 `DOUBLE` 0–1；日期 `DATE`；时间戳 `TIMESTAMPTZ`；代码 `VARCHAR` 形如 `600000.SH`。

## 迁移

- `storage/migrations/NNNN_<desc>.sql`，纯 SQL，幂等（`CREATE TABLE IF NOT EXISTS`、`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`）。
- `schema_version(version, applied_at)` 记录已应用。启动时顺序应用未应用的。
- 不写删列、改类型的迁移；需要时新建表 + 回填 + 切换视图。
- 每个迁移附 `tests/storage/test_migrations.py` 里的一条：空库应用全部迁移后表结构与 `storage/schema_expected.json` 一致。

## Parquet 直读

- 原始响应与大历史（如 5 年日线）可以不导入，直接 `read_parquet('raw/.../*.parquet')` 建视图。第一版日线导入表，原始响应保留为文件。
- 大批量写入：先写 Parquet，再 `INSERT INTO ... SELECT FROM read_parquet(...)`，比逐行快。

## 查询文件

- SQL 放 `storage/queries/<domain>/<name>.sql`，用 `?` 占位，Python 侧 `conn.execute(sql, params).pl()`。
- 复杂聚合（事后统计、多板块对比）写成视图 `v_*`，迁移里创建。
- 查询必须带 `trade_date` 过滤，不做全表扫描的 API。

## 容量

单用户 2 年资金流 + 5 年日线约 2–4 GB。每周任务 `VACUUM`；`raw/` 超过 90 天的东财「今日」快照可清理（日频正式值已入库）。

## 检查清单

主键含 `trade_date`；结果表分 intraday / confirmed；成分用快照或生效区间；迁移幂等且有结构测试；API 只用只读连接；单位与代码格式一致。
