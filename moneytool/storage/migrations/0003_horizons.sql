-- 多周期资金分析（当天 / 5 日 / 20 日 / 40 日 / 60 日 / 120 日 / 250 日）、趋势倾向、历史统计与资金动向信号。
-- sector_id = 'market' 为全市场合计。segment = 'close' 为收盘确认，其余为盘中分段。

CREATE TABLE IF NOT EXISTS sector_horizon (
    sector_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL DEFAULT 'close',
    param_version VARCHAR NOT NULL,
    horizon VARCHAR NOT NULL,           -- 1d / 5d / 20d / 40d / 60d / 120d / 250d
    days INTEGER NOT NULL,
    net_main DOUBLE,                    -- 区间主力净流入合计（元）
    amount DOUBLE,                      -- 区间成交额合计（元）
    main_ratio DOUBLE,                  -- 区间主力净流入 / 成交额
    ret DOUBLE,                         -- 区间涨跌幅
    inflow_days_ratio DOUBLE,           -- 区间内主力净流入为正的天数占比
    flow_rank_pct DOUBLE,               -- 主力占比在同级板块中的分位（0–100）
    ret_rank_pct DOUBLE,                -- 区间涨跌幅在同级板块中的分位（0–100）
    PRIMARY KEY (sector_id, trade_date, segment, param_version, horizon)
);

CREATE TABLE IF NOT EXISTS sector_trend (
    sector_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL DEFAULT 'close',
    param_version VARCHAR NOT NULL,
    score DOUBLE,                       -- -100 … 100，正为上升倾向
    direction VARCHAR,                  -- up / flat / down
    strength VARCHAR,                   -- strong / medium / weak
    short_score DOUBLE,
    mid_score DOUBLE,
    long_score DOUBLE,
    consistency VARCHAR,                -- aligned / mixed / insufficient
    accel DOUBLE,                       -- 5 日主力占比 − 20 日主力占比
    reasons JSON,
    backtest JSON,                      -- 同级、同方向历史统计（见 trend_backtest）
    PRIMARY KEY (sector_id, trade_date, segment, param_version)
);

CREATE TABLE IF NOT EXISTS trend_backtest (
    trade_date DATE NOT NULL,           -- 统计截止日（只用该日之前、前瞻窗口已走完的样本）
    param_version VARCHAR NOT NULL,
    level VARCHAR NOT NULL,
    direction VARCHAR NOT NULL,
    fwd_days INTEGER NOT NULL,
    samples INTEGER NOT NULL,
    up_ratio DOUBLE,                    -- 之后 N 日板块上涨的占比
    avg_ret DOUBLE,
    avg_excess DOUBLE,                  -- 相对同级板块平均的超额
    beat_ratio DOUBLE,                  -- 跑赢同级平均的占比
    PRIMARY KEY (trade_date, param_version, level, direction, fwd_days)
);

CREATE TABLE IF NOT EXISTS flow_signal (
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL DEFAULT 'close',
    sector_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,              -- surge_in / surge_out / turn_up / turn_down / resonance_in / resonance_out
    param_version VARCHAR NOT NULL,
    score DOUBLE,
    text VARCHAR NOT NULL,
    payload JSON,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, segment, sector_id, kind, param_version)
);
