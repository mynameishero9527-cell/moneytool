---
name: data-adapter-akshare
description: 在 moneytool 里新增或修改数据源适配器（AkShare / 东方财富 / Baostock / 申万 / 中证 / 巨潮）时使用。涵盖适配器接口、字段契约、原始响应缓存、限速与退避、数据质量记录、doctor 检查，以及东方财富反爬的应对。
paths: ["moneytool/adapters/**"]
---

# 数据源适配器

免费接口不稳定，适配器层的职责是把不稳定挡在计算层之外：拿到就落盘、不符合契约就报错、失败就标缺失。资金流只允许东方财富一个源（需求 6 节、架构 3.1）。

## 接口

每个源一个模块 `moneytool/adapters/<source>.py`，每个接口一个函数，返回 `pl.DataFrame`，列名与类型固定：

```python
class Adapter(Protocol):
    source: str  # "eastmoney" | "baostock" | "shenwan" | "csindex" | "cninfo" | "external"

    def fetch(self, endpoint: str, **params) -> pl.DataFrame: ...
    def contract(self, endpoint: str) -> Contract: ...
    def health(self) -> HealthReport: ...
```

调用方只用 `fetch`。`fetch` 内部顺序固定：

1. **查缓存**：`raw/{source}/{endpoint}/{trade_date}/{params_hash}.parquet` 存在且成功 → 直接读。
2. **限速**：从该源的令牌桶取令牌（`config.rate_limit.<source>`）。
3. **请求**：调 AkShare 或 httpx；超时 30 秒。
4. **落盘**：原始 DataFrame 原样写 Parquet，附 `_meta.json`（请求参数、时间、行数、AkShare 版本）。
5. **契约校验**：列名、类型、非空列、行数下限。失败抛 `ContractError`，原始文件保留，写 `data_quality`。
6. **标准化**：改列名为 snake_case、单位统一（金额元、比例 0–1、日期 `pl.Date`）、代码统一为 6 位 + 交易所后缀 `600000.SH`。

失败处理：按 10s、30s、90s 退避三次；仍失败抛 `AdapterError`，调用方写 `data_quality(status="missing")`，不再当日重试。

## 契约

`contract(endpoint)` 返回：

```python
@dataclass(frozen=True)
class Contract:
    columns: dict[str, pl.DataType]  # 标准化后的列名与类型
    required_non_null: tuple[str, ...]
    min_rows: int
    unit_notes: str  # 写明源单位与转换
```

契约放 `adapters/contracts/<source>.py`，测试 `tests/adapters/test_contracts.py` 用录制响应逐个验证。AkShare 升级前先跑这组测试。

## 东方财富的特殊纪律

- 资金流用**全市场排名接口**一次拿全部：`stock_individual_fund_flow_rank(indicator="今日")`、`stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流" | "概念资金流")`。每个分段只调这 3 次。
- 个股级接口（`stock_individual_fund_flow`、`stock_zh_a_hist`）只在回补和夜间补缺用，间隔 ≥ 5 秒，串行。
- 遇到滑块验证（响应非 JSON 或返回空）：记 `data_quality(reason="captcha")`，停止该源当日所有个股级请求，UI「数据状态」页提示用户在本机浏览器打开 `quote.eastmoney.com` 完成一次验证。
- 固定 AkShare 版本在 `pyproject.toml`（`akshare==x.y.z`），升级走 PR，附契约测试结果。
- 东财分档：超大单 ≥ 100 万、大单 20–100 万、中单 5–20 万、小单 < 5 万，主力 = 超大 + 大。标准化列名：`net_super`, `net_large`, `net_medium`, `net_small`, `net_main`，以及各自的 `_ratio`（占成交额比例，0–1）。

## 分段差分（`compute/flow.py` 的输入约定）

- 每次「今日」拉取存为 `flow_snapshot(code, trade_date, captured_at, segment, net_*)`。
- 分段值 = 本 segment 快照 − 上一个成功 segment 快照。上一段缺失时跨段差分，并在结果标 `spans_missing=True`。
- 竞价段用 9:25–9:30 之间的第一次拉取。
- 收盘后拉日频接口作为 `flow_daily` 正式值，与全天快照对账，偏差率写 `data_quality(reason="reconcile", value=diff_ratio)`。

## 其他源要点

- **Baostock**：登录一次复用会话；日线用 `adjustflag="3"` 不复权 + 单独拉复权因子；交易日历 `query_trade_dates`。
- **申万**：`sw_index_cons` 每日快照落 `sector_member_snapshot`；一级二级映射从 `sw_index_second_info` 取。
- **中证**：`index_stock_cons_csindex` 含纳入日期，直接写 `index_member.effective_from`。
- **巨潮**：公告按关键词分类，原文链接保存，进入人工复核队列；不自动认定处罚。
- **外部资产**：`futures_foreign_hist`、`index_global_hist_em`、`fx_spot_quote`；映射表 `external_asset_map` 由口径页维护，无映射的板块标「无对应外部资产」。

## doctor

`moneytool doctor` 对每个源每个接口：拉一次最小请求、跑契约、报告延迟与结果。输出表格；任何失败非零退出。CI 每日跑一次带 `network` 标记的 doctor。

## 新增一个接口的步骤

1. 在 `contracts/<source>.py` 写契约。
2. 用 `scripts/record_fixture.py <source> <endpoint>` 录一份原始响应到 `tests/fixtures/raw/`。
3. 实现 `fetch` 分支与标准化。
4. 写契约测试与标准化测试。
5. 在 `doctor` 注册。
6. 更新架构文档 3.2 表格。

## 检查清单

一次拉全市场而不是逐只；有契约；原始响应落盘；失败标缺失不填补；单位与代码格式已标准化；doctor 已注册；AkShare 版本已固定。
