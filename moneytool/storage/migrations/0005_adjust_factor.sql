-- 后复权因子历史：每只股票取一次即可复用，补更早的日线层时不必重复请求 Baostock。
-- effective_date = 1900-01-01 的哨兵行表示「已取过，期间无除权事件」（因子 1.0 本身也正确）。
CREATE TABLE IF NOT EXISTS adjust_factor (
    code            VARCHAR NOT NULL,
    effective_date  DATE    NOT NULL,
    adj_factor      DOUBLE,
    updated_at      TIMESTAMP DEFAULT now(),
    PRIMARY KEY (code, effective_date)
);

-- 已有日线的股票：历史因子尚未单独留存，先放哨兵行之外的空集，
-- 由回补线程在下次取该股时补齐（首次取即从上市起全量）。
