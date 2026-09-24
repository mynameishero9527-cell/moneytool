---
name: capital-flow-indicators
description: moneytool 全部资金流、价格、板块结构指标的精确定义与 Polars 计算方式。实现或修改需求 6.1 派生量（净流入比例、留存、斜率、相对强度、市场份额、集中度、扩散度、价资同向、脉冲）、8.7 评分分项、7.5 归因指标、8.9 低位企稳指标时使用。
paths: ["moneytool/compute/**", "moneytool/rules/**"]
---

# 资金与板块指标定义

需求 6.1 给了指标的用途，这里给可实现的公式、列名、窗口、边界处理。所有窗口按**交易日**计数，停牌日跳过（该股当日无行）。列名即 `feature_daily` 的列名。

## 基础列（来自 flow_daily、bar_daily）

| 列 | 定义 | 单位 |
| --- | --- | --- |
| `amount` | 成交额 | 元 |
| `pct_chg` | 涨跌幅（源字段） | 小数 |
| `turnover` | 换手率（流通股本口径） | 小数 |
| `close_adj` | 后复权收盘价 | 元 |
| `high_adj`, `low_adj` | 后复权高低 | 元 |
| `net_main`, `net_super`, `net_large`, `net_medium`, `net_small` | 各档净流入 | 元 |
| `is_limit_up` | 收盘 = 涨停价 | bool |
| `is_one_word` | 一字板 | bool |
| `is_suspended` | 停牌 | bool |

## 资金流派生

| 列 | 公式 | 说明 |
| --- | --- | --- |
| `main_ratio` | `net_main / amount` | 占成交额比例；`amount = 0` 时为 null |
| `main_chg_1d` | `net_main - net_main.shift(1)` | 相对前一日变化 |
| `main_dev_5d` | `net_main / mean(net_main, 5 日) - 1` | 相对近 5 日均值偏离；均值 ≤ 0 时为 null，脉冲判定只在均值 > 0 时有效 |
| `main_slope_3d`, `main_slope_5d` | 对最近 N 日 `main_ratio` 做 OLS，取斜率 | 用比例不用金额，避免成交额放大造成假斜率；实现见 quant-algorithms |
| `inflow_streak` | 连续 `net_main > 0` 天数（正）或 `< 0` 天数（负） | 遇 0 或 null 重置 |
| `retention_5d` | `sum(net_main where pct_chg > 0, 5 日) - sum(abs(net_main) where pct_chg < 0 and net_main < 0, 5 日)` | 需求 6.1 资金留存：涨日流入减跌日流出。跌日若为净流入不减；涨日若为净流出不加 |
| `price_flow_agree` | `sign(pct_chg) == sign(net_main)` | 价资同向；任一为 0 记 false |
| `super_share` | `net_super / net_main` | 超大单占比；`net_main ≤ 0` 时 null |
| `super_share_chg_5d` | `super_share - mean(super_share, 5 日)` | |
| `is_pulse` | `net_main > significant_multiple × mean(net_main, 前 5 日)` 且前 5 日均值 > 0 | 一日脉冲；用**前** 5 日不含当日 |
| `pulse_giveback` | 脉冲日后 2 个交易日 `sum(net_main) ≤ -majority_ratio × 脉冲日 net_main` | 脉冲回吐（需求 8.2） |
| `main_ratio_pct_20d` | 当日 `main_ratio` 在自身近 20 日中的分位 | 用于「自身常态区间」 |

## 价格派生

| 列 | 公式 |
| --- | --- |
| `ret_3d`, `ret_5d`, `ret_20d` | `close_adj / close_adj.shift(N) - 1` |
| `ma_5`, `ma_10` | `mean(close_adj, N)` |
| `high_20d`, `high_250d` | `max(high_adj, N)` |
| `low_60d`, `low_250d` | `min(low_adj, N)` |
| `is_new_high_20d` | `close_adj >= high_20d.shift(1)` |
| `drawdown_250d` | `close_adj / high_250d - 1` |
| `range_pos_250d` | `(close_adj - low_250d) / (high_250d - low_250d)`；分母 0 时 null |
| `max_dd_5d` | 近 5 日内最大回撤（从窗口内高点到之后低点） |
| `amplitude_20d`, `amplitude_60d` | `mean((high_adj - low_adj) / close_adj.shift(1), N)` |
| `turnover_ma_20d` | `mean(turnover, 20)` |
| `amount_ratio_20_60` | `mean(amount, 20) / mean(amount, 60)` |
| `gap_abs_5d` | `mean(abs(open_adj / close_adj.shift(1) - 1), 5)` 跳空幅度，用于外部联动归因 |

