-- 待补缺口清单（第十二轮）。
-- `jobs/eod.py` 的 `GAP_MAX` 上限会把超出的缺口推迟，但旧实现只记了个内存计数：
-- 今天的 bar 照样追加 → 次日 `last_date` 已是昨天、eod 认为没有缺口；
-- 而 `backfill` 的默认 resume 按 `fetch_log(backfill, ok)` 跳过，这只票永远不会被补。
-- 历史空洞就这样永久留在 raw/adj 里（实测：205 只里 5 只失败后两条续跑路径都补不回）。
CREATE TABLE gap_pending(
  code TEXT PRIMARY KEY,
  missing_through INTEGER NOT NULL,   -- 缺口**目标日**：这一天没拿到 bar（见 006 的语义修正）
  first_seen TEXT NOT NULL,           -- ISO 时间戳，便于排序与排障
  attempts INTEGER NOT NULL DEFAULT 0 -- 已尝试补拉次数（避免无限重试同一只）
);
