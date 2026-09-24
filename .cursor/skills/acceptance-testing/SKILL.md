---
name: acceptance-testing
description: 为 moneytool 编写或维护测试时使用，尤其是把需求第 14 节的 34 条验收标准映射为 pytest、维护历史交易日金样本、构造规则测试的特征行、录制适配器 fixture。
paths: ["tests/**"]
---

# 验收测试与金样本

需求第 14 节有 34 条验收标准，每条对应至少一个测试，标记 `acceptance`。规则改动必须让这组测试与金样本一起通过，或者在 PR 里逐条解释差异。

## 目录

```text
tests/
  unit/             计算与规则的单测，手工构造小 DataFrame
  acceptance/       test_ac_01.py … test_ac_34.py，一条验收一个文件
  golden/           <date>/features.parquet, expected_stages.parquet, expected_roles.parquet, expected_actions.parquet
  adapters/         契约测试，用 fixtures/raw 录制响应
  api/              路由测试，用内存 DuckDB
  fixtures/
    raw/            录制的原始响应
    features/       构造特征行的 builder
  conftest.py
```

## 验收映射

每个 `test_ac_NN.py` 顶部 docstring 写需求原文，测试名描述场景。示例映射：

| 验收 | 测试做什么 |
| --- | --- |
| 1 退潮板块买入为 0 | 构造板块阶段=退潮 + 核心股特征，跑 actions，断言 buy 为空 |
| 2 高集中度不进扩散前半段 | 构造上涨家数占比下降、前 5 只占比 > 50%，断言 stage ≠ 扩散加速前半段 |
| 12 二级启动一级退潮 | 二级阶段=启动、一级=退潮，断言 buy 为空且核心股在 strong 名单 |
| 15 多阶段同命中取高优先级 | 特征同时满足启动与高潮，断言 stage=高潮拥挤、suppressed 含启动 |
| 16 防抖 | 昨日冰点、今日命中启动一次，断言 stage=冰点、suspected_to=启动 |
| 17 退潮即切 | 昨日扩散、今日命中退潮，断言 stage=退潮 |
| 18 先行透支 | 个股 5 日涨幅 = 板块 × 2.5，断言在 strong、标 overextended、不在 buy |
| 21 跟踪闭环 | 进入 buy 后次日失效，断言 tracking 仍存在；模拟 20 日后无卖出，断言 expired |
| 23 提醒去重 | 同标的同事件盘中两次，断言 alert_log 一条；收盘撤销，断言补发 revoked |
| 30 控盘风险 | 满足低位企稳但流通市值 29 亿，断言不在 lowbase、tag 含 control_risk[1] |
| 34 多板块对比 | API 层：3 板块 + cumulative，断言每日三者都有值与阶段 |

其余条目同法。缺一条就是没做完。

## 特征行 builder

`fixtures/features/builder.py` 提供 `sector_row(**overrides)`、`stock_row(**overrides)`，默认值是「什么都不命中」的中性行，测试只覆盖要改的字段。这样每个测试只写与验收相关的几个字段。

## 金样本

- 5 个历史交易日，覆盖：一个明显主线日、一个全市场净流出日、一个概念炒作日、一个板块高潮转分歧日、一个普通震荡日。日期在 `tests/golden/DATES.md` 记录并说明为什么选。
- `features.parquet` 是当日全部特征行（从真实数据导出一次固定下来）；`expected_*.parquet` 是当时参数版本的输出。
- `pytest -m golden` 对每个日期重算并 `assert_frame_equal`（忽略 `generated_at`）。
- 参数版本变化后：`scripts/golden.py diff vA vB --dates ...` 打印名单增删；人工确认后 `scripts/golden.py accept vB` 更新期望文件，提交时附差异说明。

## 适配器 fixture

- `scripts/record_fixture.py <source> <endpoint> [params]` 联网拉一次，原始响应写 `fixtures/raw/<source>/<endpoint>/<date>.parquet`。
- 契约测试遍历 fixtures 跑 `contract()`，不联网。
- 标 `network` 的测试才真的请求，CI 每日一次。

## API 测试

- `conftest.py` 提供内存 DuckDB（`:memory:`）+ 全部迁移 + 少量种子数据。
- 用 `httpx.AsyncClient(app=app)`，断言 `meta.status`、`meta.param_version` 与业务字段。
- 回看：同一接口传 `trade_date` 为历史日，断言返回 confirmed 快照且不触发计算（用 spy 断言 pipeline 未调用）。

## 覆盖率

- `pytest --cov=moneytool --cov-report=term-missing`，规则与计算模块要求 ≥ 90%，适配器与 API ≥ 70%。数字进 CI 门禁。

## 检查清单

新规则有单测；触及的验收条目测试通过；金样本通过或差异已解释；fixture 已录制且不联网；覆盖率未下降。
