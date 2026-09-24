"""计算链 ⑤–⑦ 编排：归因 → 角色 → 画像 → 指数 → 名单 → 评分 → 持有评估 → 提示 → 存档。

由 `pipeline.run` 在板块阶段与市场风控之后调用；收盘写 *_confirmed 与跟踪闭环，盘中写 *_intraday。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

import duckdb
import polars as pl

from moneytool.compute.actions import (
    add_list_inputs,
    build_buy,
    build_lowbase,
    build_points,
    build_sell,
    entries_frame,
    load_held,
    update_tracking,
)
from moneytool.compute.attribution import compute_attribution
from moneytool.compute.hints import hint_rows, hold_eval, hold_text, sector_hint, stock_hint
from moneytool.compute.indices import (
    RETAIL_ITEMS,
    RETAIL_TIERS,
    RISK_TIERS,
    SECTOR_RISK_ITEMS,
    SECTOR_SENTIMENT_ITEMS,
    SENTIMENT_TIERS,
    STOCK_RISK_ITEMS,
    index_rows,
    retail_pressure,
    sector_risk,
    sector_sentiment,
    stock_risk,
)
from moneytool.compute.profile import (
    AMOUNT_TIER_ZH,
    build_profiles,
    crash_history,
    load_index_membership,
)
from moneytool.compute.roles import (
    basis_pairs,
    compute_scores,
    evaluate_roles,
    role_frame,
    sector_context,
)
from moneytool.logging import get_logger
from moneytool.params import Params
from moneytool.storage.repo import upsert
from moneytool.types import Role, SectorLevel

log = get_logger(__name__)


@dataclass
class ChainResult:
    stages: pl.DataFrame
    counts: dict[str, int] = field(default_factory=dict)


def dominant_l1(members: pl.DataFrame, stock_today: pl.DataFrame) -> pl.DataFrame:
    """概念主导行业：成分股近 20 日成交额占比最高的申万一级（需求 8.3）。"""
    concept = members.filter(pl.col("level") == SectorLevel.CONCEPT.value).select(
        "sector_id", "code"
    )
    l1 = members.filter(pl.col("level") == SectorLevel.L1.value).select(
        pl.col("sector_id").alias("l1_id"), "code"
    )
    if concept.is_empty() or l1.is_empty():
        return pl.DataFrame(schema={"sector_id": pl.Utf8, "dominant_l1": pl.Utf8})
    amt = stock_today.select("code", "amount_ma_20d")
    agg = (
        concept.join(l1, on="code", how="inner")
        .join(amt, on="code", how="left")
        .group_by("sector_id", "l1_id")
        .agg(_amt=pl.col("amount_ma_20d").sum())
        .sort("sector_id", "_amt", "l1_id", descending=[False, True, False])
    )
    return (
        agg.group_by("sector_id", maintain_order=True)
        .first()
        .select("sector_id", pl.col("l1_id").alias("dominant_l1"))
    )


def _prev_confirmed_date(
    conn: duckdb.DuckDBPyConnection, table: str, day: dt.date, version: str
) -> dt.date | None:
    row = conn.execute(
        f"SELECT max(trade_date) FROM {table} WHERE trade_date < ? AND param_version = ?",
        [day, version],
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _keys(conn: duckdb.DuckDBPyConnection, sql: str, args: list[Any]) -> set[tuple[str, str]]:
    return {(str(r[0]), str(r[1])) for r in conn.execute(sql, args).fetchall()}


def _nth_open_before(conn: duckdb.DuckDBPyConnection, day: dt.date, n: int) -> dt.date | None:
    row = conn.execute(
        "SELECT trade_date FROM trade_calendar WHERE is_open AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT 1 OFFSET ?",
        [day, max(n - 1, 0)],
    ).fetchone()
    return row[0] if row else None


def _clear(
    conn: duckdb.DuckDBPyConnection, table: str, day: dt.date, version: str, segment: str | None
) -> None:
    clause = " AND segment = ?" if segment is not None else ""
    conn.execute(
        f"DELETE FROM {table} WHERE trade_date = ? AND param_version = ?{clause}",
        [day, version, *([segment] if segment is not None else [])],
    )


def run_chain(
    conn: duckdb.DuckDBPyConnection,
    p: Params,
    day: dt.date,
    segment: str | None,
    *,
    stock_feat: pl.DataFrame,
    sector_feat: pl.DataFrame,
    stages: pl.DataFrame,
    sector_meta: pl.DataFrame,
    members: pl.DataFrame,
    security: pl.DataFrame,
    eqw_ret_5d: float | None,
    gate: bool,
) -> ChainResult:
    intraday = segment is not None
    version = p.version
    counts: dict[str, int] = {}
    stock_today = stock_feat.filter(pl.col("trade_date") == day)
    sector_today = sector_feat.filter(pl.col("trade_date") == day)
    if stock_today.is_empty() or sector_today.is_empty():
        return ChainResult(stages, counts)

    # 概念主导行业
    dom = dominant_l1(members, stock_today)
    if not dom.is_empty():
        sector_meta = (
            sector_meta.join(dom.rename({"dominant_l1": "_dom"}), on="sector_id", how="left")
            .with_columns(dominant_l1=pl.coalesce("_dom", "dominant_l1"))
            .drop("_dom")
        )
        if not intraday:
            conn.register("_dom", dom)
            try:
                conn.execute(
                    "UPDATE sector SET dominant_l1 = d.dominant_l1 FROM _dom d WHERE sector.sector_id = d.sector_id"
                )
            finally:
                conn.unregister("_dom")
    names = dict(
        zip(sector_meta["sector_id"].to_list(), sector_meta["name"].to_list(), strict=True)
    )

    # 归因（7.5），写回阶段行
    ctx = sector_context(sector_today, stages, sector_meta)
    attribution = compute_attribution(sector_today, ctx, p)
    stages = stages.drop("attribution").join(
        attribution.select("sector_id", "attribution"), on="sector_id", how="left"
    )
    ctx = ctx.drop("attribution").join(
        attribution.select("sector_id", "hype_score"), on="sector_id", how="left"
    )

    # ⑤ 角色
    prev_role_day = _prev_confirmed_date(conn, "stock_role_confirmed", day, version)
    prev_roles = (
        conn.execute(
            "SELECT code, sector_id, role FROM stock_role_confirmed WHERE trade_date = ? AND param_version = ?",
            [prev_role_day, version],
        ).pl()
        if prev_role_day
        else None
    )
    pairs = basis_pairs(members, sector_meta, sector_today, p)
    cutoff = _nth_open_before(conn, day, p.role.new_member_days)
    rf = role_frame(pairs, members, stock_today, ctx, eqw_ret_5d, cutoff, p)
    if rf.is_empty():
        return ChainResult(stages, counts)
    rf = evaluate_roles(rf, prev_roles, p, intraday=intraday)

    # 画像（8.6 / 8.10）
    concept_names = (
        members.filter(pl.col("level") == SectorLevel.CONCEPT.value)
        .join(sector_meta.select("sector_id", "name"), on="sector_id", how="left")
        .group_by("code")
        .agg(concept_names=pl.col("name").sort())
    )
    profiles = build_profiles(
        stock_today,
        security,
        concept_names,
        load_index_membership(conn, day),
        crash_history(conn, day, p),
        p,
    )
    df = add_list_inputs(rf, profiles, attribution, p)

    # 指数（7.7 / 8.11）
    sentiment = sector_sentiment(sector_feat, day, p)
    s_risk = sector_risk(sector_feat, day, stages, sentiment, attribution, p)
    industry = df.filter(pl.col("group") == "industry")
    risk_inputs = (
        industry.select(
            "code",
            pl.col("avoid_diverge_out").alias("diverge_out"),
            "overdraft",
            "amount_tier",
            "control_risk",
            "crash_risk",
            "sector_id",
        )
        .join(
            s_risk.select("sector_id", pl.col("total").alias("basis_sector_risk")),
            on="sector_id",
            how="left",
        )
        .drop("sector_id")
    )
    k_risk = stock_risk(stock_feat, day, risk_inputs, p)
    retail = retail_pressure(stock_feat, day, p)
    df = df.join(
        k_risk.select("code", pl.col("total").alias("risk_total")), on="code", how="left"
    ).with_columns(high_risk=(pl.col("risk_total") >= p.indices.broken_risk_min).fill_null(False))

    # ⑥ 名单
    prev_list_day = _prev_confirmed_date(conn, "action_list_confirmed", day, version)
    prev_buy: set[tuple[str, str]] = set()
    prev_low: set[tuple[str, str]] = set()
    if prev_list_day:
        q = "SELECT code, sector_id FROM action_list_confirmed WHERE trade_date = ? AND param_version = ? AND list_type = ?"
        prev_buy = _keys(conn, q, [prev_list_day, version, "buy"])
        prev_low = _keys(conn, q, [prev_list_day, version, "lowbase"])
    prev_strong: set[tuple[str, str]] = set()
    if prev_roles is not None:
        prev_strong = {
            (r["code"], r["sector_id"])
            for r in prev_roles.filter(
                pl.col("role").is_in([Role.CORE.value, Role.FOLLOW.value])
            ).iter_rows(named=True)
        }
    held = load_held(conn, day, version)
    buy = build_buy(df, prev_buy, gate, names, p)
    low = build_lowbase(df, prev_low, gate, names, p)
    sell, sell_keys = build_sell(df, held, prev_strong, names, p)
    buy_keys = {(e["code"], e["sector_id"]) for e in buy if e["list_type"] == "buy"}
    low_keys = {(e["code"], e["sector_id"]) for e in low}
    points = build_points(df, buy_keys, low_keys, held, gate, p)
    lists = entries_frame(buy + low + sell + points, day, version, segment)

    # 评分（8.7）
    scores = compute_scores(industry, profiles, p)

    # 存档
    role_table = "stock_role_intraday" if intraday else "stock_role_confirmed"
    list_table = "action_list_intraday" if intraday else "action_list_confirmed"
    for t in (role_table, list_table):
        _clear(conn, t, day, version, segment)
    for t in ("stock_profile", "index_daily", "hint"):
        _clear_segment(conn, t, day, version, segment or "close")

    role_rows = df.filter(
        (pl.col("group") == "industry")
        | (pl.col("role") != Role.OTHER.value)
        | pl.col("strong_invalid")
        | pl.col("is_consecutive_limit").fill_null(False)
        | pl.col("is_new_member")
    ).join(
        scores.select("code", "score", "score_components", pl.lit("industry").alias("group")),
        on=["code", "group"],
        how="left",
    )
    role_out = role_rows.select(
        "code",
        "sector_id",
        pl.lit(day).alias("trade_date"),
        *([pl.lit(segment).alias("segment")] if intraday else []),
        pl.lit(version).alias("param_version"),
        "role",
        "tags",
        "score",
        "score_components",
        pl.col("role_evidence").alias("evidence"),
    )
    counts["roles"] = upsert(conn, role_table, role_out)
    counts["lists"] = upsert(conn, list_table, lists)

    basis = industry.select("code", pl.col("sector_id").alias("basis_sector"))
    prof_out = (
        profiles.join(basis, on="code", how="left")
        .join(scores, on="code", how="left")
        .select(
            "code",
            pl.lit(day).alias("trade_date"),
            pl.lit(segment or "close").alias("segment"),
            pl.lit(version).alias("param_version"),
            "basis_sector",
            "identity",
            "exclusions",
            "tradable",
            "score",
            "score_tier",
            "score_components",
        )
    )
    counts["profiles"] = upsert(conn, "stock_profile", prof_out)

    idx = pl.concat(
        [
            index_rows(
                sentiment,
                id_col="sector_id",
                subject_type="sector",
                index_name="sector_sentiment",
                items=SECTOR_SENTIMENT_ITEMS,
                tiers=SENTIMENT_TIERS,
                day=day,
                segment=segment,
                version=version,
            ),
            index_rows(
                s_risk,
                id_col="sector_id",
                subject_type="sector",
                index_name="sector_risk",
                items=SECTOR_RISK_ITEMS,
                tiers=RISK_TIERS,
                day=day,
                segment=segment,
                version=version,
            ),
            index_rows(
                k_risk,
                id_col="code",
                subject_type="stock",
                index_name="stock_risk",
                items=STOCK_RISK_ITEMS,
                tiers=RISK_TIERS,
                day=day,
                segment=segment,
                version=version,
            ),
            index_rows(
                retail,
                id_col="code",
                subject_type="stock",
                index_name="retail_pressure",
                items=RETAIL_ITEMS,
                tiers=RETAIL_TIERS,
                day=day,
                segment=segment,
                version=version,
                degraded=True,
                notes={"holders": "股东户数数据未接入，其余四项按 5/4 折算"},
            ),
        ]
    )
    counts["indices"] = upsert(conn, "index_daily", idx)

    # 持有结构评估 + 提示
    sell_types: dict[str, list[str]] = {}
    for e in sell:
        sell_types.setdefault(e["code"], []).extend(e.get("_types", []))
    point_by_code: dict[str, list[str]] = {}
    sell_points: dict[str, list[str]] = {}
    for e in points:
        point_by_code.setdefault(e["code"], []).append(e["point_type"])
        if "卖点条件" in json.loads(e["tags"]) and e["point_type"] != "expire":
            sell_points.setdefault(e["code"], []).append(e["point_type"])
    actions_by_code: dict[str, set[str]] = {}
    for e in buy + low + sell:
        actions_by_code.setdefault(e["code"], set()).add(e["list_type"])

    risk_items = {r["code"]: r for r in k_risk.iter_rows(named=True)}
    retail_total = dict(zip(retail["code"].to_list(), retail["total"].to_list(), strict=True))
    prof_map = {r["code"]: r for r in profiles.iter_rows(named=True)}
    sec_names = dict(zip(security["code"].to_list(), security["name"].to_list(), strict=True))
    prev_hold = _prev_hold(conn, day, version)
    held_codes = held.codes()
    hold_rows: list[dict[str, Any]] = []
    hints: list[tuple[str, str, str, dict[str, Any]]] = []
    for row in industry.join(
        k_risk.select("code", pl.col("total").alias("_rt")), on="code", how="left"
    ).iter_rows(named=True):
        code = row["code"]
        row["risk_total"] = row.pop("_rt")
        row["retail_total"] = retail_total.get(code)
        row["sell_points"] = sell_points.get(code, [])
        ev, items = hold_eval(row, p, gate)
        if code in held_codes and not intraday:
            hold_rows.append(
                {
                    "code": code,
                    "trade_date": day,
                    "param_version": version,
                    "eval": ev,
                    "changed_from": prev_hold.get(code)
                    if prev_hold.get(code) not in (None, ev)
                    else None,
                    "evidence": json.dumps(
                        {"text": hold_text(ev, items), "items": items}, ensure_ascii=False
                    ),
                }
            )
        prof = prof_map.get(code, {})
        identity = json.loads(prof.get("identity") or "{}")
        risk_row = risk_items.get(code, {})
        payload = stock_hint(
            {
                "code": code,
                "name": sec_names.get(code),
                "sector_id": row["sector_id"],
                "stage": row.get("stage"),
                "half": row.get("half"),
                "role": row.get("role"),
                "hold_eval": ev,
                "actions": sorted(actions_by_code.get(code, set())),
                "sell_types": sorted(set(sell_types.get(code, []))),
                "points": point_by_code.get(code, []),
                "risk_total": row["risk_total"],
                "risk_items": {k: risk_row.get(k) for k in STOCK_RISK_ITEMS},
                "retail_total": row["retail_total"],
                "control_risk": prof.get("control_risk"),
                "crash_risk": prof.get("crash_risk"),
                "amount_tier_zh": AMOUNT_TIER_ZH[prof["amount_tier"]]
                if prof.get("amount_tier") is not None
                else None,
                "indices": identity.get("indices"),
            },
            names,
        )
        payload["hold_text"] = hold_text(ev, items)
        hints.append(("stock", code, ev, payload))
    if hold_rows:
        counts["hold_eval"] = upsert(conn, "hold_eval", pl.DataFrame(hold_rows))

    sector_rows = (
        sector_today.select(
            "sector_id", "price_flow_agree", "inflow_streak", "breadth", "concentration_top5"
        )
        .join(
            stages.select("sector_id", "stage", "half", "days_in_stage"), on="sector_id", how="left"
        )
        .join(
            ctx.select("sector_id", "upper_id", "upper_stage", "hype_score"),
            on="sector_id",
            how="left",
        )
        .join(
            s_risk.select("sector_id", pl.col("total").alias("risk_total")),
            on="sector_id",
            how="left",
        )
    )
    for row in sector_rows.iter_rows(named=True):
        row["name"] = names.get(row["sector_id"])
        payload = sector_hint(row, p, gate, names)
        hints.append(("sector", row["sector_id"], payload["tier"], payload))
    counts["hints"] = upsert(conn, "hint", hint_rows(hints, day, segment, version))

    if not intraday:
        stage_close = df.select("code", "sector_id", "close", "stage").unique(["code", "sector_id"])
        counts.update(update_tracking(conn, day, version, lists, sell_keys, held, stage_close, p))
    log.info("chain_done", trade_date=day.isoformat(), segment=segment, **counts)
    return ChainResult(stages, counts)


def _clear_segment(
    conn: duckdb.DuckDBPyConnection, table: str, day: dt.date, version: str, segment: str
) -> None:
    conn.execute(
        f"DELETE FROM {table} WHERE trade_date = ? AND param_version = ? AND segment = ?",
        [day, version, segment],
    )


def _prev_hold(conn: duckdb.DuckDBPyConnection, day: dt.date, version: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT code, eval FROM hold_eval
        WHERE param_version = ? AND trade_date = (
            SELECT max(trade_date) FROM hold_eval WHERE trade_date < ? AND param_version = ?)
        """,
        [version, day, version],
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}
