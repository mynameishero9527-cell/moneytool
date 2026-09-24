"""需求 8.6 身份标签、8.10 排除标签与不可交易过滤。

- 身份标签不随资金方向翻转：概念、资金体量、所属指数、交易板块、流通市值档。
- 控盘风险：①③④ 与 ② 资金体量「微」任一；③ 前十大流通股东数据未接入，照实标「数据未接入」，不填补。
- 历史暴涨暴跌：五条满足任二；①② 用 3 年日线计算，③④⑤（股东户数、处罚、减持公告）未接入。
- 流通市值用东财 / Baostock 的流通市值近似自由流通市值（口径页说明）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import duckdb
import polars as pl

from moneytool.compute.sector import amount_tier
from moneytool.params import Params
from moneytool.rules.base import Evidence, compare
from moneytool.storage.repo import to_json

AMOUNT_TIER_ZH = ("巨量", "大", "中", "小", "微")
FLOAT_MV_TIER_ZH = ("超大", "大", "中", "小", "微")
MICRO_TIER = 4
INDEX_NAME_ZH = {
    "000016.SH": "上证50",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "932000.CSI": "中证2000",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
}
NOT_CONNECTED = "数据未接入"


def crash_history(conn: duckdb.DuckDBPyConnection, day: dt.date, p: Params) -> pl.DataFrame:
    """近 N 年：①「20 日涨幅 ≥ 60% 后 40 日内回撤 ≥ 40%」事件次数；② 炸板 / 天地板次数。"""
    ex = p.exclude
    return conn.execute(
        f"""
        WITH b AS (
            SELECT code, trade_date, close * coalesce(adj_factor, 1.0) AS c, high, limit_up, pct_chg
            FROM bar_daily
            WHERE trade_date > CAST(? AS DATE) - INTERVAL {int(ex.crash_years)} YEAR
              AND trade_date <= ? AND NOT is_suspended
        ),
        w AS (
            SELECT code, trade_date,
                   c / lag(c, {int(ex.crash_run_days)}) OVER (PARTITION BY code ORDER BY trade_date) - 1 AS run_ret,
                   min(c) OVER (PARTITION BY code ORDER BY trade_date
                                ROWS BETWEEN 1 FOLLOWING AND {int(ex.crash_drawdown_days)} FOLLOWING) / c - 1 AS fwd_dd,
                   coalesce(high >= limit_up AND pct_chg <= ?, FALSE) AS blowup
            FROM b
        ),
        f AS (
            SELECT code, trade_date, blowup,
                   coalesce(run_ret >= ? AND fwd_dd <= ?, FALSE) AS ev
            FROM w
        ),
        e AS (
            SELECT code, trade_date, blowup,
                   ev AND NOT coalesce(lag(ev) OVER (PARTITION BY code ORDER BY trade_date), FALSE) AS ev_start
            FROM f
        )
        SELECT code,
               count(*) FILTER (WHERE ev_start) AS crash_events,
               max(trade_date) FILTER (WHERE ev_start) AS crash_last,
               count(*) FILTER (WHERE blowup) AS blowups,
               max(trade_date) FILTER (WHERE blowup) AS blowup_last
        FROM e GROUP BY code
        """,
        [day, day, -ex.blowup_drop, ex.crash_run_ret, -ex.crash_drawdown],
    ).pl()


def load_index_membership(conn: duckdb.DuckDBPyConnection, day: dt.date) -> pl.DataFrame:
    return conn.execute(
        "SELECT code, list(index_id ORDER BY index_id) AS indices FROM index_member "
        "WHERE effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?) GROUP BY code",
        [day, day],
    ).pl()


def float_mv_tier(mv: pl.Expr, tiers: tuple[float, float, float, float]) -> pl.Expr:
    huge, large, mid, small = tiers
    return (
        pl.when(mv.is_null())
        .then(None)
        .when(mv >= huge)
        .then(0)
        .when(mv >= large)
        .then(1)
        .when(mv >= mid)
        .then(2)
        .when(mv >= small)
        .then(3)
        .otherwise(4)
        .cast(pl.Int32)
    )


def _ev_dicts(items: list[Evidence]) -> list[dict[str, Any]]:
    return [e.to_dict() for e in items]


def build_profiles(
    today: pl.DataFrame,
    security: pl.DataFrame,
    concepts: pl.DataFrame,
    indices: pl.DataFrame,
    crash: pl.DataFrame,
    p: Params,
) -> pl.DataFrame:
    """个股当日画像。`today` 为当日个股特征；`concepts (code, names)`；`indices (code, indices)`。

    返回 code, amount_tier, identity, exclusions (JSON), tradable, control_risk, crash_risk, is_st。
    """
    ex = p.exclude
    df = (
        today.select(
            "code",
            "amount_ma_20d",
            "float_mv",
            "low_turn_big_move_60d",
        )
        .join(security.select("code", "board", "is_st", "is_delisting"), on="code", how="left")
        .join(concepts, on="code", how="left")
        .join(indices, on="code", how="left")
        .join(crash, on="code", how="left")
        .with_columns(
            amount_tier=pl.when(pl.col("amount_ma_20d").is_null())
            .then(None)
            .otherwise(amount_tier(pl.col("amount_ma_20d"), p.tags.amount_tiers)),
            mv_tier=float_mv_tier(pl.col("float_mv"), p.tags.float_mv_tiers),
            is_st=pl.col("is_st").fill_null(False),
            is_delisting=pl.col("is_delisting").fill_null(False),
        )
        .with_columns(is_micro=(pl.col("amount_tier") == MICRO_TIER).fill_null(False))
    )
    wanted = set(p.tags.index_ids)
    identity: list[str] = []
    exclusions: list[str] = []
    tradable: list[bool] = []
    control: list[bool] = []
    crash_hit: list[bool] = []
    for row in df.iter_rows(named=True):
        idx = [INDEX_NAME_ZH.get(i, i) for i in (row.get("indices") or []) if i in wanted]
        tier = row.get("amount_tier")
        mv_tier = row.get("mv_tier")
        identity.append(
            to_json(
                {
                    "concepts": row.get("concept_names") or [],
                    "is_concept_stock": bool(row.get("concept_names")),
                    "amount_tier": AMOUNT_TIER_ZH[tier] if tier is not None else None,
                    "indices": idx or ["指数外"],
                    "board": row.get("board"),
                    "float_mv_tier": FLOAT_MV_TIER_ZH[mv_tier] if mv_tier is not None else None,
                }
            )
        )
        ctl = [
            compare(
                "exclude.control.float_mv",
                row,
                "float_mv",
                "<",
                ex.control_float_mv_max,
                "流通市值",
            ),
            compare("exclude.control.micro", row, "is_micro", "is_true", None, "资金体量为「微」"),
            Evidence(
                "exclude.control.top10",
                "top10_float_share",
                None,
                ex.control_top10_share_min,
                ">=",
                False,
                NOT_CONNECTED,
            ),
            compare(
                "exclude.control.low_turn_big_move",
                row,
                "low_turn_big_move_60d",
                ">=",
                float(ex.control_days_of_60),
                "近 60 日低换手大波动天数",
            ),
        ]
        crs = [
            compare(
                "exclude.crash.run_drawdown",
                row,
                "crash_events",
                ">=",
                float(ex.crash_events_min),
                f"近 {ex.crash_years} 年暴涨后深度回撤次数，最近 {row.get('crash_last') or '-'}",
            ),
            compare(
                "exclude.crash.blowup",
                row,
                "blowups",
                ">=",
                float(ex.blowup_min),
                f"近 {ex.crash_years} 年炸板 / 天地板次数，最近 {row.get('blowup_last') or '-'}",
            ),
            Evidence(
                "exclude.crash.holders",
                "holders_increase",
                None,
                ex.holders_increase,
                ">=",
                False,
                NOT_CONNECTED,
            ),
            Evidence(
                "exclude.crash.penalty", "penalty_count", None, 1.0, ">=", False, NOT_CONNECTED
            ),
            Evidence(
                "exclude.crash.reduction",
                "reduction_after_run",
                None,
                float(ex.reduction_min),
                ">=",
                False,
                NOT_CONNECTED,
            ),
        ]
        untr = [
            compare("exclude.untradable.st", row, "is_st", "is_false", None, "非 ST / *ST"),
            compare(
                "exclude.untradable.delisting", row, "is_delisting", "is_false", None, "非退市整理"
            ),
            compare(
                "exclude.untradable.amount",
                row,
                "amount_ma_20d",
                ">=",
                ex.tradable_min_avg_amount_20d,
                "近 20 日日均成交额",
            ),
        ]
        c_hit = any(e.hit for e in ctl)
        k_hit = sum(e.hit for e in crs) >= 2
        t_ok = all(e.hit for e in untr)
        control.append(c_hit)
        crash_hit.append(k_hit)
        tradable.append(t_ok)
        exclusions.append(
            to_json(
                {
                    "control": {"hit": c_hit, "items": _ev_dicts(ctl)},
                    "crash": {"hit": k_hit, "items": _ev_dicts(crs)},
                    "untradable": {"hit": not t_ok, "items": _ev_dicts(untr)},
                }
            )
        )
    return df.select("code", "amount_tier", "is_st").with_columns(
        identity=pl.Series(identity, dtype=pl.Utf8),
        exclusions=pl.Series(exclusions, dtype=pl.Utf8),
        tradable=pl.Series(tradable, dtype=pl.Boolean),
        control_risk=pl.Series(control, dtype=pl.Boolean),
        crash_risk=pl.Series(crash_hit, dtype=pl.Boolean),
    )
