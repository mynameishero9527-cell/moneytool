"""参考数据同步：日历、证券、申万一级 / 二级与成分快照、指数成分、概念列表与成分（架构 11 节第 2 步）。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import duckdb
import polars as pl

from moneytool.adapters.base import AdapterError
from moneytool.adapters.csindex import INDEX_SYMBOLS
from moneytool.adapters.registry import Adapters
from moneytool.logging import get_logger
from moneytool.storage.repo import record_quality, upsert
from moneytool.types import QualityStatus, SectorLevel

log = get_logger(__name__)


def pinyin_initials(name: str) -> str:
    """需求 10 节：拼音首字母，如 中国平安 → ZGPA。非汉字原样保留字母数字。"""
    try:
        from pypinyin import Style, lazy_pinyin  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return ""
    # FIRST_LETTER 下汉字已是单字母；非汉字片段原样保留（TCL科技 → TCLKJ）
    parts = lazy_pinyin(name, style=Style.FIRST_LETTER, errors=lambda s: str(s))
    return "".join(c for c in "".join(str(p) for p in parts) if c.isalnum()).upper()


def sync_calendar(
    conn: duckdb.DuckDBPyConnection, ad: Adapters, start: dt.date, end: dt.date, day: dt.date
) -> int:
    df = ad.baostock.trade_dates(start, end, day)
    return upsert(conn, "trade_calendar", df.with_columns(source=pl.lit("baostock")))


def sync_securities(conn: duckdb.DuckDBPyConnection, ad: Adapters, day: dt.date) -> int:
    """证券表：Baostock 基础信息（上市 / 退市日）。ST 与交易板块由代码与名称推断，东财快照可用时补名称。"""
    basic = ad.baostock.stock_basic(day).filter(pl.col("is_stock"))
    df = basic.select(
        "code",
        "name",
        pl.col("code").str.slice(-2).alias("exchange"),
        pl.when(pl.col("code").str.starts_with("688"))
        .then(pl.lit("科创板"))
        .when(pl.col("code").str.starts_with("30"))
        .then(pl.lit("创业板"))
        .when(pl.col("code").str.ends_with(".BJ"))
        .then(pl.lit("北交所"))
        .when(pl.col("code").str.ends_with(".SH"))
        .then(pl.lit("沪市主板"))
        .otherwise(pl.lit("深市主板"))
        .alias("board"),
        "list_date",
        pl.col("name").str.contains("ST").alias("is_st"),
        (pl.col("delist_date").is_not_null() | pl.col("name").str.contains("退")).alias(
            "is_delisting"
        ),
        pl.col("name").map_elements(pinyin_initials, return_dtype=pl.Utf8).alias("pinyin_initials"),
    )
    return upsert(conn, "security", df)


def sync_shenwan(conn: duckdb.DuckDBPyConnection, ad: Adapters, day: dt.date) -> tuple[int, int]:
    """申万一级 / 二级列表 + 当日成分快照。返回 (板块数, 成分行数)。"""
    l1 = ad.shenwan.l1_list(day)
    l2 = ad.shenwan.l2_list(day)
    name_to_id = dict(zip(l1["name"].to_list(), l1["sector_id"].to_list(), strict=True))
    sectors = pl.concat(
        [
            l1.select(
                "sector_id",
                "name",
                pl.lit(SectorLevel.L1.value).alias("level"),
                pl.lit(None, dtype=pl.Utf8).alias("parent_id"),
            ),
            l2.select(
                "sector_id",
                "name",
                pl.lit(SectorLevel.L2.value).alias("level"),
                pl.col("parent_name").replace_strict(name_to_id, default=None).alias("parent_id"),
            ),
        ]
    ).with_columns(source=pl.lit("shenwan"))
    n_sectors = upsert(conn, "sector", sectors)

    members: list[pl.DataFrame] = []
    for sector_id in sectors["sector_id"].to_list():
        try:
            cons = ad.shenwan.cons(sector_id, day)
        except AdapterError as exc:
            record_quality(
                conn,
                source="shenwan",
                endpoint="cons",
                trade_date=day,
                status=QualityStatus.MISSING,
                reason=f"{sector_id}: {exc.reason}",
            )
            continue
        members.append(
            cons.select(
                pl.lit(sector_id).alias("sector_id"), "code", pl.lit(day).alias("snapshot_date")
            )
        )
    n_members = upsert(conn, "sector_member_snapshot", pl.concat(members)) if members else 0
    return n_sectors, n_members


def sync_index_members(conn: duckdb.DuckDBPyConnection, ad: Adapters, day: dt.date) -> int:
    """中证成分：新出现的写 effective_from=当日（首次运行则为当日），消失的补 effective_to。"""
    total = 0
    for index_id in INDEX_SYMBOLS:
        try:
            cons = ad.csindex.cons(index_id, day)
        except (AdapterError, KeyError) as exc:
            record_quality(
                conn,
                source="csindex",
                endpoint="cons",
                trade_date=day,
                status=QualityStatus.MISSING,
                reason=str(exc),
            )
            continue
        current = conn.execute(
            "SELECT code FROM index_member WHERE index_id = ? AND effective_to IS NULL", [index_id]
        ).pl()
        existing = set(current["code"].to_list()) if not current.is_empty() else set()
        fresh = set(cons["code"].to_list())
        added = sorted(fresh - existing)
        removed = sorted(existing - fresh)
        if added:
            total += upsert(
                conn,
                "index_member",
                pl.DataFrame(
                    {
                        "index_id": [index_id] * len(added),
                        "code": added,
                        "effective_from": [day] * len(added),
                        "effective_to": [None] * len(added),
                    },
                    schema={
                        "index_id": pl.Utf8,
                        "code": pl.Utf8,
                        "effective_from": pl.Date,
                        "effective_to": pl.Date,
                    },
                ),
            )
        for code in removed:
            conn.execute(
                "UPDATE index_member SET effective_to = ? WHERE index_id = ? AND code = ? AND effective_to IS NULL",
                [day, index_id, code],
            )
    return total


@dataclass
class ConceptFetch:
    sectors: pl.DataFrame
    members: list[pl.DataFrame] = field(default_factory=list)
    issues: list[tuple[QualityStatus, str]] = field(default_factory=list)


def fetch_concepts(ad: Adapters, day: dt.date, *, max_boards: int | None = None) -> ConceptFetch:
    """东财概念列表 + 成分（只拉不写，写库锁外调用）。成分是个股级接口，逐个板块串行带间隔。"""
    boards = ad.eastmoney.concept_list(day)
    out = ConceptFetch(
        boards.select(
            ("concept:" + pl.col("board_code")).alias("sector_id"),
            "name",
            pl.lit(SectorLevel.CONCEPT.value).alias("level"),
            pl.lit(None, dtype=pl.Utf8).alias("parent_id"),
            pl.lit("eastmoney").alias("source"),
        )
    )
    for i, row in enumerate(out.sectors.iter_rows(named=True)):
        if max_boards is not None and i >= max_boards:
            break
        try:
            cons = ad.eastmoney.concept_cons(row["name"], day)
        except AdapterError as exc:
            captcha = "验证" in exc.reason
            status = QualityStatus.CAPTCHA if captcha else QualityStatus.MISSING
            out.issues.append((status, f"{row['name']}: {exc.reason}"))
            if captcha:
                break
            continue
        out.members.append(
            cons.select(
                pl.lit(row["sector_id"]).alias("sector_id"),
                "code",
                pl.lit(day).alias("snapshot_date"),
            )
        )
    return out


def store_concepts(
    conn: duckdb.DuckDBPyConnection, fetched: ConceptFetch, day: dt.date
) -> tuple[int, int]:
    for status, reason in fetched.issues:
        record_quality(
            conn,
            source="eastmoney",
            endpoint="concept_cons",
            trade_date=day,
            status=status,
            reason=reason,
        )
    n_sectors = upsert(conn, "sector", fetched.sectors)
    members = fetched.members
    n_members = upsert(conn, "sector_member_snapshot", pl.concat(members)) if members else 0
    return n_sectors, n_members


def sync_concepts(
    conn: duckdb.DuckDBPyConnection, ad: Adapters, day: dt.date, *, max_boards: int | None = None
) -> tuple[int, int]:
    return store_concepts(conn, fetch_concepts(ad, day, max_boards=max_boards), day)


def members_as_of(
    conn: duckdb.DuckDBPyConnection, trade_date: dt.date, levels: tuple[str, ...] | None = None
) -> pl.DataFrame:
    """某日生效成分：每个板块取 ≤ 目标日的最近一次快照。返回 (sector_id, code, trade_date, level, first_seen)。"""
    level_filter = ""
    params: list[object] = [trade_date, trade_date]
    if levels:
        placeholders = ", ".join("?" for _ in levels)
        level_filter = f"AND s.level IN ({placeholders})"
        params.extend(levels)
    sql = f"""
        WITH latest AS (
            SELECT sector_id, max(snapshot_date) AS snapshot_date
            FROM sector_member_snapshot WHERE snapshot_date <= ? GROUP BY sector_id
        ),
        first_seen AS (
            SELECT sector_id, code, min(snapshot_date) AS first_seen FROM sector_member_snapshot GROUP BY sector_id, code
        )
        SELECT m.sector_id, m.code, CAST(? AS DATE) AS trade_date, s.level, f.first_seen
        FROM sector_member_snapshot m
        JOIN latest l USING (sector_id, snapshot_date)
        JOIN sector s ON s.sector_id = m.sector_id
        JOIN first_seen f ON f.sector_id = m.sector_id AND f.code = m.code
        WHERE 1 = 1 {level_filter}
    """
    return conn.execute(sql, params).pl()
