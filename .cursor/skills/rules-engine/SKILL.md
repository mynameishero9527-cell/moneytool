---
name: rules-engine
description: 在 moneytool 里新增、修改或调试板块阶段、个股角色、名单、买卖点、归因、排除标签等规则时使用。涵盖规则函数签名、证据结构、阈值参数化、阶段优先级与防抖的编排、参数版本新增、金样本比对。
paths: ["moneytool/rules/**", "moneytool/compute/**", "moneytool/params/**"]
---

# 规则引擎

需求的核心是可解释：每个标签都要能展开看命中了哪几条、当日值和阈值是多少（需求 7.1、8.3、2 节）。规则引擎的设计就是围绕这一点。

## 规则函数

```python
@dataclass(frozen=True)
class Evidence:
    rule: str            # 规则名，如 "stage.start.inflow_turn_positive"
    metric: str          # 指标列名
    value: float | None  # 当日值
    threshold: float | None
    op: str              # ">=", "<", "in", ...
    hit: bool
    note: str = ""       # 可选说明，如「板块近 20 日 90 分位」

@dataclass(frozen=True)
class RuleResult:
    hit: bool
    evidence: tuple[Evidence, ...]

def rule_xxx(row: FeatureRow, p: Params) -> RuleResult: ...
```

- 纯函数：只读 `row` 与 `p`，不读连接、不读时钟、不读全局。
- 一条规则对应需求里的一条条件；复合条件拆成多条规则再组合，组合逻辑在 `compute/*.py`。
- 未命中的规则也要返回完整 `Evidence`（`hit=False`），前端灰显而不是隐藏。
- 规则名三段式 `<域>.<主题>.<条件>`，域取 `stage | role | avoid | buy | sell | point | lowbase | exclude | attribution | market`。

## 阈值

- 全部来自 `params/<version>.yaml`。结构按需求章节组织：

```yaml
version: v1
effective_from: 2026-10-01
note: 初始版本，对应需求 0.6
windows: {pulse: 1, short: 3, persist: 5, cycle: 20}
qualifiers:             # 需求 6.3
  significant_multiple: 2.0
  majority_ratio: 0.6
  clear_pp: 2.0
  extreme_pct: 0.9
  flat_abs_pct: 0.01
  volume_multiple: 1.5
  large_outflow_pct: 0.1
  tail_share: 0.5
stage: {...}            # 7.1
role: {...}             # 8.1 / 8.2
buy: {...}              # 8.3 含 position_multiple: 2.0, hype_position_multiple: 1.5
exclude: {...}          # 8.10
lowbase: {...}          # 8.9
attribution: {...}      # 7.5
score: {...}            # 8.7
```

- `Params` 用 pydantic 加载并校验，缺键报错。代码引用 `p.buy.position_multiple`，不写数字。
- 新阈值：先加进 YAML 与 `Params` 模型，再在规则里用。

## 阶段编排（`compute/stages.py`）

1. 对每个板块每日跑六个阶段的全部规则，得到六个 `RuleResult`。
2. 按需求 7.1 优先级 `退潮 > 分歧背离 > 高潮拥挤 > 扩散加速 > 启动 > 冰点` 取第一个 `hit` 的为候选；其余 `hit` 的记为 `suppressed`。
3. 防抖：读昨日 `sector_stage_confirmed.stage`。候选 ≠ 昨日：若候选为退潮，直接切；否则查前一日候选是否相同，相同则切换，不同则保持昨日并标 `suspected_to=候选`。
4. 迁移路径：对照允许路径表，不在表内的标 `abnormal_transition=True`。
5. 扩散加速拆前后半段：按 `qualifiers.extreme_pct` 判定。
6. 输出行：`stage, half, suspected_to, abnormal_transition, suppressed[], evidence[]`。

盘中模式跳过第 3–4 步，只输出候选作为「盘中倾向」。

## 角色与名单编排

- 角色（8.1、8.2）先算，再算排除标签（8.10），再算买入 / 卖出 / 低位企稳（8.3、8.4、8.9），最后买卖点（8.8）。顺序不能变，因为后者读前者。
- 排名类条件（前 30%、前 10%）用 Polars `rank(descending=True) / count` 在依据板块分组内算，剔除连板、新纳入、复牌首日、一字板（8.1）。
- 每条名单记录带 `basis_sector_id`、`basis_level`（L1 / L2 / concept）、完整 `evidence[]`、`invalidation[]`（失效条件的当前状态）。
- 概念主导行业（8.3 第 3 条）每日算一次存 `sector.dominant_l1`。

## 参数版本新增

1. 复制最新 YAML 为 `params/v{n+1}.yaml`，改 `version`、`effective_from`、`note`。
2. 改阈值。
3. 跑 `pytest -m acceptance` 与 `pytest -m golden`；金样本会失败是预期的，用 `scripts/golden.py diff v{n} v{n+1} --dates ...` 输出名单差异，人工确认后 `scripts/golden.py accept v{n+1}`。
4. 提交时把 YAML、金样本更新、差异说明放同一提交。
5. 旧版本文件不动。

## 金样本

- `tests/golden/<date>/` 存 5 个历史交易日的输入特征与期望输出（阶段、角色、名单）。
- 规则改动后先跑金样本，差异必须能用需求条款解释。

## 调试

- `moneytool explain sector <id> --date` / `moneytool explain stock <code> --sector <id> --date` 打印该日全部证据，与前端证据卡片同源。
- 不用 print 调试规则，用 explain 命令。

## 检查清单

规则是纯函数；未命中也返回证据；阈值来自 YAML；阶段走优先级和防抖；名单带依据板块与失效条件；改规则跑了验收与金样本；参数版本只增不改。
