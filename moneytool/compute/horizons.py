"""多周期资金分析：当天 / 5 日 / 20 日 / 40 日 / 60 日 / 120 日 / 250 日。

输入是计算链里已有的板块日线特征（`sector_feat`，含近 320 个交易日）与市场日线行，全部向量化计算：

- 周期累计：主力净流入、成交额、主力占比（净流入 / 成交额）、涨跌幅、净流入天数占比，及同级板块分位；
- 趋势倾向：短期（5 日 + 资金加速度）、中期（20 / 40 日）、长期（60 / 120 / 250 日）资金与价格分位合成 −100…100，
  给出方向、强弱与短中长期是否一致；
- 历史统计：同一算法在过去每个交易日的倾向，与其后 5 / 20 日板块实际涨跌、相对同级平均的超额对照
  （只用前瞻窗口已走完的样本；日与日样本有重叠，只作参考）；
- 资金动向信号：资金异动、倾向转向、多周期共振。

倾向是对已发生资金与价格的统计归纳，不是对未来走势的判断。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import polars as pl

from moneytool.compute.quant import min_samples
from moneytool.params import Params
from moneytool.storage.repo import to_json

MARKET_ID = "market"

HORIZONS: tuple[tuple[str, int, str], ...] = (
    ("1d", 1, "当天"),
    ("5d", 5, "5 日"),
    ("20d", 20, "20 日"),
    ("40d", 40, "40 日"),
    ("60d", 60, "60 日"),
    ("120d", 120, "120 日"),
    ("250d", 250, "250 日"),
)
HORIZON_LABEL = {k: label for k, _, label in HORIZONS}
HORIZON_HINT = {
    "1d": "当天",
    "5d": "一周",
    "20d": "一个月",
    "40d": "两个月",
    "60d": "三个月",
    "120d": "半年",
    "250d": "一年",
}

SHORT, MID, LONG = ("5d",), ("20d", "40d"), ("60d", "120d", "250d")
TERM_WEIGHTS = (0.5, 0.3, 0.2)
DIRECTION_THRESHOLD = 20.0
STRONG_THRESHOLD = 50.0
SIGN_THRESHOLD = 0.1
FWD_DAYS = (5, 20)

SURGE_MULTIPLE = 3.0
SURGE_RANK = 90.0
TURN_SCORE = 35.0
RESONANCE_RANK = 80.0
RESONANCE_HORIZONS = ("5d", "20d", "60d")

SIGNAL_ZH = {
    "surge_in": "资金异动流入",
    "surge_out": "资金异动流出",
    "turn_up": "倾向转为上升",
    "turn_down": "倾向转为下降",
    "resonance_in": "多周期共振流入",
    "resonance_out": "多周期共振流出",
}
DIRECTION_ZH = {"up": "上升", "flat": "震荡", "down": "下降"}


@dataclass
class HorizonResult:
    horizons: pl.DataFrame  # 当日长表：每板块 × 周期一行
    trends: pl.DataFrame  # 当日每板块一行
    backtest: pl.DataFrame  # 截至当日的历史统计：级别 × 方向 × 前瞻天数
    signals: pl.DataFrame  # 当日资金动向信号
    history: pl.DataFrame  # 窗口内当日之前各日的倾向得分（无依据文本），供补齐得分走势


def _base(sector_feat: pl.DataFrame, market: pl.DataFrame) -> pl.DataFrame:
    sectors = sector_feat.select(
        "sector_id",
        "trade_date",
        pl.col("level").fill_null("other").cast(pl.Utf8),
        pl.col("small_sample").fill_null(False),
        pl.col("sector_net_main").cast(pl.Float64).alias("net"),
        pl.col("sector_amount").cast(pl.Float64).alias("amt"),
        pl.col("sector_pct_chg").cast(pl.Float64).alias("pct"),
    )
    frames = [sectors]
    if not market.is_empty():
        frames.append(
            market.select(
                pl.lit(MARKET_ID).alias("sector_id"),
                "trade_date",
                pl.lit(MARKET_ID).alias("level"),
                pl.lit(False).alias("small_sample"),
                pl.col("net_main_all").cast(pl.Float64).alias("net"),
                pl.col("amount_all").cast(pl.Float64).alias("amt"),
                pl.col("eqw_ret").cast(pl.Float64).alias("pct"),
            )
        )
    return pl.concat(frames, how="vertical_relaxed").sort("sector_id", "trade_date")


def _pct_rank(col: str) -> pl.Expr:
    """同日、同级、非小样本板块中的分位（0–100，越大越靠前）；市场行与小样本为 null。"""
    masked = (
        pl.when(~pl.col("small_sample") & (pl.col("level") != MARKET_ID))
        .then(pl.col(col))
        .otherwise(None)
    )
    by = ["trade_date", "level"]
    n = masked.count().over(by)
    return (
        pl.when(masked.is_null())
        .then(None)
        .when(n > 1)
        .then((masked.rank("average").over(by) - 1) / (n - 1) * 100)
        .otherwise(50.0)
    )


def horizon_wide(sector_feat: pl.DataFrame, market: pl.DataFrame, p: Params) -> pl.DataFrame:
    """每板块每日一行，各周期指标展开成列（`net_5d`、`mr_5d`、`fr_5d` …）。"""
    r = p.windows.min_valid_ratio
    df = _base(sector_feat, market).with_columns(
        _g=(1 + pl.col("pct").fill_null(0.0)).cum_prod().over("sector_id"),
        _i=pl.int_range(pl.len()).over("sector_id"),
        _in=pl.when(pl.col("net").is_null())
        .then(None)
        .otherwise((pl.col("net") > 0).cast(pl.Float64)),
        absavg_prev20=pl.col("net")
        .abs()
        .rolling_mean(20, min_samples=min_samples(20, r))
        .shift(1)
        .over("sector_id"),
    )
    cols: list[pl.Expr] = []
    for key, n, _ in HORIZONS:
        ms = min_samples(n, r)
        # 样本达到窗口的 60% 就出数，与净流入同一门槛；不满整窗时用已有区间的累计涨跌。
        if n == 1:
            ret = pl.col("pct")
        else:
            ret = (
                pl.when(pl.col("_i") >= n)
                .then(pl.col("_g") / pl.col("_g").shift(n).over("sector_id") - 1)
                .when(pl.col("_i") + 1 >= ms)
                .then(pl.col("_g") - 1)
                .otherwise(None)
            )
        cols += [
            pl.col("net").rolling_sum(n, min_samples=ms).over("sector_id").alias(f"net_{key}"),
            pl.col("amt").rolling_sum(n, min_samples=ms).over("sector_id").alias(f"amt_{key}"),
            ret.alias(f"ret_{key}"),
            pl.col("_in").rolling_mean(n, min_samples=ms).over("sector_id").alias(f"inflow_{key}"),
        ]
    df = df.with_columns(cols)
    df = df.with_columns(
        [
            pl.when(pl.col(f"amt_{k}") > 0)
            .then(pl.col(f"net_{k}") / pl.col(f"amt_{k}"))
            .otherwise(None)
            .alias(f"mr_{k}")
            for k, _, _ in HORIZONS
        ]
    )
    return df.with_columns(
        [_pct_rank(f"mr_{k}").alias(f"fr_{k}") for k, _, _ in HORIZONS]
        + [_pct_rank(f"ret_{k}").alias(f"rr_{k}") for k, _, _ in HORIZONS]
    )


def horizon_long(wide: pl.DataFrame) -> pl.DataFrame:
    """宽表 → `sector_horizon` 长表（不含 segment / param_version）。"""
    parts = [
        wide.select(
            "sector_id",
            "trade_date",
            pl.lit(k).alias("horizon"),
            pl.lit(n, dtype=pl.Int32).alias("days"),
            pl.col(f"net_{k}").alias("net_main"),
            pl.col(f"amt_{k}").alias("amount"),
            pl.col(f"mr_{k}").alias("main_ratio"),
            pl.col(f"ret_{k}").alias("ret"),
            pl.col(f"inflow_{k}").alias("inflow_days_ratio"),
            pl.col(f"fr_{k}").alias("flow_rank_pct"),
            pl.col(f"rr_{k}").alias("ret_rank_pct"),
        )
        for k, n, _ in HORIZONS
    ]
    return pl.concat(parts)


def _centred(col: str) -> pl.Expr:
    return (pl.col(col) - 50) / 50


def _weighted(terms: list[tuple[float, pl.Expr]]) -> pl.Expr:
    num = sum((pl.lit(w) * x).fill_null(0.0) for w, x in terms)
    den = sum(pl.when(x.is_null()).then(0.0).otherwise(w) for w, x in terms)
    return pl.when(den > 0).then(num / den).otherwise(None)


def _sign(col: str) -> pl.Expr:
    return (
        pl.when(pl.col(col).is_null())
        .then(None)
        .when(pl.col(col) >= SIGN_THRESHOLD)
        .then(1)
        .when(pl.col(col) <= -SIGN_THRESHOLD)
        .then(-1)
        .otherwise(0)
    )


def trend_frame(wide: pl.DataFrame) -> pl.DataFrame:
    """逐日趋势倾向。每个周期的分量 = 0.5 × 资金分位 + 0.3 × 涨跌分位 + 0.2 × 净流入天数，均居中到 −1…1。"""
    comp = [
        (
            0.5 * _centred(f"fr_{k}")
            + 0.3 * _centred(f"rr_{k}")
            + 0.2 * (pl.col(f"inflow_{k}") - 0.5) * 2
        ).alias(f"c_{k}")
        for k, _, _ in HORIZONS
        if k != "1d"
    ]
    df = wide.with_columns(comp, accel=pl.col("mr_5d") - pl.col("mr_20d"))
    df = df.with_columns(_accel_rank=_pct_rank("accel"))
    df = df.with_columns(
        short_score=_weighted([(0.8, pl.col("c_5d")), (0.2, _centred("_accel_rank"))]),
        mid_score=pl.mean_horizontal([pl.col(f"c_{k}") for k in MID]),
        long_score=pl.mean_horizontal([pl.col(f"c_{k}") for k in LONG]),
    )
    ws, wm, wl = TERM_WEIGHTS
    df = df.with_columns(
        score=(
            _weighted(
                [(ws, pl.col("short_score")), (wm, pl.col("mid_score")), (wl, pl.col("long_score"))]
            )
            * 100
        ).round(1),
    )
    signs = [_sign(c) for c in ("short_score", "mid_score", "long_score")]
    n_valid = sum(s.is_not_null().cast(pl.Int32) for s in signs)
    n_pos = sum((s == 1).fill_null(False).cast(pl.Int32) for s in signs)
    n_neg = sum((s == -1).fill_null(False).cast(pl.Int32) for s in signs)
    df = df.with_columns(
        direction=pl.when(pl.col("score").is_null() | (pl.col("level") == MARKET_ID))
        .then(None)
        .when(pl.col("score") >= DIRECTION_THRESHOLD)
        .then(pl.lit("up"))
        .when(pl.col("score") <= -DIRECTION_THRESHOLD)
        .then(pl.lit("down"))
        .otherwise(pl.lit("flat")),
        strength=pl.when(pl.col("score").is_null())
        .then(None)
        .when(pl.col("score").abs() >= STRONG_THRESHOLD)
        .then(pl.lit("strong"))
        .when(pl.col("score").abs() >= DIRECTION_THRESHOLD)
        .then(pl.lit("medium"))
        .otherwise(pl.lit("weak")),
        consistency=pl.when(n_valid < 2)
        .then(pl.lit("insufficient"))
        .when((n_pos == n_valid) | (n_neg == n_valid))
        .then(pl.lit("aligned"))
        .otherwise(pl.lit("mixed")),
    )
    return df.drop("_accel_rank")


def backtest_frame(trend: pl.DataFrame) -> pl.DataFrame:
    """历史每日倾向 → 其后 N 日实际涨跌统计。只统计前瞻窗口已走完、非小样本的板块。"""
    df = trend.filter(pl.col("level") != MARKET_ID)
    fwd = [
        (pl.col("_g").shift(-n).over("sector_id") / pl.col("_g") - 1).alias(f"fwd_{n}")
        for n in FWD_DAYS
    ]
    df = df.with_columns(fwd).filter(~pl.col("small_sample") & pl.col("direction").is_not_null())
    parts: list[pl.DataFrame] = []
    for n in FWD_DAYS:
        col = f"fwd_{n}"
        sub = df.filter(pl.col(col).is_not_null()).with_columns(
            _excess=pl.col(col) - pl.col(col).mean().over("trade_date", "level")
        )
        if sub.is_empty():
            continue
        parts.append(
            sub.group_by("level", "direction")
            .agg(
                samples=pl.len().cast(pl.Int32),
                up_ratio=(pl.col(col) > 0).mean(),
                avg_ret=pl.col(col).mean(),
                avg_excess=pl.col("_excess").mean(),
                beat_ratio=(pl.col("_excess") > 0).mean(),
            )
            .with_columns(fwd_days=pl.lit(n, dtype=pl.Int32))
        )
    if not parts:
        return pl.DataFrame(
            schema={
                "level": pl.Utf8,
                "direction": pl.Utf8,
                "samples": pl.Int32,
                "up_ratio": pl.Float64,
                "avg_ret": pl.Float64,
                "avg_excess": pl.Float64,
                "beat_ratio": pl.Float64,
                "fwd_days": pl.Int32,
            }
        )
    return pl.concat(parts).sort("level", "direction", "fwd_days")


def fmt_money(v: float | None, *, signed: bool = True) -> str:
    if v is None:
        return "—"
    sign = "+" if signed else ""
    if abs(v) >= 1e8:
        return f"{v / 1e8:{sign}.2f} 亿"
    return f"{v / 1e4:{sign}.0f} 万"


def _pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def trend_reasons(row: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k in ("5d", "20d", "60d"):
        if row.get(f"net_{k}") is None:
            continue
        rank = row.get(f"fr_{k}")
        out.append(
            f"{HORIZON_LABEL[k]}主力净流入 {fmt_money(row[f'net_{k}'])}"
            f"（占成交 {_pct(row.get(f'mr_{k}'), 2)}"
            + (f"，同级 {rank:.0f} 分位" if rank is not None else "")
            + f"），涨跌 {_pct(row.get(f'ret_{k}'))}"
        )
    accel = row.get("accel")
    if accel is not None:
        out.append(
            f"近 5 日主力占比较 20 日{'抬升' if accel >= 0 else '回落'} {abs(accel) * 100:.2f} 个百分点"
        )
    cons = row.get("consistency")
    if cons == "aligned":
        out.append("短、中、长期资金方向一致")
    elif cons == "mixed":
        out.append("短、中、长期资金方向分化")
    return out


def _backtest_lookup(bt: pl.DataFrame) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in bt.iter_rows(named=True):
        out.setdefault((r["level"], r["direction"]), {})[f"{r['fwd_days']}d"] = {
            "samples": r["samples"],
            "up_ratio": r["up_ratio"],
            "avg_ret": r["avg_ret"],
            "avg_excess": r["avg_excess"],
            "beat_ratio": r["beat_ratio"],
        }
    return out


def _signal_rows(
    today: pl.DataFrame, prev: pl.DataFrame | None, names: dict[str, str]
) -> list[dict[str, Any]]:
    prev_map: dict[str, dict[str, Any]] = (
        {r["sector_id"]: r for r in prev.iter_rows(named=True)} if prev is not None else {}
    )
    rows: list[dict[str, Any]] = []

    def add(r: dict[str, Any], kind: str, score: float, text: str, **payload: Any) -> None:
        rows.append(
            {
                "sector_id": r["sector_id"],
                "kind": kind,
                "score": float(score),
                "text": text,
                "payload": to_json(
                    {
                        "level": r["level"],
                        "direction": r.get("direction"),
                        "trend_score": r.get("score"),
                        **payload,
                    }
                ),
            }
        )

    def resonance(r: dict[str, Any] | None, up: bool) -> bool:
        if r is None:
            return False
        raw = [r.get(f"fr_{k}") for k in RESONANCE_HORIZONS]
        ranks = [float(x) for x in raw if x is not None]
        if len(ranks) < len(raw):
            return False
        if up:
            return r.get("direction") == "up" and all(x >= RESONANCE_RANK for x in ranks)
        return r.get("direction") == "down" and all(x <= 100 - RESONANCE_RANK for x in ranks)

    for r in today.iter_rows(named=True):
        sid = r["sector_id"]
        if r["level"] == MARKET_ID or r["small_sample"]:
            continue
        name = names.get(sid, sid)
        net, avg, fr = r.get("net_1d"), r.get("absavg_prev20"), r.get("fr_1d")
        if net is not None and avg and fr is not None:
            multiple = abs(net) / avg
            if multiple >= SURGE_MULTIPLE:
                if net > 0 and fr >= SURGE_RANK:
                    add(
                        r,
                        "surge_in",
                        multiple,
                        f"{name} 当天主力净流入 {fmt_money(net, signed=False)}，为近 20 日日均规模的 {multiple:.1f} 倍，"
                        f"主力占比居同级 {fr:.0f} 分位",
                        net=net,
                        multiple=multiple,
                    )
                elif net < 0 and fr <= 100 - SURGE_RANK:
                    add(
                        r,
                        "surge_out",
                        -multiple,
                        f"{name} 当天主力净流出 {fmt_money(-net, signed=False)}，为近 20 日日均规模的 {multiple:.1f} 倍，"
                        f"主力占比居同级 {fr:.0f} 分位",
                        net=net,
                        multiple=multiple,
                    )
        score = r.get("score")
        before = prev_map.get(sid)
        prev_score = before.get("score") if before else None
        prev_dir = DIRECTION_ZH.get((before or {}).get("direction") or "flat", "震荡")
        if score is not None and prev_score is not None:
            if score >= TURN_SCORE and prev_score < DIRECTION_THRESHOLD:
                add(
                    r,
                    "turn_up",
                    score,
                    f"{name} 趋势倾向由{prev_dir}转为上升（得分 {prev_score:.0f} → {score:.0f}）",
                    prev_score=prev_score,
                )
            elif score <= -TURN_SCORE and prev_score > -DIRECTION_THRESHOLD:
                add(
                    r,
                    "turn_down",
                    score,
                    f"{name} 趋势倾向由{prev_dir}转为下降（得分 {prev_score:.0f} → {score:.0f}）",
                    prev_score=prev_score,
                )
        for up, kind in ((True, "resonance_in"), (False, "resonance_out")):
            if resonance(r, up) and not resonance(before, up):
                ranks = "、".join(
                    f"{HORIZON_LABEL[k]} {r[f'fr_{k}']:.0f}" for k in RESONANCE_HORIZONS
                )
                add(
                    r,
                    kind,
                    score or 0.0,
                    f"{name} 多周期资金共振{'流入' if up else '流出'}：主力占比同级分位 {ranks}",
                    ranks={k: r[f"fr_{k}"] for k in RESONANCE_HORIZONS},
                )
    return rows


TREND_COLUMNS = (
    "sector_id",
    "trade_date",
    "score",
    "direction",
    "strength",
    pl.col("short_score") * 100,
    pl.col("mid_score") * 100,
    pl.col("long_score") * 100,
    "consistency",
    "accel",
)


def compute_horizons(
    sector_feat: pl.DataFrame,
    market: pl.DataFrame,
    p: Params,
    day: dt.date,
    *,
    intraday: bool,
    names: dict[str, str] | None = None,
) -> HorizonResult:
    names = names or {}
    wide = horizon_wide(sector_feat, market, p)
    trend = trend_frame(wide)
    # 盘中当日行是部分数据，不进历史统计
    bt = backtest_frame(trend.filter(pl.col("trade_date") < day) if intraday else trend)
    lookup = _backtest_lookup(bt)
    today = trend.filter(pl.col("trade_date") == day)
    prev_day = trend.filter(pl.col("trade_date") < day)["trade_date"].max()
    prev = trend.filter(pl.col("trade_date") == prev_day) if prev_day is not None else None

    sectors_today = today.filter(pl.col("level") != MARKET_ID)
    trend_rows = sectors_today.select(TREND_COLUMNS).with_columns(
        reasons=pl.Series(
            [to_json(trend_reasons(r)) for r in sectors_today.iter_rows(named=True)], dtype=pl.Utf8
        ),
        backtest=pl.Series(
            [
                to_json(lookup.get((r["level"], r["direction"])) or {})
                for r in sectors_today.select("level", "direction").iter_rows(named=True)
            ],
            dtype=pl.Utf8,
        ),
    )
    sig = _signal_rows(today, prev, names)
    signals = (
        pl.DataFrame(sig)
        if sig
        else pl.DataFrame(
            schema={
                "sector_id": pl.Utf8,
                "kind": pl.Utf8,
                "score": pl.Float64,
                "text": pl.Utf8,
                "payload": pl.Utf8,
            }
        )
    ).with_columns(trade_date=pl.lit(day, dtype=pl.Date))
    return HorizonResult(
        horizons=horizon_long(today),
        trends=trend_rows,
        backtest=bt.with_columns(trade_date=pl.lit(day, dtype=pl.Date)),
        signals=signals,
        history=trend.filter(
            (pl.col("trade_date") < day)
            & (pl.col("level") != MARKET_ID)
            & pl.col("score").is_not_null()
        ).select(TREND_COLUMNS),
    )
