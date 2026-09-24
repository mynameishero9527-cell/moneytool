"""需求 9.7.1 标签事后统计：名单 / 买卖点 / 持有评估 / 用户已买入 在命中后 N 个交易日的走势。

只统计命中之后的走势分布，不是策略收益：收益用复权收盘价，超额相对全 A 等权与依据板块（成分等权），
最大回撤取区间内最低复权收盘价。只补算尚未算过且窗口已走完的样本，结果按参数版本分开。
"""

from __future__ import annotations

import datetime as dt

import duckdb

from moneytool.params import Params

SOURCES = {
    # 名单类型 → 取样本的 SQL（列：list_type, point_type, code, sector_id, entered_date, entered_stage）
    "buy": """
        SELECT 'buy' AS list_type, NULL AS point_type, code, sector_id, entered_date, entered_stage
        FROM tracking WHERE param_version = $v AND list_type = 'buy'
    """,
    "lowbase": """
        SELECT 'lowbase', NULL, code, sector_id, entered_date, entered_stage
        FROM tracking WHERE param_version = $v AND list_type = 'lowbase'
    """,
    "point": """
        SELECT 'point:' || a.point_type, a.point_type, a.code, a.sector_id, a.trade_date, s.stage
        FROM action_list_confirmed a
        LEFT JOIN sector_stage_confirmed s ON s.sector_id = a.sector_id AND s.trade_date = a.trade_date
             AND s.param_version = a.param_version
        WHERE a.param_version = $v AND a.list_type = 'point'
    """,
    "sell": """
        SELECT 'sell', NULL, a.code, a.sector_id, a.trade_date, s.stage
        FROM action_list_confirmed a
        LEFT JOIN sector_stage_confirmed s ON s.sector_id = a.sector_id AND s.trade_date = a.trade_date
             AND s.param_version = a.param_version
        WHERE a.param_version = $v AND a.list_type = 'sell'
    """,
    "hold": """
        SELECT 'hold_' || h.eval, NULL, h.code, coalesce(p.basis_sector, ''), h.trade_date, NULL
        FROM hold_eval h
        LEFT JOIN stock_profile p ON p.code = h.code AND p.trade_date = h.trade_date
             AND p.segment = 'close' AND p.param_version = h.param_version
        WHERE h.param_version = $v
    """,
    "bought": """
        SELECT 'bought', NULL, m.code, coalesce(p.basis_sector, ''), c.trade_date, NULL
        FROM (SELECT code, CAST(marked_at AS DATE) AS d FROM user_mark WHERE mark = 'bought') m
        JOIN LATERAL (
            SELECT max(trade_date) AS trade_date FROM trade_calendar WHERE is_open AND trade_date <= m.d
        ) c ON TRUE
        LEFT JOIN stock_profile p ON p.code = m.code AND p.trade_date = c.trade_date
             AND p.segment = 'close' AND p.param_version = $v
    """,
}


def compute_labels(conn: duckdb.DuckDBPyConnection, day: dt.date, p: Params) -> int:
    """补算截至 `day` 已走完窗口的样本，返回写入行数。"""
    total = 0
    for key, sql in SOURCES.items():
        horizons = p.labels.horizons.get(key, ())
        if not horizons:
            continue
        before = conn.execute("SELECT count(*) FROM label_outcome").fetchone()
        conn.execute(
            f"""
            INSERT OR REPLACE INTO label_outcome
                (list_type, code, sector_id, entered_date, horizon, param_version, point_type, entered_stage,
                 ret, excess_vs_sector, excess_vs_eqw, max_drawdown, risk_gate)
            WITH cal AS (
                SELECT trade_date, row_number() OVER (ORDER BY trade_date) AS idx
                FROM trade_calendar WHERE is_open AND trade_date <= $day
            ),
            src AS (SELECT DISTINCT * FROM ({sql}) t(list_type, point_type, code, sector_id, entered_date, entered_stage)),
            e AS (
                SELECT src.*, h.horizon, c1.trade_date AS end_date
                FROM src
                CROSS JOIN (SELECT unnest($h) AS horizon) h
                JOIN cal c0 ON c0.trade_date = src.entered_date
                JOIN cal c1 ON c1.idx = c0.idx + h.horizon
                ANTI JOIN label_outcome l ON l.list_type = src.list_type AND l.code = src.code
                    AND l.sector_id = src.sector_id AND l.entered_date = src.entered_date
                    AND l.horizon = h.horizon AND l.param_version = $v
            ),
            px AS (
                SELECT e.*,
                    (SELECT close * coalesce(adj_factor, 1) FROM bar_daily b
                      WHERE b.code = e.code AND b.trade_date = e.entered_date) AS p0,
                    (SELECT close * coalesce(adj_factor, 1) FROM bar_daily b
                      WHERE b.code = e.code AND b.trade_date = e.end_date) AS p1,
                    (SELECT min(close * coalesce(adj_factor, 1)) FROM bar_daily b
                      WHERE b.code = e.code AND b.trade_date > e.entered_date AND b.trade_date <= e.end_date) AS pmin,
                    (SELECT exp(sum(ln(1 + eqw_ret))) - 1 FROM market_daily m
                      WHERE m.segment = 'close' AND m.param_version = $v
                        AND m.trade_date > e.entered_date AND m.trade_date <= e.end_date) AS eqw_ret,
                    (SELECT exp(sum(ln(1 + TRY_CAST(json_extract(f.features, '$.sector_pct_chg') AS DOUBLE)))) - 1
                       FROM feature_daily f
                      WHERE f.subject_type = 'sector' AND f.subject_id = e.sector_id AND f.segment = 'close'
                        AND f.param_version = $v AND f.trade_date > e.entered_date
                        AND f.trade_date <= e.end_date) AS sector_ret,
                    (SELECT risk_gate FROM market_daily m WHERE m.trade_date = e.entered_date
                        AND m.segment = 'close' AND m.param_version = $v) AS gate
                FROM e
            )
            SELECT list_type, code, sector_id, entered_date, horizon, $v, point_type, entered_stage,
                   p1 / NULLIF(p0, 0) - 1,
                   p1 / NULLIF(p0, 0) - 1 - sector_ret,
                   p1 / NULLIF(p0, 0) - 1 - eqw_ret,
                   pmin / NULLIF(p0, 0) - 1,
                   gate
            FROM px WHERE p0 IS NOT NULL AND p1 IS NOT NULL
            """,
            {"day": day, "v": p.version, "h": list(horizons)},
        )
        after = conn.execute("SELECT count(*) FROM label_outcome").fetchone()
        total += int(after[0] if after else 0) - int(before[0] if before else 0)
    return total
