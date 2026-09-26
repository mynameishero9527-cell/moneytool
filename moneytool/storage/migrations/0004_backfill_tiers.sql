-- 分层回补：日线先补近半年、再补近一年、最后补满配置年数。记录每只股票已覆盖到的最早日期。
-- covered_from = 1900-01-01 表示旧版一次补满的历史（视为已补齐所有层）。

CREATE TABLE IF NOT EXISTS backfill_tier (
    task VARCHAR NOT NULL,
    subject_id VARCHAR NOT NULL,
    covered_from DATE,                  -- 已成功拉取区间的最早日期
    failures INTEGER NOT NULL DEFAULT 0, -- 连续失败次数，成功即清零；启动时清零重试
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task, subject_id)
);

INSERT OR IGNORE INTO backfill_tier (task, subject_id, covered_from)
SELECT task, subject_id, DATE '1900-01-01' FROM backfill_progress WHERE task = 'bars' AND status = 'done';
