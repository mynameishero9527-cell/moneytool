"""计算链 ⑥：动作名单（需求 8.3 / 8.4 / 8.8 / 8.9）与跟踪闭环。

名单存 action_list_*，`list_type`：
- buy / buy_invalid（已失效，保留到下一交易日收盘）
- sell / hold_watch（持有观察：相对强度仍为正、只是板块热度下降）
- point（买点 / 卖点条件，`point_type` 区分，tags 标「买点条件」/「卖点条件」）
- lowbase（低位企稳）

风控开启：不新增买入关注与低位企稳，已有条目保留并标「风控期间」；买点停止触发，卖点照常。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

import duckdb
import polars as pl

from moneytool.compute import quant as q
from moneytool.params import Params
from moneytool.rules.stock import (
    BUY_POINTS,
    POINT_LABEL_ZH,
    SELL_LABEL_ZH,
    SELL_POINTS,
    SELL_TYPES,
    buy_conditions,
    buy_point_rules,
    lowbase_rule,
    sell_point_rules,
    sell_rules,
)
from moneytool.storage.repo import to_json
from moneytool.types import Role, Stage

MAX_POINTS = 2
BUY_INVALIDATION = (
    "板块离开启动 / 扩散加速前半段",
    "所属一级或主导行业进入退潮",
    "个股强势失效",
    "出现背离出货或脉冲回吐",
    "触发位置约束（先行透支）",
)
LOWBASE_INVALIDATION = (
    "跌破近 20 日最低价并伴随主力净流出",
    "近 5 日主力转为净流出且留存转负",
)
BUY_LIST_TYPES = ("buy", "lowbase")


@dataclass
class HeldSets:
    """跟踪中（按名单类型）、自选、用户标记已买入的股票。"""

    tracking: (
        pl.DataFrame
    )  # list_type, code, sector_id, entered_date, entered_price, entered_stage, age
    watch: set[str] = field(default_factory=set)
    bought: set[str] = field(default_factory=set)

    def codes(self) -> set[str]:
        return set(self.tracking["code"].to_list()) | self.watch | self.bought


def add_list_inputs(
    rf: pl.DataFrame,
    profiles: pl.DataFrame,
    attribution: pl.DataFrame,
    p: Params,
) -> pl.DataFrame:
    """买入 / 低位企稳 / 卖出规则需要的组合列。"""
    b = p.buy
    df = rf.join(
        profiles.select("code", "tradable", "control_risk", "crash_risk", "amount_tier"),
        on="code",
        how="left",
    ).join(attribution.select("sector_id", "hype_score"), on="sector_id", how="left")
    by = "sector_id"
    follow_ret = pl.when(pl.col("role") == Role.FOLLOW.value).then(pl.col("retention_5d"))
    df = df.with_columns(
        _f_rank=follow_ret.rank(method="max", descending=True).over(by),
        _f_n=follow_ret.count().over(by),
        _mkt_ratio_pct=pl.col("main_ratio").rank(method="average") / pl.col("main_ratio").count(),
        is_hype=(pl.col("hype_score").fill_null(0) >= p.attribution.min_score),
    )
    multiple = (
        pl.when(pl.col("is_hype")).then(b.hype_position_multiple).otherwise(b.position_multiple)
    )
    df = df.with_columns(
        role_ok=(pl.col("role") == Role.CORE.value)
        | (
            (pl.col("role") == Role.FOLLOW.value)
            & (
                q.safe_div(pl.col("_f_rank").cast(pl.Float64), pl.col("_f_n").cast(pl.Float64))
                <= b.follow_top_share
            )
        ),
        pos_limit=multiple
        * pl.max_horizontal(pl.col("sector_ret_5d"), pl.lit(p.qualifiers.flat_abs_pct)),
        buy_tradable=pl.col("tradable").fill_null(False)
        & (pl.col("amount_ma_20d") >= b.min_avg_amount_20d).fill_null(False)
        & ~pl.col("is_consecutive_limit").fill_null(False)
        & ~pl.col("is_new_member")
        & ~pl.col("is_resumed").fill_null(False),
        excluded=pl.col("control_risk").fill_null(False) | pl.col("crash_risk").fill_null(False),
        limit_outflow=pl.col("is_limit_up").fill_null(False)
        & (pl.col("net_main") < 0).fill_null(False)
        & (pl.col("_mkt_ratio_pct") <= p.qualifiers.large_outflow_pct).fill_null(False),
    )
    df = df.with_columns(
        overdraft=pl.col("stage_entry").fill_null(False)
        & pl.col("is_strong")
        & (pl.col("ret_5d") > pl.col("pos_limit")).fill_null(False),
        lowbase_clean=~pl.col("role").is_in([Role.AVOID.value])
        & ~pl.col("excluded")
        & pl.col("tradable").fill_null(False)
        & ~pl.col("is_consecutive_limit").fill_null(False)
        & ~pl.col("is_new_member")
        & ~pl.col("is_resumed").fill_null(False)
        & ~pl.col("is_one_word").fill_null(False),
    )
    return df.drop([c for c in df.columns if c.startswith("_")])


def _key(row: dict[str, Any]) -> tuple[str, str]:
    return (row["code"], row["sector_id"])


def _entry(
    row: dict[str, Any],
    list_type: str,
    *,
    tags: list[str],
    evidence: dict[str, Any],
    invalidation: list[str] | tuple[str, ...] = (),
    point_type: str | None = None,
) -> dict[str, Any]:
    return {
        "code": row["code"],
        "sector_id": row["sector_id"],
        "list_type": list_type,
        "basis_level": row["basis_level"],
        "point_type": point_type,
        "tags": to_json(tags),
        "evidence": to_json(evidence),
        "invalidation": to_json(list(invalidation)),
        "_retention": row.get("retention_5d"),
        "_l1": row.get("upper_id") if row.get("basis_level") == "L2" else row["sector_id"],
        "_group": row.get("group"),
        "_high_risk": bool(row.get("high_risk")),
    }


def _common_tags(row: dict[str, Any], sector_names: dict[str, str]) -> list[str]:
    tags: list[str] = []
    if row.get("group") == "concept":
        tags.append(f"概念股：{sector_names.get(row['sector_id'], row['sector_id'])}")
    if row.get("is_hype"):
        tags.append("炒作倾向")
    if row.get("high_risk"):
        tags.append("高风险")
    return tags


def _stage_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sector_stage": row.get("stage"),
        "half": row.get("half"),
        "days_in_stage": row.get("days_in_stage"),
        "upper_id": row.get("upper_id"),
        "upper_stage": row.get("upper_stage"),
        "role": row.get("role"),
        "retention_5d": row.get("retention_5d"),
        "rs_5d_sector": row.get("rs_5d_sector"),
        "ratio_top": row.get("ratio_top"),
        "close": row.get("close"),
    }


def build_buy(
    df: pl.DataFrame,
    prev_buy: set[tuple[str, str]],
    gate: bool,
    sector_names: dict[str, str],
    p: Params,
) -> list[dict[str, Any]]:
    """8.3 买入关注 + 已失效。上限：每依据板块 3 只；行业组同一一级合计 6 只。"""
    cand = df.filter(pl.col("is_strong") | _in_keys(prev_buy))
    kept: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for row in cand.iter_rows(named=True):
        conds = buy_conditions(row, p)
        was = _key(row) in prev_buy
        failed = [k for k, e in conds.items() if not e.hit]
        tags = _common_tags(row, sector_names)
        evidence = {
            "conditions": {k: e.to_dict() for k, e in conds.items()},
            **_stage_evidence(row),
        }
        concept_parent_veto = failed == ["parent"] and row.get("group") == "concept"
        if not failed or (concept_parent_veto and was):
            if concept_parent_veto:
                tags.append("主导行业退潮：只保留已有")
            if gate and not was:
                continue
            if gate:
                tags.append("风控期间")
            kept.append(
                _entry(row, "buy", tags=tags, evidence=evidence, invalidation=BUY_INVALIDATION)
            )
        elif was:
            if row.get("overdraft"):
                tags.append("先行透支")
            reasons = [conds[k].note or conds[k].rule for k in failed]
            invalid.append(
                _entry(row, "buy_invalid", tags=tags, evidence=evidence, invalidation=reasons)
            )
    return _cap_buy(kept, p) + invalid


def _in_keys(keys: set[tuple[str, str]]) -> pl.Expr:
    codes = [f"{c}|{s}" for c, s in keys]
    return pl.concat_str([pl.col("code"), pl.col("sector_id")], separator="|").is_in(codes)


def _cap_buy(entries: list[dict[str, Any]], p: Params) -> list[dict[str, Any]]:
    """同组内先按风险（高风险排最后）再按留存降序截断。"""
    entries = sorted(
        entries,
        key=lambda e: (
            e["_high_risk"],
            -(e["_retention"] if e["_retention"] is not None else -1e30),
        ),
    )
    per_sector: dict[str, int] = {}
    per_l1: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    truncated_l1: set[str] = set()
    for e in entries:
        if per_sector.get(e["sector_id"], 0) >= p.buy.per_sector_max:
            continue
        if e["_group"] == "industry":
            l1 = e["_l1"] or e["sector_id"]
            if per_l1.get(l1, 0) >= p.buy.per_l1_max:
                truncated_l1.add(l1)
                continue
            per_l1[l1] = per_l1.get(l1, 0) + 1
        per_sector[e["sector_id"]] = per_sector.get(e["sector_id"], 0) + 1
        out.append(e)
    for e in out:
        if e["_group"] == "industry" and (e["_l1"] or e["sector_id"]) in truncated_l1:
            tags = [*_loads(e["tags"]), "一级已截断"]
            e["tags"] = to_json(tags)
    return out


def _loads(s: str) -> list[str]:
    v = json.loads(s)
    return list(v) if isinstance(v, list) else []


def build_sell(
    df: pl.DataFrame,
    held: HeldSets,
    prev_strong: set[tuple[str, str]],
    sector_names: dict[str, str],
    p: Params,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """8.4 卖出关注（跟踪 / 自选 / 已买入 / 昨日强势），以及持有观察。返回 (条目, 命中卖出的键)。"""
    held_codes = held.codes()
    track = {(r["code"], r["sector_id"]): r for r in held.tracking.iter_rows(named=True)}
    cand = df.filter(pl.col("code").is_in(list(held_codes)) | _in_keys(prev_strong))
    out: list[dict[str, Any]] = []
    hit_keys: set[tuple[str, str]] = set()
    for row in cand.iter_rows(named=True):
        rules = sell_rules(row, p)
        types = [t for t in SELL_TYPES if rules[t].hit]
        tags = _common_tags(row, sector_names)
        evidence: dict[str, Any] = {
            "types": {t: rules[t].to_dicts() for t in SELL_TYPES},
            **_stage_evidence(row),
        }
        tr = track.get(_key(row))
        if tr is not None:
            tags.append("跟踪中")
            evidence["tracking"] = {
                "entered_date": tr["entered_date"],
                "entered_stage": tr["entered_stage"],
                "entered_price": tr["entered_price"],
            }
        if types:
            hit_keys.add(_key(row))
            tags.extend(SELL_LABEL_ZH[t] for t in types)
            tags.append("结构已变化，优先复核是否卖出")
            out.append({**_entry(row, "sell", tags=tags, evidence=evidence), "_types": types})
        elif (
            row["code"] in held_codes
            and (row.get("rs_5d_sector") or 0) > 0
            and row.get("stage")
            in (
                Stage.CLIMAX.value,
                Stage.DIVERGE.value,
            )
        ):
            tags.append("持有观察")
            out.append(_entry(row, "hold_watch", tags=tags, evidence=evidence))
    return out, hit_keys


def build_points(
    df: pl.DataFrame,
    buy_keys: set[tuple[str, str]],
    lowbase_keys: set[tuple[str, str]],
    held: HeldSets,
    gate: bool,
    p: Params,
) -> list[dict[str, Any]]:
    """8.8 买点条件（买入关注 / 低位企稳名单内，最多 2 种）与卖点条件（跟踪 / 自选 / 已买入）。"""
    out: list[dict[str, Any]] = []
    held_codes = held.codes()
    track = {(r["code"], r["sector_id"]): r for r in held.tracking.iter_rows(named=True)}
    keys = buy_keys | lowbase_keys
    cand = df.filter(pl.col("code").is_in(list(held_codes)) | _in_keys(keys))
    for row in cand.iter_rows(named=True):
        k = _key(row)
        base_ev = _stage_evidence(row)
        if k in keys and not gate:
            rules = buy_point_rules(row, p)
            hits = [t for t in BUY_POINTS if rules[t].hit]
            if k in lowbase_keys:
                hits.append("low_base")
            for t in hits[:MAX_POINTS]:
                ev = {"rule": rules[t].to_dicts() if t in rules else [], **base_ev}
                out.append(
                    _entry(
                        row,
                        "point",
                        point_type=t,
                        tags=["买点条件", POINT_LABEL_ZH[t]],
                        evidence=ev,
                    )
                )
        if row["code"] in held_codes:
            rules = sell_point_rules(row, p)
            hits = [t for t in SELL_POINTS if rules[t].hit]
            tr = track.get(k)
            limit = (
                p.lowbase.tracking_days
                if tr and tr["list_type"] == "lowbase"
                else p.buy.tracking_days
            )
            if (
                tr is not None
                and (tr.get("age") or 0) >= limit
                and (row.get("rs_5d_sector") or 0) < 0
            ):
                hits.append("expire")
            for t in hits:
                ev = {"rule": rules[t].to_dicts() if t in rules else [], **base_ev}
                out.append(
                    _entry(
                        row,
                        "point",
                        point_type=t,
                        tags=["卖点条件", POINT_LABEL_ZH[t]],
                        evidence=ev,
                    )
                )
    return out


def build_lowbase(
    df: pl.DataFrame,
    prev_lowbase: set[tuple[str, str]],
    gate: bool,
    sector_names: dict[str, str],
    p: Params,
) -> list[dict[str, Any]]:
    """8.9 低位企稳：行业依据板块；每个一级最多 3 只，按留存排序。"""
    ind = df.filter(pl.col("group") == "industry")
    kept: list[dict[str, Any]] = []
    for row in ind.iter_rows(named=True):
        res = lowbase_rule(row, p)
        if not res.hit:
            continue
        was = _key(row) in prev_lowbase
        if gate and not was:
            continue
        tags = _common_tags(row, sector_names) + ["低位企稳"] + (["风控期间"] if gate else [])
        kept.append(
            _entry(
                row,
                "lowbase",
                tags=tags,
                evidence={"conditions": res.to_dicts(), **_stage_evidence(row)},
                invalidation=LOWBASE_INVALIDATION,
            )
        )
    kept.sort(key=lambda e: -(e["_retention"] if e["_retention"] is not None else -1e30))
    per_l1: dict[str, int] = {}
    out = []
    for e in kept:
        l1 = e["_l1"] or e["sector_id"]
        if per_l1.get(l1, 0) >= p.lowbase.per_l1_max:
            continue
        per_l1[l1] = per_l1.get(l1, 0) + 1
        out.append(e)
    return out


def entries_frame(
    entries: list[dict[str, Any]], day: dt.date, version: str, segment: str | None
) -> pl.DataFrame:
    schema = {
        "code": pl.Utf8,
        "sector_id": pl.Utf8,
        "trade_date": pl.Date,
        "list_type": pl.Utf8,
        "param_version": pl.Utf8,
        "basis_level": pl.Utf8,
        "point_type": pl.Utf8,
        "tags": pl.Utf8,
        "evidence": pl.Utf8,
        "invalidation": pl.Utf8,
    }
    if segment is not None:
        schema = {**schema, "segment": pl.Utf8}
    rows = [
        {
            **{k: v for k, v in e.items() if not k.startswith("_")},
            "trade_date": day,
            "param_version": version,
            **({"segment": segment} if segment is not None else {}),
        }
        for e in entries
    ]
    df = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
    return df.unique(subset=["code", "sector_id", "list_type", "point_type"], keep="first")


# ---------- 跟踪闭环（仅收盘确认） ----------


def load_held(conn: duckdb.DuckDBPyConnection, day: dt.date, version: str) -> HeldSets:
    """截至上一交易日仍在跟踪的条目（age = 入选至今交易日数）、自选、最新标记为已买入的股票。"""
    tracking = conn.execute(
        """
        SELECT t.list_type, t.code, t.sector_id, t.entered_date, t.entered_price, t.entered_stage,
               (SELECT count(*) FROM trade_calendar c WHERE c.is_open AND c.trade_date > t.entered_date
                AND c.trade_date <= ?) AS age
        FROM tracking t
        WHERE t.param_version = ? AND t.entered_date < ? AND (t.exited_date IS NULL OR t.exited_date >= ?)
        """,
        [day, version, day, day],
    ).pl()
    watch = {r[0] for r in conn.execute("SELECT DISTINCT code FROM watchlist").fetchall()}
    bought = {
        r[0]
        for r in conn.execute(
            "SELECT code FROM (SELECT code, mark, row_number() OVER (PARTITION BY code ORDER BY marked_at DESC) rn "
            "FROM user_mark) WHERE rn = 1 AND mark = 'bought'"
        ).fetchall()
    }
    return HeldSets(tracking, watch, bought)


def update_tracking(
    conn: duckdb.DuckDBPyConnection,
    day: dt.date,
    version: str,
    new_entries: pl.DataFrame,
    sell_keys: set[tuple[str, str]],
    held: HeldSets,
    stage_close: pl.DataFrame,
    p: Params,
) -> dict[str, int]:
    """重算安全：先撤销 ≥ 当日的入选与退出，再按当日名单重放。`stage_close (code, sector_id, close, stage)`。"""
    conn.execute(
        "DELETE FROM tracking WHERE param_version = ? AND entered_date >= ?", [version, day]
    )
    conn.execute(
        "UPDATE tracking SET exited_date = NULL, exit_reason = NULL WHERE param_version = ? AND exited_date >= ?",
        [version, day],
    )
    exited = 0
    for r in held.tracking.iter_rows(named=True):
        key = (r["code"], r["sector_id"])
        limit = p.lowbase.tracking_days if r["list_type"] == "lowbase" else p.buy.tracking_days
        reason = "sell" if key in sell_keys else ("expired" if (r["age"] or 0) >= limit else None)
        if reason:
            conn.execute(
                "UPDATE tracking SET exited_date = ?, exit_reason = ? WHERE list_type = ? AND code = ? "
                "AND sector_id = ? AND entered_date = ? AND param_version = ?",
                [
                    day,
                    reason,
                    r["list_type"],
                    r["code"],
                    r["sector_id"],
                    r["entered_date"],
                    version,
                ],
            )
            exited += 1
    open_keys = {
        (r["list_type"], r["code"], r["sector_id"]) for r in held.tracking.iter_rows(named=True)
    }
    fresh = new_entries.filter(pl.col("list_type").is_in(list(BUY_LIST_TYPES))).join(
        stage_close, on=["code", "sector_id"], how="left"
    )
    added = 0
    for r in fresh.iter_rows(named=True):
        if (r["list_type"], r["code"], r["sector_id"]) in open_keys:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO tracking (list_type, code, sector_id, entered_date, param_version, "
            "entered_price, entered_stage) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                r["list_type"],
                r["code"],
                r["sector_id"],
                day,
                version,
                r.get("close"),
                r.get("stage"),
            ],
        )
        added += 1
    return {"added": added, "exited": exited}
