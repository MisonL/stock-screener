-- 第一批（batch1）schema。表名/列名是后续所有任务的公共接口，改动等于改接口。
-- 迁移由 core.db.migrate() 按文件名顺序执行，每个文件一个事务。
-- schema_version 元表由 migrate() 自己创建（它要先读已应用版本才知道跑哪些文件），这里不再建。

CREATE TABLE daily_bars(code TEXT NOT NULL, date INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
  low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, amount REAL NOT NULL,   -- 股 / 元
  source TEXT NOT NULL, PRIMARY KEY(code, date)) WITHOUT ROWID;

CREATE TABLE corporate_actions(code TEXT NOT NULL, ex_date INTEGER NOT NULL, source TEXT NOT NULL,
  cash_per_share REAL NOT NULL DEFAULT 0, bonus_ratio REAL NOT NULL DEFAULT 0, tran_ratio REAL NOT NULL DEFAULT 0,
  implied_factor REAL,                     -- 仅 source='snapshot_implied'
  PRIMARY KEY(code, ex_date, source));     -- 同日两源并存时 westock_dividend 优先，见 Task 10

CREATE TABLE daily_bars_adj(code TEXT NOT NULL, date INTEGER NOT NULL, open REAL, high REAL, low REAL,
  close REAL, volume REAL, factor REAL NOT NULL, vol_factor REAL NOT NULL, PRIMARY KEY(code, date)) WITHOUT ROWID;

CREATE TABLE adj_state(code TEXT PRIMARY KEY, actions_hash TEXT NOT NULL, raw_max_date INTEGER NOT NULL DEFAULT 0,
  raw_count INTEGER NOT NULL DEFAULT 0,    -- 只比 max_date 会漏掉「中间补回一根」（raw 变长但最大日不变）
  raw_sum INTEGER NOT NULL DEFAULT 0,      -- 行数与日期和：两者一起足以识别集合变化
  rebuilt_at TEXT NOT NULL);               -- 见 Task 10 / D11

CREATE TABLE stock_meta(code TEXT PRIMARY KEY, name TEXT NOT NULL, market TEXT NOT NULL,
  industry TEXT, list_date INTEGER, updated_at TEXT NOT NULL);

CREATE TABLE stock_status_history(code TEXT NOT NULL, start_date INTEGER NOT NULL, end_date INTEGER,
  st_status TEXT NOT NULL CHECK(st_status IN ('normal','ST','*ST','delisting')), PRIMARY KEY(code, start_date));

CREATE TABLE sector_meta(sector TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('industry','concept')));

CREATE TABLE sector_member(sector TEXT NOT NULL, code TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(sector, code));

CREATE TABLE fetch_log(code TEXT NOT NULL, purpose TEXT NOT NULL CHECK(purpose IN ('backfill','snapshot_eod','snapshot_intraday')),
  n INTEGER, status TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY(code, purpose));  -- ok/empty/error；三种写者按 purpose 分行，互不覆盖

CREATE TABLE pool(trade_date INTEGER NOT NULL, market TEXT NOT NULL, code TEXT NOT NULL, rank INTEGER NOT NULL,
  score REAL NOT NULL, factors TEXT NOT NULL,              -- JSON：各因子原值、达成度、得分
  box_days INTEGER NOT NULL, box_high_adj REAL NOT NULL, box_low_adj REAL NOT NULL,
  box_high_raw REAL NOT NULL, box_low_raw REAL NOT NULL,   -- §4.6.9：盘中只用 raw
  close_raw REAL NOT NULL, box_vol_mean REAL NOT NULL,     -- C2 分母：剔除放量日后的横盘期 raw 均量（股/日）
  pos_in_box REAL NOT NULL, a_group_discounted INTEGER NOT NULL DEFAULT 0, b4_degenerate INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(trade_date, market, code));

CREATE TABLE watch_stock(code TEXT PRIMARY KEY, note TEXT, pct_threshold REAL, source TEXT NOT NULL DEFAULT 'manual',
  added_at TEXT NOT NULL);

CREATE TABLE signals(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, trade_date INTEGER NOT NULL, market TEXT NOT NULL,
  code TEXT NOT NULL, kind TEXT NOT NULL, level TEXT NOT NULL, event_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,                      -- 与 event_log.dedupe_key 第 4 段同源，同一个变量（D3/D4）
  payload TEXT NOT NULL, read_at TEXT);

