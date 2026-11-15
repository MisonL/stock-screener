"""第一步：快照三循环、上游请求记账与退避。"""
import json
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.clock import FixedClock
from core.db import connect, migrate
from core.models import Quote, SectorRow
from jobs.snapshot import (CHUNK, INTERVALS, LUNCH_INTERVALS, SnapshotLoops, calls_by_scope,
                           record_call)
from tests.helpers import SH, _trading_days_rev

T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)


class Clock:
    def __init__(self, t):
        self._t = t

    def now(self):
        return self._t

    def set(self, h, m):
        self._t = self._t.replace(hour=h, minute=m)
        return self


class FakeReg:
    def __init__(self, n_universe=6000, fail=False):
        self.n = n_universe
        self.fail = fail
        self.snapshot_calls = []
        self.sina = SimpleNamespace(fetch_sector_snapshot=self._sectors)

    def _sectors(self):
        return [SectorRow("new_001", "银行", 100.0, 99.0, 1.0, 1e6, None, members=12)]

    def snapshot(self, codes):
        self.snapshot_calls.append(list(codes))
        if self.fail:
            raise RuntimeError("上游挂了")
        return ({c: Quote(c, c, 10.0, 9.9, 9.9, 10.1, 9.8, 1e6, 1e7, 1.0, None, None, T0, [])
                 for c in codes}, "tencent")


@pytest.fixture
def env(tmp_path, CFG):
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    # 交易日历：到 20261029（20261030 由工作日兜底判成交易日，20261031 是周六）。
    conn.executemany(
        "INSERT INTO daily_bars VALUES ('sh000300', ?, 3000, 3000, 3000, 3000, 0, 0, 'backfill')",
        [(d,) for d in _trading_days_rev(20261029, 40)])
    conn.executemany("INSERT INTO stock_meta(code,name,market,updated_at) VALUES (?,?,'cn_main','x')",
                     [(f"sh{600000 + i}", f"票{i}") for i in range(6000)])
    conn.commit()
    clock = Clock(T0)
    reg = FakeReg()
    alerts = []
    tick_lock = threading.Lock()               # 与 tick 共享的那把锁
    loops = SnapshotLoops(CFG, reg, conn, clock, on_alert=lambda *a: alerts.append(a),
                          lock=tick_lock)
    return SimpleNamespace(conn=conn, clock=clock, reg=reg, loops=loops, alerts=alerts, cfg=CFG,
                           tick=SimpleNamespace(lock=tick_lock))


def test_calls_by_scope_zero_fills_all_keys(env):
    c = calls_by_scope(env.conn, T0)
    assert set(c) == {"pool", "sector", "market", "eod", "auction", "members", "backfill"}
    assert all(v == 0 for v in c.values())


def test_calls_by_scope_counts_last_hour_only(env):
    record_call(env.conn, env.clock, "pool")
    env.conn.execute("INSERT INTO upstream_calls(ts, scope) VALUES (?, 'backfill')",
                     ((T0 - timedelta(hours=2)).isoformat(),))
    env.conn.commit()
    c = calls_by_scope(env.conn, T0)
    assert c["pool"] == 1 and c["backfill"] == 0          # 2 小时前的不算


def test_market_loop_chunks_600_and_counts_each(env):
    assert env.loops.poll("market", T0) is True
    assert len(env.reg.snapshot_calls) == 10              # 6000 / 600
    assert [len(c) for c in env.reg.snapshot_calls] == [CHUNK] * 10
    assert calls_by_scope(env.conn, T0)["market"] == 10


def test_pool_loop_uses_one_request_and_writes_cache(env):
    env.loops.poll("pool", T0)
    assert len(env.reg.snapshot_calls) == 1
    payload = json.loads(env.conn.execute(
        "SELECT payload FROM snapshot_cache WHERE scope='pool'").fetchone()[0])
    assert payload["as_of"] == T0.isoformat() and payload["rows"]
    assert calls_by_scope(env.conn, T0)["pool"] == 1


