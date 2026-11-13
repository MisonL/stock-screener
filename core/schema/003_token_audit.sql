-- 真实用量审计（第九轮预算简化）。
-- 预算表 `llm_budget` 记的是「已扣保守估算」，**不是**真实花费；
-- 真实 usage 只写这张表，二者差异是设计而不是缺陷。
-- 为什么单独一个文件：`migrate` 按文件名记录版本，往 002 里追加语句对
-- 已经部署过的库永远不会执行（`schema_version` 里已经有 002）。
CREATE TABLE IF NOT EXISTS token_audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    pool TEXT NOT NULL,
    real_in INTEGER,
    real_out INTEGER,
    estimated INTEGER NOT NULL DEFAULT 0,
    reserve_in INTEGER NOT NULL,
    reserve_out INTEGER NOT NULL,
    attempt_seq INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_token_audit_pool_day ON token_audit(pool, substr(ts,1,10));
