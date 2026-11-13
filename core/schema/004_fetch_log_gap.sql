-- `fetch_log.purpose` 增加 `gap_backfill`（第十轮调研）。
-- 缺口补拉失败原本复用 `snapshot_eod` 那一行，结果是「今天的快照已入库」
-- 被改写成 error/0，事后完全看不出这只票其实成功了。
-- SQLite 不支持 ALTER CHECK，只能重建表；行数很小（每只票每种用途一行）。
-- 单独一个迁移文件的原因同 003：`migrate` 按文件名记版本，往旧文件追加语句
-- 对已部署的库永远不会执行。
CREATE TABLE fetch_log_new(
  code TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN ('backfill','snapshot_eod','snapshot_intraday','gap_backfill')),
  n INTEGER,
  status TEXT NOT NULL,
  ts TEXT NOT NULL,
  PRIMARY KEY(code, purpose)
);
INSERT INTO fetch_log_new(code, purpose, n, status, ts)
  SELECT code, purpose, n, status, ts FROM fetch_log;
DROP TABLE fetch_log;
ALTER TABLE fetch_log_new RENAME TO fetch_log;
