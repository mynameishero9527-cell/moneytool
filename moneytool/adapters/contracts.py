"""各源各接口标准化后的契约。测试用录制 / 构造响应逐个验证。"""

from __future__ import annotations

import polars as pl

from moneytool.adapters.base import Contract

FLOW_COLS: dict[str, pl.DataType] = {
    "net_main": pl.Float64(),
    "net_super": pl.Float64(),
    "net_large": pl.Float64(),
    "net_medium": pl.Float64(),
    "net_small": pl.Float64(),
    "main_ratio": pl.Float64(),
    "super_ratio": pl.Float64(),
    "large_ratio": pl.Float64(),
    "medium_ratio": pl.Float64(),
    "small_ratio": pl.Float64(),
}

EASTMONEY: dict[str, Contract] = {
    "flow_rank_stock": Contract(
        columns={
            "code": pl.Utf8(),
            "name": pl.Utf8(),
            "close": pl.Float64(),
            "pct_chg": pl.Float64(),
            **FLOW_COLS,
        },
        required_non_null=("code",),
        min_rows=3000,
        unit_notes="净额 元；净占比源为百分数已 /100；涨跌幅已 /100。",
    ),
    "flow_rank_sector": Contract(
        columns={"name": pl.Utf8(), "pct_chg": pl.Float64(), **FLOW_COLS, "top_stock": pl.Utf8()},
        required_non_null=("name",),
        min_rows=50,
        unit_notes="同 flow_rank_stock；行业为东财行业口径，只用于概念与对账。",
    ),
    "flow_daily_stock": Contract(
        columns={
            "trade_date": pl.Date(),
            "close": pl.Float64(),
            "pct_chg": pl.Float64(),
            **FLOW_COLS,
        },
        required_non_null=("trade_date",),
        min_rows=1,
        unit_notes="东财按股票返回全部历史（约 100 个交易日以上）。",
    ),
    "flow_daily_sector": Contract(
        columns={"trade_date": pl.Date(), **FLOW_COLS},
        required_non_null=("trade_date",),
        min_rows=1,
    ),
    "flow_market": Contract(
        columns={
            "trade_date": pl.Date(),
            **FLOW_COLS,
            "sh_close": pl.Float64(),
            "sh_pct_chg": pl.Float64(),
        },
        required_non_null=("trade_date",),
        min_rows=1,
    ),
    "concept_list": Contract(
        columns={
            "board_code": pl.Utf8(),
            "name": pl.Utf8(),
            "pct_chg": pl.Float64(),
            "up_count": pl.Int64(),
        },
        required_non_null=("board_code", "name"),
        min_rows=100,
    ),
    "concept_cons": Contract(
        columns={"code": pl.Utf8(), "name": pl.Utf8()},
        required_non_null=("code",),
        min_rows=1,
    ),
    "spot": Contract(
        columns={
            "code": pl.Utf8(),
            "name": pl.Utf8(),
            "open": pl.Float64(),
            "high": pl.Float64(),
            "low": pl.Float64(),
            "close": pl.Float64(),
            "pre_close": pl.Float64(),
            "volume": pl.Float64(),
            "pct_chg": pl.Float64(),
            "amount": pl.Float64(),
            "turnover": pl.Float64(),
            "total_mv": pl.Float64(),
            "float_mv": pl.Float64(),
        },
        required_non_null=("code",),
        min_rows=3000,
        unit_notes="换手率与涨跌幅源为百分数已 /100；市值 元；成交量 股（东财源为手已 ×100）；昨收为交易所除权后价。",
    ),
    "hist": Contract(
        columns={
            "trade_date": pl.Date(),
            "open": pl.Float64(),
            "close": pl.Float64(),
            "high": pl.Float64(),
            "low": pl.Float64(),
            "volume": pl.Float64(),
            "amount": pl.Float64(),
            "pct_chg": pl.Float64(),
            "turnover": pl.Float64(),
        },
        required_non_null=("trade_date",),
        min_rows=1,
    ),
}