## 相对强度

- 板块相对全 A 等权：`sector_rs_5d = sector_ret_5d - eqw_ret_5d`，单位百分点（×100 后展示）。
- 个股相对板块：`stock_rs_5d = ret_5d - sector_ret_5d`，依据板块是二级 / 概念，按 `basis_sector_id` 计算，一只股票在多个板块有多行。
- 个股相对全 A：`stock_rs_eqw_5d = ret_5d - eqw_ret_5d`。
- 全 A 等权收益：每日对合格股票 `pct_chg` 取算术平均，再累乘得区间收益。合格条件见 a-share-market-knowledge。
- 板块收益：用申万指数行情（一级、二级），概念用成分股等权（东财概念指数口径不透明，自算更稳）。

## 板块结构

| 列 | 公式 | 说明 |
| --- | --- | --- |
| `sector_net_main` | 成分股 `net_main` 求和，剔除 `is_one_word` | 需求 8.1 |
| `sector_main_ratio` | `sector_net_main / sum(amount)` | |
| `breadth` | 上涨家数 / 有效家数 | 有效 = 非停牌；需求 11.3 |
| `limit_up_ratio` | 涨停家数 / 有效家数 | |
| `concentration_top5` | 前 5 只正净流入之和 / 全部正净流入之和 | 只用正值，避免负值抵消；无正值时 null |
| `market_share` | `sector_net_main / sum(max(sector_net_main, 0) over all L1)` | 市场流入份额；全市场正值和为 0 时 null；一级、二级、概念各自算但分母都用一级的正值和 |
| `small_cap_share` | 资金体量为小、微的成分股正净流入 / 全部正净流入 | 概念炒作归因 |
| `sector_turnover` | 成分股换手按流通市值加权 | |
| `sector_amount_ratio_20_60` | 同个股 | |
| `member_count` | 有效成分数 | < 8 标小样本 |

## 分位与排名（依据板块内）

- 排名类条件（前 30%、前 10%、后 30%）在 `basis_sector_id` 分组内用 `rank(method="average", descending=True) / n`，剔除连板、新纳入、复牌首日、一字板、停牌后再排。
- 组内 n < 8 时不排名，角色全部为「其他」，板块标小样本。
- 「自身近 20 日常态区间」= 该指标近 20 日的 10%–90% 分位区间；「极端」= 高于 90 分位（需求 6.3）。

## 评分分项映射（需求 8.7）

| 分项 | 用哪列 | 20 分与 0 分 |
| --- | --- | --- |
| 相对板块 | `stock_rs_5d` 在板块内分位 | 前 10% → 20；后 10% 或 < −clear_pp → 0 |
| 相对全 A | `stock_rs_eqw_5d` 全市场分位 | 前 20% 且 > 0 → 20；< −clear_pp → 0 |
| 资金留存 | `retention_5d`、`inflow_streak` | 留存 > 0 且 streak ≥ 5 → 20；留存 < 0 且 streak ≤ −5 → 0 |
| 价资一致 | `price_flow_agree`、`is_pulse`、背离 | 同向且非脉冲 → 20；背离出货或脉冲回吐 → 0 |
| 体量匹配 | 资金体量档、`main_ratio_pct_20d` | 中以上且分位在 10–90% → 20；微或分位极端 → 0 |

中间档按全市场当日分位线性映射到 0–20 后取整。

## 归因指标（需求 7.5）

- 同月历史：取过去 5 个自然年同一月份，每年算 `sector_ret_month - eqw_ret_month`；统计为正年数、中位数、与其他月份中位数的差。
- 外部联动：`corr(sector_pct_chg, external_ret.shift(0), 20 日)`，其中外部资产用**前一交易日**（隔夜）收益对齐 A 股当日；启动前 3 日外部累计变动；`gap_abs_5d / mean(gap_abs, 60) `。
- 概念炒作：对应行业阶段、`limit_up_ratio` 60 日分位、`concentration_top5`、`sector_turnover / mean(60)`、`small_cap_share`。

## 边界处理清单

- 任何分母为 0 或窗口内有效样本 < 窗口长度的 60% → null，不算 0。
- 停牌日不产生行；窗口用「最近 N 个有行的交易日」，Polars 里先 `filter(~is_suspended)` 再 `rolling`。
- 复权价只用于区间和位置指标；展示价用不复权。
- 板块聚合前剔除一字板；个股排名前剔除连板、新纳入、复牌首日。
- 概念成分「新纳入」看 `sector_member_snapshot` 首次出现日期。
- 所有比例列存 0–1，展示层乘 100。