def test_sector_loop_two_requests_semantics(env):
    env.loops.poll("sector", T0)
    # 真源一次 fetch 实发两个 HTTP（industry + concept），记 2 才是真实请求数
    assert calls_by_scope(env.conn, T0)["sector"] == 2
    payload = json.loads(env.conn.execute(
        "SELECT payload FROM snapshot_cache WHERE scope='sector'").fetchone()[0])
    assert payload["rows"][0]["members"] == 12


def test_pool_codes_include_indices_and_watch(env):
    env.conn.execute("INSERT INTO watch_stock(code, added_at) VALUES ('sz000002','x')")
    env.conn.execute("INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
                     " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean,"
                     " pos_in_box) VALUES (20261029,'cn_main','sh600519',1,80,'{}',40,10,9,10,9,9.5,1e6,0.5)")
    env.conn.commit()
    env.loops.poll("pool", T0)
    codes = set(env.reg.snapshot_calls[0])
    assert {"sh600519", "sz000002", "sh000001", "sh000300"} <= codes


def test_backoff_doubles_after_three_failures(env):
    env.reg.fail = True
    base = INTERVALS["pool"]
    # **逐次断言**：只看「N 次之后是 2 倍」会让「第 4 次直接跳到 60」也通过。
    # 口径：连续失败 3 次 → 2 倍；随后每次再翻倍，封顶 60。
    env.loops.poll("pool", T0)
    assert env.loops.interval("pool", T0) == base            # 1 次
    env.loops.poll("pool", T0)
    assert env.loops.interval("pool", T0) == base            # 2 次
    env.loops.poll("pool", T0)
    assert env.loops.interval("pool", T0) == min(base * 2, 60)   # 3 次 → 2 倍
    env.loops.poll("pool", T0)
    assert env.loops.interval("pool", T0) == min(base * 4, 60)   # 4 次 → 4 倍
    env.loops.poll("pool", T0)
    assert env.loops.interval("pool", T0) == min(base * 8, 60)
    for _ in range(6):
        env.loops.poll("pool", T0)
    assert env.loops.interval("pool", T0) == 60            # 封顶 60 秒


def test_backoff_recovers_to_base(env):
    env.reg.fail = True
    env.loops.poll("pool", T0)
    env.reg.fail = False
    env.loops.poll("pool", T0)
    assert env.loops.interval("pool", T0) == INTERVALS["pool"]


def test_single_p0_after_60s_of_failures(env):
    env.reg.fail = True
    env.loops.poll("pool", T0)
    env.clock.set(10, 1)
    env.loops.poll("pool", env.clock.now())                # 60 秒后
    env.loops.poll("pool", env.clock.now())
    assert len(env.alerts) == 1 and env.alerts[0][2] == "P0"


def test_lunch_intervals(env):
    env.clock.set(12, 0)
    assert env.loops.interval("pool", env.clock.now()) == LUNCH_INTERVALS["pool"]
    assert env.loops.interval("market", env.clock.now()) == LUNCH_INTERVALS["market"]


def test_no_polling_outside_session(env):
    # 09:00 不在交易时段：线程不会发请求（poll 本身仍可显式调用）
    assert env.loops._in_session(datetime(2026, 10, 30, 9, 0, tzinfo=SH)) is False
    # **午休不轮询**：spec §「一直在场」写明「午休 11:30–13:00 暂停」。
    # 降频继续轮询（旧行为）90 分钟打约 210 次请求，而午休期间快照字段根本不变。
    assert env.loops._in_session(datetime(2026, 10, 30, 12, 0, tzinfo=SH)) is False
    assert env.loops._in_session(datetime(2026, 10, 30, 11, 29, tzinfo=SH)) is True
    assert env.loops._in_session(datetime(2026, 10, 30, 13, 0, tzinfo=SH)) is True
    assert env.loops._in_session(datetime(2026, 10, 30, 15, 30, tzinfo=SH)) is False


# ---- Codex 复核：非交易日闸 ------------------------------------------------

SAT = datetime(2026, 10, 31, 10, 0, tzinfo=SH)          # 周六 10:00，时段窗口内但不是交易日


def _scopes_hit(env):
    return sum(len(c) for c in env.reg.snapshot_calls)


