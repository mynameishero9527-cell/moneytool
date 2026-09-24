-- 计算链 ⑤–⑦：个股画像、跟踪闭环扩展、快照成交额。
-- DuckDB 对带主键的表 ADD COLUMN 后 INSERT OR REPLACE 会失败，这里一律重建表（建新表 → 拷贝 → 删旧 → 改名）。

-- 东财「今日」累计的成交额（主力净额 / 主力净占比 反推），收盘日线未到时作为比例分母的近似成交额。
CREATE TABLE flow_snapshot_new (
    subject_type VARCHAR NOT NULL,
    subject_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    net_main DOUBLE, net_super DOUBLE, net_large DOUBLE, net_medium DOUBLE, net_small DOUBLE,
    pct_chg DOUBLE,
    close DOUBLE,
    amount DOUBLE,
    PRIMARY KEY (subject_type, subject_id, trade_date, segment)
);
INSERT INTO flow_snapshot_new
SELECT subject_type, subject_id, trade_date, segment, captured_at,
       net_main, net_super, net_large, net_medium, net_small, pct_chg, close, NULL
FROM flow_snapshot;
DROP TABLE flow_snapshot;
ALTER TABLE flow_snapshot_new RENAME TO flow_snapshot;

-- 个股每日画像：身份标签（8.6）、排除标签（8.10）、不可交易过滤、结构分（8.7，按主依据板块）。
CREATE TABLE IF NOT EXISTS stock_profile (
    code VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL DEFAULT 'close',
    param_version VARCHAR NOT NULL,
    basis_sector VARCHAR,
    identity JSON,
    exclusions JSON,
    tradable BOOLEAN NOT NULL DEFAULT TRUE,
    score INTEGER,
    score_tier VARCHAR,
    score_components JSON,
    PRIMARY KEY (code, trade_date, segment, param_version)
);

-- 跟踪闭环（8.8）：按名单类型与参数版本分开跟踪，记录入选价与入选时板块阶段。
CREATE TABLE tracking_new (
    list_type VARCHAR NOT NULL DEFAULT 'buy',
    code VARCHAR NOT NULL,
    sector_id VARCHAR NOT NULL,
    entered_date DATE NOT NULL,
    param_version VARCHAR NOT NULL,
    entered_price DOUBLE,
    entered_stage VARCHAR,
    exited_date DATE,
    exit_reason VARCHAR,
    PRIMARY KEY (list_type, code, sector_id, entered_date, param_version)
);
INSERT INTO tracking_new (code, sector_id, entered_date, param_version, exited_date, exit_reason)
SELECT code, sector_id, entered_date, 'v1', exited_date, exit_reason FROM tracking;
DROP TABLE tracking;
ALTER TABLE tracking_new RENAME TO tracking;

-- 事后统计（13.3）：主键纳入参数版本，便于跨版本对比。
CREATE TABLE label_outcome_new (
    list_type VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    sector_id VARCHAR NOT NULL,
    entered_date DATE NOT NULL,
    horizon INTEGER NOT NULL,
    param_version VARCHAR NOT NULL,
    point_type VARCHAR,
    entered_stage VARCHAR,
    ret DOUBLE,
    excess_vs_sector DOUBLE,
    excess_vs_eqw DOUBLE,
    max_drawdown DOUBLE,
    risk_gate BOOLEAN,
    PRIMARY KEY (list_type, code, sector_id, entered_date, horizon, param_version)
);
INSERT INTO label_outcome_new
SELECT list_type, code, sector_id, entered_date, horizon, param_version, NULL, NULL,
       ret, excess_vs_sector, excess_vs_eqw, max_drawdown, risk_gate
FROM label_outcome;
DROP TABLE label_outcome;
ALTER TABLE label_outcome_new RENAME TO label_outcome;

-- 模板提示（9.7）：与阶段结果一样按 segment + param_version 快照。
CREATE TABLE hint_new (
    subject_type VARCHAR NOT NULL,
    subject_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    segment VARCHAR NOT NULL DEFAULT 'close',
    param_version VARCHAR NOT NULL,
    template_id VARCHAR NOT NULL,
    tier VARCHAR NOT NULL,
    text VARCHAR NOT NULL,
    links JSON,
    PRIMARY KEY (subject_type, subject_id, trade_date, segment, param_version, template_id)
);
INSERT INTO hint_new (subject_type, subject_id, trade_date, param_version, template_id, tier, text, links)
SELECT subject_type, subject_id, trade_date, 'v1', template_id, tier, text, links FROM hint;
DROP TABLE hint;
ALTER TABLE hint_new RENAME TO hint;
