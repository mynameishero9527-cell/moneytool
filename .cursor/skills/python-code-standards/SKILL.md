---
name: python-code-standards
description: moneytool 的 Python 代码规范与工具链。写或审查任何 Python 代码时使用；涵盖 ruff、mypy strict、pytest 配置，类型、命名、错误处理、docstring 与注释、提交规范，以及 `make check` 的内容。
paths: ["moneytool/**/*.py", "tests/**/*.py", "scripts/**/*.py", "pyproject.toml"]
---

# Python 代码规范

目标：读起来不需要猜，改起来有测试兜底，格式不靠人工。

## 工具链

`pyproject.toml` 统一配置：

- `ruff`：lint + format。规则集 `E, F, W, I, N, UP, B, SIM, PL, RUF`，行宽 100，双引号。忽略 `PLR0913`（参数个数）按需在函数级 `noqa`。
- `mypy --strict`：全部包。第三方无类型的（akshare、baostock）在 `[[tool.mypy.overrides]]` 里 `ignore_missing_imports = true`，并在 adapters 边界把返回值转成有类型的 Polars DataFrame。
- `pytest`：`tests/` 下，`-q --strict-markers`。标记：`acceptance`（需求 14 节）、`golden`（金样本）、`network`（需要联网，默认跳过）。
- `pre-commit`：ruff、ruff-format、mypy、`check-yaml`、`end-of-file-fixer`。

`make check` = `ruff check . && ruff format --check . && mypy moneytool && pytest -m "not network"`。

## 类型

- 所有函数签名有参数与返回类型。内部变量按需标注。
- 用 `pl.DataFrame` 而不是 `Any`；DataFrame 的列约定写在函数 docstring 第一行之后的 `Columns:` 段。
- 用 `dataclass(frozen=True)` 或 pydantic 表达配置和结果，不用裸 dict 传递结构化数据。
- 枚举用 `StrEnum`：`Stage`、`Role`、`ListType`、`Segment`、`DataStatus`。
- 日期类型统一 `datetime.date`；时间戳带时区 `Asia/Shanghai`。

## 命名

- 模块、函数、变量 `snake_case`；类 `PascalCase`；常量 `UPPER_SNAKE`。
- 表名与列名和 DuckDB 一致，也是 `snake_case`。
- 布尔用 `is_` / `has_` 前缀。
- 规则函数命名 `rule_<主题>_<条件>`，如 `rule_stage_start_inflow_turn_positive`。
- 不缩写到看不懂：`retention` 不写 `ret`；`net_inflow` 不写 `ni`。

## 结构

- 函数做一件事；超过 50 行考虑拆。
- 纯函数优先：计算、规则、option 生成都不做 IO。IO 集中在 adapters、storage、scheduler。
- 不用全局可变状态。配置通过参数传入。
- 早返回，少嵌套。

## 错误处理

- 适配器：网络与解析错误包装成 `AdapterError(source, endpoint, reason)`，由调用方决定重试或标缺失。不吞异常。
- 计算：输入不满足前提（缺列、空表）抛 `ValueError` 带列名，不返回空结果假装成功。
- API：见 backend skill；不把异常文本直接返回给前端。
- 不用 `except Exception: pass`。

## 注释与 docstring

- 公共函数一行 docstring 说意图与口径来源（引用需求章节号，如「需求 6.1 资金留存」）。
- 注释只写「为什么」：口径取舍、数据源坑、与需求的对应。不写「循环遍历」「返回结果」这类复述。
- 魔法数字一律进 `params/*.yaml`，代码里通过 `params.<section>.<key>` 引用。

## 测试

- 每个计算与规则函数有单测，用手工构造的小 DataFrame，断言精确值。
- 用 `polars.testing.assert_frame_equal`。
- 适配器测试用录制的原始响应（`tests/fixtures/raw/`），不联网；联网测试标 `network`。
- 验收测试与金样本见 `acceptance-testing` skill。

## 提交

- 一行标题，祈使句，说明做了什么；正文说明为什么与口径影响。
- 一个逻辑变更一个提交；规则改动与参数版本新增放同一提交。
- 提交前 `make check` 全绿。

## 检查清单

类型完整；无魔法数字；纯函数无 IO；异常有类型；docstring 引用需求章节；测试同 PR；`make check` 通过。
