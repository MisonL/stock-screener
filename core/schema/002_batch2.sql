-- 第二批（batch2）schema：agent 的工作日志、记忆、闹钟、对话与双池日预算。
-- 迁移由 core.db.migrate() 按文件名顺序执行，每个文件一个事务。
--
-- **本文件不含任何 `ALTER TABLE push_log`**：body / event_id / signal_id / trade_date 已由 001 建好（B1/D2）。
-- `agent_turn` 的列照 spec §7.6.1，另加 market / pending_action / pending_status。

CREATE TABLE agent_turn(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  wake_kind TEXT NOT NULL,            -- event / heartbeat / alarm / auction / open / close / chat
  trigger_id INTEGER,                 -- 对应 event_log.id（心跳为空）
  context_digest TEXT NOT NULL,
  context_chars INTEGER,
  context_truncated INTEGER NOT NULL DEFAULT 0,
  tool_calls TEXT,                    -- JSON：[{name, args, ms}]；稳态下事件回合应为空数组
  rounds INTEGER NOT NULL,
  decision TEXT NOT NULL,             -- speak / silent / invalid / timeout / error / deduped / gave_up / shadow
  why TEXT,                           -- 沉默的一句话理由；说话时的判断依据（这张表的灵魂）
  level TEXT,                         -- P0/P1/P2（说话时）
  push_seq TEXT,                      -- ntfy X-Sequence-ID（任意字符串）
  schedule_next INTEGER,
  latency_ms INTEGER,
  tokens_in INTEGER,
  tokens_out INTEGER,
  fallback INTEGER NOT NULL,          -- 0=正常 1=重试后成功 2=模板降级
  market TEXT NOT NULL,
  -- **唯一格式**：JSON 串 {"tool": ..., "args": {...}}；回调要据此执行写，描述文本无法还原工具与参数
  pending_action TEXT,
  -- 与 spec §5.4.5「把 pending_action 改成 expired」的落法差异（X4）：状态单独一列，动作内容不动
  pending_status TEXT CHECK(pending_status IN ('pending','confirmed','expired'))
);
CREATE INDEX idx_turn_ts ON agent_turn(ts);

CREATE TABLE agent_turn_archive(      -- 90 天前的回合移到这里（不删，§7.6.5）；显式 DDL，不用 CTAS
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  wake_kind TEXT NOT NULL, trigger_id INTEGER, context_digest TEXT NOT NULL, context_chars INTEGER,
  context_truncated INTEGER NOT NULL DEFAULT 0, tool_calls TEXT,
  rounds INTEGER NOT NULL, decision TEXT NOT NULL, why TEXT, level TEXT,
  push_seq TEXT, schedule_next INTEGER, latency_ms INTEGER,
  tokens_in INTEGER, tokens_out INTEGER, fallback INTEGER NOT NULL,
  market TEXT NOT NULL, pending_action TEXT,
  pending_status TEXT CHECK(pending_status IN ('pending','confirmed','expired')));

CREATE TABLE agent_schedule(
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, due_at TEXT NOT NULL, market TEXT NOT NULL,
  target TEXT, note TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','fired','cancelled')),
  fired_turn INTEGER);

CREATE TABLE llm_budget(
  day INTEGER NOT NULL,                -- 自然日 YYYYMMDD（周末/节假日对话同样算当天的钱）
  pool TEXT NOT NULL CHECK(pool IN ('agent','chat')),
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, pool)) WITHOUT ROWID;

CREATE TABLE chat_session(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, title TEXT);

CREATE TABLE chat_message(
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES chat_session(id),
  ts TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, tool_calls TEXT, grounded INTEGER);

CREATE TABLE agent_memory (
  id             INTEGER PRIMARY KEY,
  created_at     TEXT NOT NULL,
  expires_at     TEXT,                 -- NULL = 长期；judgment 默认 5 个交易日
  scope          TEXT NOT NULL,        -- stock / sector / market / user_pref
  target         TEXT,
  kind           TEXT NOT NULL,        -- judgment / outcome / feedback
  content        TEXT NOT NULL,
  confidence     REAL,
  superseded_by  INTEGER,              -- 被哪条新记忆推翻（不删旧，标失效）
  source_turn_id INTEGER REFERENCES agent_turn(id)
);
CREATE INDEX idx_memory_target ON agent_memory(scope, target, created_at);