BAOSTOCK: dict[str, Contract] = {
    "trade_dates": Contract(columns={"trade_date": pl.Date(), "is_open": pl.Boolean()}, min_rows=1),
    "kdata": Contract(
        columns={
            "code": pl.Utf8(),
            "trade_date": pl.Date(),
            "open": pl.Float64(),
            "high": pl.Float64(),
            "low": pl.Float64(),
            "close": pl.Float64(),
            "pre_close": pl.Float64(),
            "volume": pl.Float64(),
            "amount": pl.Float64(),
            "turnover": pl.Float64(),
            "pct_chg": pl.Float64(),
            "is_suspended": pl.Boolean(),
            "is_st": pl.Boolean(),
        },
        required_non_null=("code", "trade_date"),
        min_rows=0,
        unit_notes="turn 源为百分数已 /100；pctChg 已 /100；tradestatus 0 = 停牌。",
    ),
    "adjust_factor": Contract(
        columns={"code": pl.Utf8(), "effective_date": pl.Date(), "adj_factor": pl.Float64()},
        min_rows=0,
        unit_notes="backAdjustFactor 后复权因子，按生效日前向填充。",
    ),
    "stock_basic": Contract(
        columns={
            "code": pl.Utf8(),
            "name": pl.Utf8(),
            "list_date": pl.Date(),
            "delist_date": pl.Date(),
            "is_stock": pl.Boolean(),
            "is_listed": pl.Boolean(),
        },
        required_non_null=("code",),
        min_rows=1,
    ),
    "all_stock": Contract(
        columns={"code": pl.Utf8(), "name": pl.Utf8(), "is_trading": pl.Boolean()},
        required_non_null=("code",),
        min_rows=1000,
    ),
}

SHENWAN: dict[str, Contract] = {
    "l1_list": Contract(
        columns={"sector_id": pl.Utf8(), "name": pl.Utf8(), "member_count": pl.Int64()},
        required_non_null=("sector_id", "name"),
        min_rows=25,
    ),
    "l2_list": Contract(
        columns={
            "sector_id": pl.Utf8(),
            "name": pl.Utf8(),
            "parent_name": pl.Utf8(),
            "member_count": pl.Int64(),
        },
        required_non_null=("sector_id", "name"),
        min_rows=100,
    ),
    "cons": Contract(
        columns={"code": pl.Utf8(), "name": pl.Utf8(), "weight": pl.Float64(), "since": pl.Date()},
        required_non_null=("code",),
        min_rows=1,
    ),
    "index_daily": Contract(
        columns={"trade_date": pl.Date(), "close": pl.Float64(), "amount": pl.Float64()},
        required_non_null=("trade_date",),
        min_rows=1,
    ),
}

CSINDEX: dict[str, Contract] = {
    "cons": Contract(
        columns={"index_id": pl.Utf8(), "code": pl.Utf8(), "name": pl.Utf8(), "as_of": pl.Date()},
        required_non_null=("index_id", "code"),
        min_rows=1,
    ),
}

SINA: dict[str, Contract] = {
    "flow_daily_stock": Contract(
        columns={
            "trade_date": pl.Date(),
            "close": pl.Float64(),
            "pct_chg": pl.Float64(),
            **FLOW_COLS,
        },
        required_non_null=("trade_date",),
        min_rows=0,
        unit_notes=(
            "新浪 MoneyFlow.ssl_qsfx_lscjfb：r0 特大单、r1 大单、r2 中单、r3 小单的成交额与净额（元）；"
            "主力 = 特大单 + 大单；净占比 = 净额 / 四档成交额合计；涨跌幅源为小数。"
            "各档净额按主动买卖统计，合计不为零，与东财口径不同，不能混用。"
        ),
    ),
    "flow_rank_stock": Contract(
        columns={
            "code": pl.Utf8(),
            "name": pl.Utf8(),
            "close": pl.Float64(),
            "pct_chg": pl.Float64(),
            **FLOW_COLS,
        },
        required_non_null=("code",),
        min_rows=3000,
        unit_notes=(
            "新浪 MoneyFlow.ssl_bkzj_ssggzj：r0 特大单、r3 小单净额（元，与日频接口同档同值），"
            "大单与中单只给合计、各按一半估算；主力 = 特大单 + 大单（估算）；净占比分母为成交额。"
        ),
    ),
    "spot": EASTMONEY["spot"],
    "concept_list": Contract(
        columns={
            "board_code": pl.Utf8(),
            "name": pl.Utf8(),
            "pct_chg": pl.Float64(),
            "up_count": pl.Int64(),
        },
        required_non_null=("board_code", "name"),
        min_rows=50,
        unit_notes="新浪概念板块（gn_ 开头）；不提供上涨家数。",
    ),
    "concept_cons": EASTMONEY["concept_cons"],
}
