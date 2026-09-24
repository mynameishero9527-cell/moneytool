---
name: data-incident-runbook
description: moneytool 数据异常排查手册。页面出现「数据缺失」「未经对账」「降级」「历史不足」、盘中分段没更新、收盘迟迟不确认、命令行报锁占用、时钟偏差警告、AkShare 契约失败时使用；按症状给检查项与修复命令。
paths: ["moneytool/adapters/**", "moneytool/scheduler/**", "scripts/**"]
---

# 数据异常排查

原则：先看 `data_quality` 表与数据状态页，再看日志，最后才动数据。任何修复都通过命令行工具，不手改 DuckDB。缺失的数据按需求 11.3 标缺失，不填补。

## 通用第一步

```bash
moneytool doctor                         # 各源连通性、契约、时钟偏差、锁状态
moneytool status --date 2026-09-24       # 当日各 segment 采集状态、confirmed 是否存在、降级标志
tail -n 200 ~/.moneytool/logs/moneytool-$(date +%F).log | jq -r 'select(.level!="info")'
```

`data_quality.status` 取值：`missing`（拉取失败）、`captcha`（滑块）、`contract_error`（字段变更）、`reconcile`（对账偏差，`value` 为偏差率）、`stale`（数据未更新）、`degraded`（价格模式）。

## 症状 → 检查 → 修复

| 症状 | 检查 | 修复 |
| --- | --- | --- |
| 盘中某分段显示「未采集」 | `data_quality` 该 segment 是否 `missing` / `captcha`；日志里 `eastmoney.flow_rank` 的 HTTP 状态；本机是否休眠错过触发（`misfire`） | 分段不补（需求 11.2）。若是 `captcha`：本机浏览器打开 `quote.eastmoney.com` 完成一次验证，下一段会自动恢复。若是休眠：无需处理，收盘全量会补 confirmed |
| 连续多段 `captcha` | 同一 IP 当日个股级请求是否过多（回补与盘中同时跑） | `moneytool backfill --pause` 暂停回补到 20:30 后；确认 `rate_limit.eastmoney.per_stock_interval_seconds ≥ 5` |
| 收盘后到 20:00 仍「未经对账」 | 东财日频接口是否返回当日行（`moneytool fetch --date <d> --source eastmoney.flow_daily --dry-run`） | 数据商延迟：次日 08:00 自检会自动重拉并 `recompute`。若 08:00 后仍缺，手工 `moneytool fetch --date <d> --source eastmoney.flow_daily && moneytool recompute --date <d>` |
| 对账偏差率 > 20% | `flow_snapshot` 当日 segment 数是否齐；是否有跨段（`spans_missing`） | 偏差是事实，记录即可；若因跨段导致，页面已标出，不重算。偏差持续多日说明分档口径变了，走契约检查 |
| `contract_error` | `doctor` 输出的列差异；AkShare 是否被升级（`pip show akshare`） | 回退到 `pyproject.toml` 锁定版本：`pip install akshare==<locked>`；确实是源改字段则改契约 + 录新 fixture，走 PR |
| 页面顶部「降级：价格模式」 | 当日资金流分段与日频是否全部 `missing` | 恢复后 `moneytool recompute --date <d>`，旧快照自动保留 |
| 指数或归因显示「历史不足」 | `moneytool status` 的回补进度；`bar_daily` / `flow_daily` 最早日期 | 等回补完成；或 `moneytool backfill --resume`。不要缩短窗口 |
| 阶段显示「沿用」 | 当日是否降级；`sector_stage_confirmed` 昨日是否存在 | 同降级处理 |
| `recompute` 报「锁被占用」 | `~/.moneytool/.lock` 持有者与开始时间（命令输出已给） | 主进程在跑时改用 `moneytool recompute` 会自动转 HTTP；若持有者进程已不存在，`moneytool doctor --unlock` 清理 |
| 启动警告「时钟偏差 > 2 分钟」 | `timedatectl` / 系统时间同步设置 | 校准系统时钟后重启程序；偏差期间的分段边界以服务器时间戳记录，无需重算 |
| 成分「近似」标记 | 目标日期是否早于首次运行日 | 预期行为（架构 4.5），不处理 |
| 板块全部「小样本」 | `sector_member_snapshot` 当日是否为空（申万接口失败） | `moneytool fetch --date <d> --source shenwan.cons`；成分表会回退到最近一次快照，阶段不受影响 |
| 磁盘占用异常增长 | `raw/` 是否超过 90 天未清理；日志是否轮转 | `moneytool doctor --disk` 输出各目录大小；夜间任务 21:30 应自动清理，检查调度器是否在跑 |

## 不该做的事

- 不用昨日值、均值或其他源填补缺失资金流。
- 不手工编辑 `*_confirmed` 表；要改就 `recompute`，旧快照保留。
- 不在盘中跑 `backfill` 全市场个股级接口。
- 不为了「让页面有数」缩短 250 日 / 60 日窗口。

## 事后

每次 `captcha` 或 `contract_error` 事件在 `docs/incidents/YYYY-MM-DD.md` 记一条：现象、根因、处理、是否需要改契约或限速参数。连续三次同类事件要改架构 3.3 的纪律参数并走 PR。