CREATE TABLE push_log(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, code TEXT, kind TEXT NOT NULL, level TEXT NOT NULL,
  seq_id TEXT NOT NULL, segment INTEGER NOT NULL DEFAULT 1, ntfy_id TEXT,
  body TEXT,                          -- 第一段正文原文；第二段拼「原文 + 解读」用（D2）
  event_id INTEGER, signal_id INTEGER, trade_date INTEGER NOT NULL,   -- 非事件类推送填当日 Clock.today()
  status TEXT NOT NULL CHECK(status IN ('pending','accepted','failed','suppressed')), error TEXT);

CREATE INDEX idx_push_code_ts ON push_log(code, ts);
CREATE INDEX idx_push_event ON push_log(event_id);   -- 第二段按 event_id 定位第一段（D2）
CREATE INDEX idx_signals_event ON signals(event_id);     -- load_sig 每条事件按 event_id 查

CREATE TABLE app_secret(name TEXT PRIMARY KEY, value TEXT NOT NULL);   -- HMAC 密钥，首次启动生成

CREATE TABLE suspension_calendar(code TEXT NOT NULL, start_date INTEGER NOT NULL,
  end_date INTEGER, PRIMARY KEY(code, start_date));       -- 停牌日历；resume_dates 由本表算出（D24）

CREATE TABLE job_state(name TEXT PRIMARY KEY, value TEXT NOT NULL);     -- 当日 eod 是否已跑等启动恢复标记（D10）

CREATE TABLE upstream_calls(ts TEXT NOT NULL, scope TEXT NOT NULL);    -- 上游请求计数，/api/status 的近 1 小时统计读它
CREATE INDEX idx_upstream_ts ON upstream_calls(ts);

CREATE TABLE stock_stats(code TEXT PRIMARY KEY, n_bars INTEGER NOT NULL, amount_20d REAL NOT NULL,
  suspended_250 INTEGER NOT NULL, updated_at TEXT NOT NULL);   -- 粗筛用的物化字段，build_pool 第一趟读它

-- 以下四张照 spec §5.3 / §5.4.7 的 DDL 建。auction_daily 的 `date TEXT` 改 `trade_date INTEGER`。

-- 三个快照循环的共享缓存。三行，不是一行：三条循环频率不同（池内 3–5s / 板块 15s / 全市场 30s），
-- 单行会让 30 秒的全市场写覆盖掉 3 秒的池内数据。
CREATE TABLE snapshot_cache (
  scope    TEXT PRIMARY KEY CHECK (scope IN ('pool','sector','market')),
  as_of    TEXT    NOT NULL,
  payload  TEXT    NOT NULL
);

-- 各容器的存活心跳。三行，由各自进程原地 UPDATE。
CREATE TABLE service_heartbeat (
  service      TEXT PRIMARY KEY,
  last_beat_at TEXT NOT NULL,
  detail       TEXT
);

CREATE TABLE event_log (
  id           INTEGER PRIMARY KEY,
  ts           TEXT NOT NULL,
  trade_date   INTEGER NOT NULL,
  market       TEXT NOT NULL,
  kind         TEXT NOT NULL,
  scope        TEXT NOT NULL,
  target       TEXT,
  payload      TEXT NOT NULL,
  salience     REAL NOT NULL,
  escalated    INTEGER NOT NULL DEFAULT 0,   -- jobs 的 INSERT 不写该列；无默认值首写即 NOT NULL 失败
  turn_id      INTEGER,
  agent_status TEXT NOT NULL DEFAULT 'new',
  escalated_at TEXT,
  dedupe_key   TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_event_dedupe ON event_log(dedupe_key);
-- escalated 必须在这个部分索引里：心跳要能回收「已放行但回合中途崩溃」的滞留事件。
CREATE INDEX idx_event_agent ON event_log(agent_status) WHERE agent_status IN ('new','pending_retry','escalated');

CREATE TABLE auction_daily(code TEXT, trade_date INTEGER, auction_vol REAL, auction_amt REAL,
                           PRIMARY KEY(code, trade_date));