def test_poll_on_saturday_makes_no_upstream_request(env):
    """周六 10:00：三个 scope 都不得向上游发请求，也不得写快照缓存。"""
    for scope in ("pool", "sector", "market"):
        assert env.loops.poll(scope, SAT) is False
    assert env.reg.snapshot_calls == []
    assert env.conn.execute("SELECT count(*) FROM snapshot_cache").fetchone()[0] == 0
    assert sum(calls_by_scope(env.conn, SAT).values()) == 0


def test_poll_on_trading_day_still_works(env):
    assert env.loops.poll("pool", T0) is True
    assert len(env.reg.snapshot_calls) == 1


def test_in_session_gate_rejects_saturday(env):
    assert env.loops._in_session(T0) is True
    assert env.loops._in_session(SAT) is False
    assert env.loops._in_session(datetime(2026, 10, 31, 12, 0, tzinfo=SH)) is False


def test_pool_failure_still_records_request(env):
    """失败也要记账：预算表统计的是「向上游发了多少次」，不是「成功几次」。"""

    def boom(_codes):
        raise RuntimeError("上游炸了")

    env.reg.snapshot = boom
    env.loops.poll("pool", T0)
    assert calls_by_scope(env.conn, T0)["pool"] == 1


def test_snapshot_loops_share_tick_lock(env):  # noqa: D401
    """共用连接必须串行化：poll 与 tick 用同一把锁，否则事务交错会提交别人的半条写。"""
    import threading

    assert env.loops.lock is env.tick.lock


def test_recorder_skips_loop_scopes():
    """钩子只记 eod/members/auction/backfill；pool/sector/market 由循环自记，否则双计。"""
    from jobs.snapshot import make_recorder

    recorded = []

    class C:
        def execute(self, *a, **k):
            recorded.append(a[1][1])

        def commit(self):
            pass

    rec = make_recorder(C(), FixedClock(T0))
    for scope in ("pool", "sector", "market"):
        rec(scope)
    assert recorded == []
    for scope in ("eod", "members", "auction", "backfill"):
        rec(scope)
    assert recorded == ["eod", "members", "auction", "backfill"]


def test_write_cache_acquires_loops_lock(env):
    """去掉 `with self.lock` 必须变红：缓存写入与 tick 共用连接，必须串行。"""
    import threading
    import time

    started = threading.Event()
    held = threading.Event()

    def hold():
        with env.loops.lock:
            held.set()
            time.sleep(0.3)

    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(2)
    t0 = time.monotonic()
    env.loops._write_cache("pool", [], T0)
    elapsed = time.monotonic() - t0
    t.join()
    assert elapsed >= 0.2, "_write_cache 没拿锁"


def test_record_call_does_not_commit_callers_transaction(conn):
    """别人有事务在进行时，记账的 commit 会把对方半条事务一起提交（实测）。"""
    from jobs.snapshot import record_call
    from core.clock import FixedClock
    from datetime import datetime as _dt
    from tests.helpers import SH

    conn.execute("BEGIN")
    conn.execute("INSERT INTO job_state(name, value) VALUES ('half', '1')")
    assert conn.in_transaction
    record_call(conn, FixedClock(_dt(2026, 10, 30, 10, 0, tzinfo=SH)), "pool")
    assert conn.in_transaction, "记账把调用方的事务提交了"
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM upstream_calls").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM job_state WHERE name='half'").fetchone()[0] == 0


def test_record_call_commits_on_its_own(conn):
    """自己记账（没有外层事务）时必须落库，不能挂在未提交事务里丢掉。"""
    from jobs.snapshot import record_call
    from core.clock import FixedClock
    from datetime import datetime as _dt
    from tests.helpers import SH

    record_call(conn, FixedClock(_dt(2026, 10, 30, 10, 0, tzinfo=SH)), "pool")
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM upstream_calls WHERE scope='pool'").fetchone()[0] == 1


def test_lunch_poll_makes_no_upstream_request(env):
    """午休期间 poll 一次，上游请求数为 0（不是「少」，是不发）。"""
    env.clock.set(12, 0)
    before = _scopes_hit(env)
    for scope in ("pool", "sector", "market"):
        env.loops.poll(scope)
    assert _scopes_hit(env) == before, "午休还在打上游"
