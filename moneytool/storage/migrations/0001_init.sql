-- 初始结构。对应架构 5.2。金额 DOUBLE 单位元；比例 DOUBLE 0–1；代码形如 600000.SH。

CREATE TABLE IF NOT EXISTS trade_calendar (
    trade_date DATE PRIMARY KEY,
    is_open BOOLEAN NOT NULL,
    source VARCHAR NOT NULL DEFAULT 'baostock'
);

CREATE TABLE IF NOT EXISTS security (
    code VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    exchange VARCHAR NOT NULL,          -- SH / SZ / BJ
    board VARCHAR,                      -- 主板 / 创业板 / 科创板 / 北交所
    list_date DATE,
    is_st BOOLEAN NOT NULL DEFAULT FALSE,
    is_delisting BOOLEAN NOT NULL DEFAULT FALSE,
    pinyin_initials VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sector (
    sector_id VARCHAR PRIMARY KEY,      -- sw:801010 / concept:BK0493
    name VARCHAR NOT NULL,
    level VARCHAR NOT NULL,             -- L1 / L2 / concept
    parent_id VARCHAR,                  -- 二级所属一级
    dominant_l1 VARCHAR,                -- 概念主导行业（需求 8.3）
    source VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sector_member_snapshot (
    sector_id VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    snapshot_date DATE NOT NULL,
    PRIMARY KEY (sector_id, code, snapshot_date)
);

CREATE TABLE IF NOT EXISTS index_member (
    index_id VARCHAR NOT NULL,          -- 000300.SH
    code VARCHAR NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE,
    PRIMARY KEY (index_id, code, effective_from)
);

CREATE TABLE IF NOT EXISTS bar_daily (
    code VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    pre_close DOUBLE,
    volume DOUBLE,
    amount DOUBLE,
    turnover DOUBLE,
    pct_chg DOUBLE,
    adj_factor DOUBLE,
    limit_up DOUBLE,
    limit_down DOUBLE,
    float_mv DOUBLE,
    is_suspended BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (code, trade_date)
);

-- 东财「今日」实时累计快照，每分段一行。
CREATE TABLE IF NOT EXISTS flow_snapshot (
    subject_type VARCHAR NOT NULL,      -- stock / sector
    subject_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    net_main DOUBLE, net_super DOUBLE, net_large DOUBLE, net_medium DOUBLE, net_small DOUBLE,
    pct_chg DOUBLE,
    close DOUBLE,
    PRIMARY KEY (subject_type, subject_id, trade_date, segment)
);

CREATE TABLE IF NOT EXISTS flow_intraday (
    code VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL,
    net_main DOUBLE, net_super DOUBLE, net_large DOUBLE, net_medium DOUBLE, net_small DOUBLE,
    spans_missing BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (code, trade_date, segment)
);

CREATE TABLE IF NOT EXISTS flow_daily (
    code VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    net_main DOUBLE, net_super DOUBLE, net_large DOUBLE, net_medium DOUBLE, net_small DOUBLE,
    main_ratio DOUBLE, super_ratio DOUBLE, large_ratio DOUBLE, medium_ratio DOUBLE, small_ratio DOUBLE,
    close DOUBLE,
    pct_chg DOUBLE,
    reconciled BOOLEAN NOT NULL DEFAULT FALSE,
    reconcile_diff DOUBLE,
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE IF NOT EXISTS sector_flow_intraday (
    sector_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL,
    net_main DOUBLE, net_super DOUBLE, net_large DOUBLE, net_medium DOUBLE, net_small DOUBLE,
    spans_missing BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (sector_id, trade_date, segment)
);

CREATE TABLE IF NOT EXISTS sector_flow_daily (
    sector_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    net_main DOUBLE, net_super DOUBLE, net_large DOUBLE, net_medium DOUBLE, net_small DOUBLE,
    pct_chg DOUBLE,
    reconciled BOOLEAN NOT NULL DEFAULT FALSE,
    reconcile_diff DOUBLE,
    PRIMARY KEY (sector_id, trade_date)
);

CREATE TABLE IF NOT EXISTS feature_daily (
    subject_type VARCHAR NOT NULL,      -- stock / sector
    subject_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL DEFAULT 'close',
    param_version VARCHAR NOT NULL,
    features JSON NOT NULL,             -- 6.1 派生量，列名见 capital-flow-indicators skill
    PRIMARY KEY (subject_type, subject_id, trade_date, segment, param_version)
);

CREATE TABLE IF NOT EXISTS market_daily (
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL DEFAULT 'close',
    param_version VARCHAR NOT NULL,
    eqw_ret DOUBLE,
    eqw_ret_5d DOUBLE,
    breadth_all DOUBLE,
    limit_up_count INTEGER,
    limit_up_ratio_all DOUBLE,
    amount_all DOUBLE,
    net_main_all DOUBLE,
    regime VARCHAR,
    mainline JSON,
    rotation JSON,
    market_pressure INTEGER,
    pressure_components JSON,
    risk_gate BOOLEAN NOT NULL DEFAULT FALSE,
    risk_gate_reasons JSON,
    evidence JSON,
    data_status VARCHAR NOT NULL DEFAULT 'confirmed',
    PRIMARY KEY (trade_date, segment, param_version)
);

CREATE TABLE IF NOT EXISTS sector_stage_confirmed (
    sector_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    param_version VARCHAR NOT NULL,
    stage VARCHAR NOT NULL,
    half VARCHAR,
    candidate VARCHAR,
    suspected_to VARCHAR,
    abnormal_transition BOOLEAN NOT NULL DEFAULT FALSE,
    carried_over BOOLEAN NOT NULL DEFAULT FALSE,
    days_in_stage INTEGER,
    entered_from VARCHAR,
    suppressed JSON,
    pattern VARCHAR,
    is_pulse BOOLEAN NOT NULL DEFAULT FALSE,
    small_sample BOOLEAN NOT NULL DEFAULT FALSE,
    attribution JSON,
    evidence JSON,
    PRIMARY KEY (sector_id, trade_date, param_version)
);

CREATE TABLE IF NOT EXISTS sector_stage_intraday (
    sector_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL,
    param_version VARCHAR NOT NULL,
    stage VARCHAR NOT NULL,
    half VARCHAR,
    candidate VARCHAR,
    suspected_to VARCHAR,
    abnormal_transition BOOLEAN NOT NULL DEFAULT FALSE,
    carried_over BOOLEAN NOT NULL DEFAULT FALSE,
    days_in_stage INTEGER,
    entered_from VARCHAR,
    suppressed JSON,
    pattern VARCHAR,
    is_pulse BOOLEAN NOT NULL DEFAULT FALSE,
    small_sample BOOLEAN NOT NULL DEFAULT FALSE,
    attribution JSON,
    evidence JSON,
    PRIMARY KEY (sector_id, trade_date, segment, param_version)
);

CREATE TABLE IF NOT EXISTS stock_role_confirmed (
    code VARCHAR NOT NULL,
    sector_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    param_version VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    tags JSON,
    score INTEGER,
    score_components JSON,
    evidence JSON,
    PRIMARY KEY (code, sector_id, trade_date, param_version)
);

CREATE TABLE IF NOT EXISTS stock_role_intraday (
    code VARCHAR NOT NULL,
    sector_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL,
    param_version VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    tags JSON,
    score INTEGER,
    score_components JSON,
    evidence JSON,
    PRIMARY KEY (code, sector_id, trade_date, segment, param_version)
);

CREATE TABLE IF NOT EXISTS action_list_confirmed (
    code VARCHAR NOT NULL,
    sector_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    list_type VARCHAR NOT NULL,
    param_version VARCHAR NOT NULL,
    basis_level VARCHAR NOT NULL,
    point_type VARCHAR,
    tags JSON,
    evidence JSON,
    invalidation JSON,
    PRIMARY KEY (code, sector_id, trade_date, list_type, param_version)
);

CREATE TABLE IF NOT EXISTS action_list_intraday (
    code VARCHAR NOT NULL,
    sector_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL,
    list_type VARCHAR NOT NULL,
    param_version VARCHAR NOT NULL,
    basis_level VARCHAR NOT NULL,
    point_type VARCHAR,
    tags JSON,
    evidence JSON,
    invalidation JSON,
    PRIMARY KEY (code, sector_id, trade_date, segment, list_type, param_version)
);

CREATE TABLE IF NOT EXISTS tracking (
    code VARCHAR NOT NULL,
    sector_id VARCHAR NOT NULL,
    entered_date DATE NOT NULL,
    exited_date DATE,
    exit_reason VARCHAR,
    PRIMARY KEY (code, sector_id, entered_date)
);

CREATE TABLE IF NOT EXISTS label_outcome (
    list_type VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    sector_id VARCHAR NOT NULL,
    entered_date DATE NOT NULL,
    horizon INTEGER NOT NULL,
    param_version VARCHAR NOT NULL,
    ret DOUBLE,
    excess_vs_sector DOUBLE,
    excess_vs_eqw DOUBLE,
    max_drawdown DOUBLE,
    risk_gate BOOLEAN,
    PRIMARY KEY (list_type, code, sector_id, entered_date, horizon)
);

CREATE TABLE IF NOT EXISTS index_daily (
    subject_type VARCHAR NOT NULL,
    subject_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    index_name VARCHAR NOT NULL,        -- sector_sentiment / sector_risk / stock_risk / retail_pressure
    segment VARCHAR NOT NULL DEFAULT 'close',
    param_version VARCHAR NOT NULL,
    total INTEGER,
    components JSON,
    window_ok BOOLEAN NOT NULL DEFAULT TRUE,
    degraded BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (subject_type, subject_id, trade_date, index_name, segment, param_version)
);

CREATE TABLE IF NOT EXISTS hold_eval (
    code VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    param_version VARCHAR NOT NULL,
    eval VARCHAR NOT NULL,              -- intact / review / broken
    changed_from VARCHAR,
    evidence JSON,
    PRIMARY KEY (code, trade_date, param_version)
);

CREATE TABLE IF NOT EXISTS hint (
    subject_type VARCHAR NOT NULL,
    subject_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    template_id VARCHAR NOT NULL,
    tier VARCHAR NOT NULL,
    text VARCHAR NOT NULL,
    links JSON,
    PRIMARY KEY (subject_type, subject_id, trade_date, template_id)
);

CREATE TABLE IF NOT EXISTS user_mark (
    code VARCHAR NOT NULL,
    marked_at TIMESTAMPTZ NOT NULL,
    mark VARCHAR NOT NULL,              -- bought / sold / ignored
    note VARCHAR,
    PRIMARY KEY (code, marked_at)
);

CREATE TABLE IF NOT EXISTS brief (
    trade_date DATE NOT NULL,
    kind VARCHAR NOT NULL,              -- premarket / close
    markdown VARCHAR NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trade_date, kind)
);

CREATE TABLE IF NOT EXISTS data_quality (
    source VARCHAR NOT NULL,
    endpoint VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL DEFAULT '',
    status VARCHAR NOT NULL,
    reason VARCHAR,
    value DOUBLE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, endpoint, trade_date, segment, status)
);

CREATE TABLE IF NOT EXISTS external_asset_map (
    sector_id VARCHAR PRIMARY KEY,
    asset_id VARCHAR,
    asset_name VARCHAR,
    direction INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS external_asset_daily (
    asset_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    close DOUBLE,
    ret DOUBLE,
    PRIMARY KEY (asset_id, trade_date)
);

CREATE TABLE IF NOT EXISTS watchlist_group (
    group_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watchlist (
    code VARCHAR NOT NULL,
    group_id VARCHAR NOT NULL DEFAULT 'default',
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (code, group_id)
);

CREATE TABLE IF NOT EXISTS alert_log (
    trade_date DATE NOT NULL,
    subject_id VARCHAR NOT NULL,
    event VARCHAR NOT NULL,
    segment VARCHAR NOT NULL DEFAULT '',
    payload JSON,
    pushed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, subject_id, event, segment)
);

CREATE TABLE IF NOT EXISTS settings (
    key VARCHAR PRIMARY KEY,
    value VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backfill_progress (
    task VARCHAR NOT NULL,              -- bars / flow_daily / sector_flow_daily
    subject_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL,            -- pending / done / failed
    last_date DATE,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task, subject_id)
);

CREATE TABLE IF NOT EXISTS job_run (
    job VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    ok BOOLEAN,
    rows INTEGER,
    error VARCHAR,
    PRIMARY KEY (job, trade_date, segment, started_at)
);
