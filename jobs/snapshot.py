"""快照三循环（spec §5.1、§6.1.2.2）与上游请求记账。

三条循环频率不同、代码集不同，共用一个 `conn`：

| scope  | 间隔 | 代码集 |
|---|---|---|
| pool   | 3s   | 最近一个已收盘交易日的池子 ∪ 自选 ∪ 5 个指数（1 个请求） |
| sector | 15s  | 新浪行业 + 概念（2 个请求） |
| market | 30s  | 全宇宙，600 只/请求分块（≈10 个请求/周期） |

**午休不跑**：11:30–13:00 把间隔拉到 60/120/120 秒（不是完全停——万一有午间公告，
下一档慢速轮询还能看见）。

**每个请求都要落 `upstream_calls`**：`/api/status` 的近 1 小时请求数与 Task 29 的
「合计 ≤ 3000」验收都读它；只记三个循环会漏掉 eod 全市场快照、缺口补拉、竞价、
成分股分页与 backfill 进程的请求。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

from core.clock import TradingCalendar, phase

SCOPES = ("pool", "sector", "market", "eod", "auction", "members", "backfill")
CHUNK = 600
# pool 循环的固定指数名单（§5.1）：大盘 4 个 + 沪深300（D1 的基准）
POOL_INDICES = ("sh000001", "sz399001", "sz399006", "sh000688", "sh000300")
# market 从 30 秒放宽到 60 秒：它每轮把**全市场 5576 只票**拉一遍，
# 每轮 10 个请求，占全天请求量的 43%，是封 IP 风险的最大来源。
# 30 秒刷新率的收益其实很小——agent 心跳最快 2 分钟一次，市场宽度
# 60 秒的滞后对判断没有实质影响（spec:2481 记录过 WAF 封禁：
# 「不是 60 秒能恢复的，代价是整条 K 线链路不可用」）。
INTERVALS = {"pool": 3, "sector": 15, "market": 60}
# 午休不轮询（`_in_session` 直接排除），这份表只剩「理论上」的兜底值
LUNCH_INTERVALS = {"pool": 60, "sector": 120, "market": 120}
FAIL_ALERT_S = 60.0            # 连续失败超过这个时长发一次 P0「数据源失效」
MAX_INTERVAL = 60


def calls_by_scope(conn, now: datetime) -> dict[str, int]:
    """近 1 小时各 scope 的请求数；**全部 scope 键都补齐**（缺的记 0）。

    生产的心跳 writer 与测试共用这一份实现——统计口径必须只有一个。
    """
    cutoff = (now - timedelta(hours=1)).isoformat()
    out = {scope: 0 for scope in SCOPES}
    for scope, n in conn.execute(
        "SELECT scope, count(*) FROM upstream_calls WHERE ts >= ? GROUP BY scope", (cutoff,)
    ):
        out[scope] = int(n)
    return out


def record_call(conn, clock, scope: str, lock=None) -> None:
    """记一次上游请求。`ts` 与写入侧同格式（`clock.now().isoformat()`），TEXT 比较才正确。

    **事务归属必须判断**：这个函数是全进程共享连接上的「顺带写」，直接 `commit()` 会把
    **别人正在进行的半条事务**一起提交（实测：eod 的 `with conn` 里发生一次钩子记账，
    eod 的半条写就被提交了）。规则：
    - 已经有事务在进行（我们是被别人调进来的）→ 只 INSERT，不 commit，跟随调用方的事务；
    - 没有事务（poller 线程自己记账）→ INSERT + commit，并优先在 `lock` 内做。
    """
    row = (clock.now().isoformat(), scope)
    if lock is not None:
        with lock:
            _record(conn, row)
        return
    _record(conn, row)


def _record(conn, row) -> None:
    in_tx = bool(getattr(conn, "in_transaction", False))
    conn.execute("INSERT INTO upstream_calls(ts, scope) VALUES (?, ?)", row)
    if not in_tx:
        conn.commit()


SECTORS_PER_FETCH = 2          # `fetch_sector_snapshot` 实发两个请求（industry + concept）

_HOOK_SCOPES = {"eod", "members", "auction", "backfill"}   # 由 Http 钩子记账的 scope


def make_recorder(conn, clock):
    """给 `Http.on_request` 用的记账闭包：**唯一 HTTP 出口每发一次就记一次**。

    各 scope 手工 `record_call` 的老做法会漏：失败出口不记、一个 flag 发两个请求记一条、
    eod/members/backfill 干脆没人记——预算表也就没法自证「≤3000/小时」。
    """

    def _record(scope: str) -> None:
        if scope not in _HOOK_SCOPES:
            return                      # pool/sector/market 由循环自己记，避免双计
        try:
            record_call(conn, clock, scope)
        except Exception:                  # noqa: BLE001 —— 记账失败不能把请求本身搞挂
            pass

    return _record


class SnapshotLoops:
    """三个快照循环。`poll(scope, now)` 是可单独调用的单次入口（测试与线程都走它）。"""

    def __init__(self, cfg, reg, conn, clock, on_pool=None, on_sector=None, on_alert=None,
                 lock=None):
        self.cfg = cfg
        self.reg = reg
        self.conn = conn
        self.clock = clock
        self.on_pool = on_pool
        self.on_sector = on_sector
        self.on_alert = on_alert
        # **与 tick 共用同一把锁**：conn 是跨线程共享的单一连接，两个线程的 `with conn`
        # 事务交错时，A 的回滚也带不走 B 已提交的半条写——实测出现过「event 有、signal 没有」。
        self.lock = lock if lock is not None else threading.RLock()
        self._fails = {scope: 0 for scope in INTERVALS}
        self._fail_since: dict[str, datetime | None] = {scope: None for scope in INTERVALS}
        self._alerted = {scope: False for scope in INTERVALS}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._cal_lock = threading.Lock()
        self._cal_day: int | None = None
        self._cal = None
        self.last_tick_at = 0.0

    # ---- 单次轮询 --------------------------------------------------------

    def poll(self, scope: str, now: datetime | None = None) -> bool:
        """跑一次该 scope 的轮询。返回是否成功（失败会计入退避）。

        **非交易日直接跳过**：隔夜/周末的定时唤醒仍会落到 `_run_scope`，不在这里挡住
        就会向上游打请求并把上一个交易日的快照覆盖成「今天」。
        """
        now = now or self.clock.now()
        if not self._is_trading_day(now):
            return False
        # **午休与非时段也要在这里挡**：线程版靠 `_run_scope` 的 `_in_session` 闸，
        # 但 `poll()` 是可以被显式调用的（测试、手工补拉），只挡非交易日会让
        # 午休期间照样打上游。
        if not self._in_session(now):
            return False
        try:
            if scope == "pool":
                self._poll_pool(now)
            elif scope == "sector":
                self._poll_sector(now)
            elif scope == "market":
                self._poll_market(now)
            else:
                raise ValueError(scope)
        except Exception:                      # noqa: BLE001 —— 单次失败不能让循环线程死掉
            self._note_failure(scope, now)
            return False
        self._fails[scope] = 0
        self._fail_since[scope] = None
        self._alerted[scope] = False
        self.last_tick_at = now.timestamp()
        return True

    def interval(self, scope: str, now: datetime) -> int:
        """当前间隔（秒）：午休拉长、连续失败翻倍封顶 60。"""
        base = (LUNCH_INTERVALS if phase(now.time()) == "lunch" else INTERVALS)[scope]
        n = self._fails[scope]
        if n >= 3:
            base = min(base * (2 ** (n - 2)), MAX_INTERVAL)
        return base

    def _note_failure(self, scope: str, now: datetime) -> None:
        self._fails[scope] += 1
        if self._fail_since[scope] is None:
            self._fail_since[scope] = now
            return
        elapsed = (now - self._fail_since[scope]).total_seconds()
        if elapsed >= FAIL_ALERT_S and not self._alerted[scope]:
            self._alerted[scope] = True           # 30 分钟不重复由 Task 23 的 dedupe 兜底
            if self.on_alert is not None:
                self.on_alert("upstream_dead", f"{scope} 循环连续失败 {int(elapsed)} 秒", "P0")

    # ---- 各 scope --------------------------------------------------------

    def _poll_pool(self, now: datetime) -> None:
        codes = self._pool_codes()
        try:
            quotes, used = self.reg.snapshot(codes)
        finally:
            # 失败也要记：预算表是「实际向上游发了多少」，不是「成功了多少」
            record_call(self.conn, self.clock, "pool")
        rows = [self._quote_row(q, now) for q in quotes.values()]
        self._write_cache("pool", rows, now, source=used)
        if self.on_pool is not None:
            self.on_pool(quotes, now)

    def _poll_sector(self, now: datetime) -> None:
        try:
            rows = self.reg.sina.fetch_sector_snapshot()
        finally:
            for _ in range(SECTORS_PER_FETCH):
                record_call(self.conn, self.clock, "sector")
        payload = [{"code": r.code, "name": r.name, "price": r.price, "prev_close": r.prev_close,
                    "chg": r.chg, "volume": r.volume, "members": r.members} for r in rows]
        self._write_cache("sector", payload, now, source="sina")
        if self.on_sector is not None:
            self.on_sector(rows, now)

    def _poll_market(self, now: datetime) -> None:
        codes = self._universe_codes()
        quotes = {}
        source = None
        for start in range(0, len(codes), CHUNK):
            part = codes[start:start + CHUNK]
            try:
                chunk, used = self.reg.snapshot(part)         # 每块一个请求：10 块 = 10 次
            finally:
                record_call(self.conn, self.clock, "market")
            quotes.update(chunk)
            source = used
        rows = [self._quote_row(q, now) for q in quotes.values()]
        self._write_cache("market", rows, now, source=source)

    # ---- 数据来源 --------------------------------------------------------

    def _pool_codes(self) -> list[str]:
        today = int(self.clock.now().strftime("%Y%m%d"))
        row = self.conn.execute(
            "SELECT max(trade_date) FROM pool WHERE trade_date < ?", (today,)).fetchone()
        latest = row[0] if row else None
        codes = [r[0] for r in self.conn.execute(
            "SELECT code FROM pool WHERE trade_date = ?", (latest,))] if latest else []
        codes += [r[0] for r in self.conn.execute("SELECT code FROM watch_stock")]
        codes += list(POOL_INDICES)
        return sorted(set(codes))

    def _universe_codes(self) -> list[str]:
        return sorted(r[0] for r in self.conn.execute("SELECT code FROM stock_meta"))

    @staticmethod
    def _quote_row(q, now: datetime) -> dict:
        return {"code": q.code, "name": q.name, "price": q.price, "volume": q.volume,
                "prev_close": q.prev_close, "open": q.open, "high": q.high, "low": q.low,
                "amount": q.amount, "volume_ratio": q.volume_ratio,
                "up": q.limit_up, "down": q.limit_down,
                "ts": q.ts.isoformat() if isinstance(q.ts, datetime) else q.ts}

    def _write_cache(self, scope: str, rows: list[dict], now: datetime, source=None) -> None:
        body = json.dumps({"as_of": now.isoformat(), "source": source, "rows": rows},
                          ensure_ascii=False)
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES (?,?,?)",
                (scope, now.isoformat(), body),
            )

    # ---- 线程 ------------------------------------------------------------

    def start(self) -> None:
        for scope in INTERVALS:
            t = threading.Thread(target=self._run_scope, args=(scope,), name=f"snap-{scope}",
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def _run_scope(self, scope: str) -> None:
        while not self._stop.is_set():
            try:
                now = self.clock.now()
                if self._in_session(now):
                    self.poll(scope, now)
            except Exception as exc:              # noqa: BLE001 —— 线程不能死
                if self.on_alert is not None:
                    self.on_alert("snapshot_crash", f"{scope} 循环异常：{exc}"[:200], "P0")
            self._stop.wait(self.interval(scope, self.clock.now()))

    def _is_trading_day(self, now: datetime) -> bool:
        today = int(now.strftime("%Y%m%d"))
        if self._cal_day == today and self._cal is not None:
            return self._cal.is_trading_day(today)
        # **先构造再一次性发布**：先写 day 后写 cal 的写法下，另一线程会在两步之间
        # 看到 day 已更新、cal 还是 None，`None.is_trading_day` 直接抛，把循环线程打死
        with self._cal_lock:
            if self._cal_day != today or self._cal is None:
                self._cal = TradingCalendar(self.conn)
                self._cal_day = today
            cal = self._cal
        return cal.is_trading_day(today)

    def _in_session(self, now: datetime) -> bool:
        """是否该轮询。

        **午休 11:30–13:00 不轮询**（spec §「一直在场」：「午休 11:30–13:00 暂停」）。
        旧实现只放宽间隔继续拉，90 分钟约 210 次请求，而午休期间快照字段
        （价格、量、额）根本不变——纯浪费，还抬高封 IP 风险。
        """
        hm = now.strftime("%H:%M")
        if "11:30" <= hm < "13:00":
            return False
        return "09:14" <= hm <= "15:05" and self._is_trading_day(now)
