"""规则引擎：每条规则一个纯函数，输出证据。约定见 rules-engine skill。"""

from moneytool.rules.base import Evidence, FeatureRow, RuleResult, all_hit, any_hit, compare

__all__ = ["Evidence", "FeatureRow", "RuleResult", "all_hit", "any_hit", "compare"]
