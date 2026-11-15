"""交易时段主循环的四类唤醒。

**必须让位事件回合、开盘回合必须判重、闹钟必须先认领**——这三条各有一个「缺陷版本
仍然看起来能用」的失败模式，所以每一条都单独钉住。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.context import Rendered
from agent.main import AgentMain, in_session, should_wake
from agent.schedule import next_heartbeat
from core.clock import FixedClock
from core.config import load_config
from core.db import connect, migrate
from llm.budget import Budget
from llm.tools import ToolRegistry
from tests.helpers import SH

CFG = load_config(Path("config"))
DAY = 20261030
T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)


class FakeLlm:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.sent = []

    async def chat(self, messages, tools=None, max_tokens=None, timeout_s=None,
                   deduct=None, est_in=0, on_attempt=None):
        # 模仿真实 client：先扣额度再发请求（扣不动就不发）
        if deduct is not None and not deduct(est_in, max_tokens):
            from llm.client import BudgetExceeded

            raise BudgetExceeded("预算不足")
        self.sent.append(messages)
        step = self.script.pop(0) if self.script else _silent()
        return step


def _silent(why="无事"):
    from tests.agent.test_loop import Step, ToolCall

    return Step(tool_calls=[ToolCall("emit_decision",
                                     {"speak": False, "body": "", "why_silent": why})])


class FakePusher:
    def __init__(self):
        self.open_second_segment_calls = 0
        self.second_segment_calls = 0
        self.agent_note_calls = 0

    def second_segment(self, *a):
        self.second_segment_calls += 1
        return "p1"

    def open_second_segment(self, *a):
        self.open_second_segment_calls += 1
        return "p2"

    def agent_note(self, *a, **k):
        self.agent_note_calls += 1
        return "p3"

    def confirm_card(self, *a):
        return "p4"


def _cfg(shadow: bool = False):
    """生产配置，只显式钉住 `agent.attention.shadow_mode`（生产默认是 true）。

    本文件测的是**正常路径**（回合真的排队、真的推送），所以 fixture 必须显式关掉影子；
    「影子期不跑模型、不推送」由 `test_shadow_and_drill.py` 单独覆盖。
    """
    import yaml

    from core.config import Config

    agent = yaml.safe_load(Path("config/agent.yaml").read_text(encoding="utf-8"))
    agent.setdefault("attention", {})["shadow_mode"] = shadow
    return Config(CFG._strategy_raw, CFG._markets_raw, CFG.notify._d, agent, None)


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "m.db")
    migrate(conn)
    _seed_calendar(conn)
    clock = FixedClock(T0)
    llm = FakeLlm()
    pusher = FakePusher()
    from agent.loop import Deps

    cfg = _cfg(shadow=False)
    deps = Deps(conn=conn, cfg=cfg, clock=clock, llm=llm,
                budget=Budget(conn, "agent", cfg.llm.budget.agent),
                tools=ToolRegistry(conn, cfg, clock, mcp=None),
                render=lambda t: Rendered("…", 1, False, {}),
                pusher=pusher, memory=None, westock=None)
    deps.conn_factory = lambda: conn
    deps.alert = lambda *a, **k: None
    main = AgentMain(deps, clock)
    return SimpleNamespace(conn=conn, clock=clock, llm=llm, pusher=pusher, deps=deps,
                           main=main, cfg=cfg)


def _seed_calendar(conn):
    for i in range(60):
        d = 20261001 + i
        conn.execute("INSERT OR IGNORE INTO daily_bars VALUES"
                     " ('sh000300', ?, 3000,3000,3000,3000,0,0,'x')", (d,))
    conn.commit()


def _escalated(env, *, minutes_ago=0, eid=1, market="cn_main"):
    when = env.clock.now() - timedelta(minutes=minutes_ago)
    env.conn.execute(
        "INSERT INTO event_log(id, ts, trade_date, market, kind, scope, target, payload,"
        " salience, agent_status, escalated, escalated_at, dedupe_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, when.isoformat(), DAY, market, "index_move", "market", "sh000001",
         json.dumps({"code": "sh000001"}), 0.7, "escalated", 1, when.isoformat(), f"k{eid}"))
    env.conn.commit()


def _pending_retry(env, eid=2):
    env.conn.execute(
        "INSERT INTO event_log(id, ts, trade_date, market, kind, scope, target, payload,"
        " salience, agent_status, escalated, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, env.clock.now().isoformat(), DAY, "cn_main", "index_move", "market", "sh000001",
         "{}", 0.7, "pending_retry", 0, f"kr{eid}"))
    env.conn.commit()


# ---- 时段与交易日闸 ------------------------------------------------------

def test_in_session_bounds():
    def hm(h, m):
        return T0.replace(hour=h, minute=m)

    assert in_session(hm(9, 15)) is True
    assert in_session(hm(15, 5)) is True
    assert in_session(hm(9, 14)) is False
    assert in_session(hm(15, 6)) is False
    assert in_session(hm(12, 0)) is False


def test_should_wake_uses_is_trading_day(env):
    assert should_wake(env.conn, T0) is True
    # 日历里没有这一天 → 休市（节假日/周末走同一条判断）
    holiday = T0.replace(month=11, day=1)
    assert should_wake(env.conn, holiday) is False


async def test_startup_in_session_enters_loop(env):
    """10:30 启动 → 立即 poll（架构 F15），不睡到 13:00。"""
    await env.main._once()
    assert env.llm.sent != []                     # 心跳回合已经跑过


async def test_out_of_session_no_llm_calls(env):
    """23:00 不调任何 LLM。"""
    assert should_wake(env.conn, datetime(2026, 10, 30, 23, 0, tzinfo=SH)) is False


# ---- 心跳家务 ------------------------------------------------------------

def test_escalated_recycled_after_30min(env):
    _escalated(env, minutes_ago=31, eid=1)
    _escalated(env, minutes_ago=29, eid=2)
    env.main.heartbeat_housekeeping()
    rows = dict(env.conn.execute("SELECT id, agent_status FROM event_log").fetchall())
    assert rows[1] == "pending_retry" and rows[2] == "escalated"


def test_confirm_cards_expire_after_24h(env):
    env.conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback,"
        " market, pending_action, pending_status) VALUES (?,?,?,?,?,?,?,?,?)",
        ((env.clock.now() - timedelta(hours=25)).isoformat(), "event", "d", 1, "speak", 0,
         "cn_main", json.dumps({"tool": "mcp_add_watch", "args": {}}), "pending"))
    env.conn.commit()
    env.main.heartbeat_housekeeping()
    row = env.conn.execute("SELECT pending_status FROM agent_turn").fetchone()
    assert row[0] == "expired"


def test_close_sweeps_pending_retry(env):
    """15:00 收尾把残留的 pending_retry 全置 gave_up，各写一条 agent_turn。

    心跳（唯一出口）在盘后不排，不清扫就隔夜滞留——次日 poll 只取当天，没人再管。
    """
    _pending_retry(env, eid=1)
    _pending_retry(env, eid=2)
    env.main.close_sweep()
    statuses = [r[0] for r in env.conn.execute("SELECT agent_status FROM event_log").fetchall()]
    assert statuses == ["gave_up", "gave_up"]
    rows = env.conn.execute("SELECT decision, why FROM agent_turn").fetchall()
    assert len(rows) == 2 and all(r[0] == "gave_up" and r[1] == "收盘未接手" for r in rows)


# ---- 闹钟 ----------------------------------------------------------------

async def test_due_alarm_runs_and_marks_fired_and_fired_turn(env):
    env.conn.execute(
        "INSERT INTO agent_schedule(created_at, due_at, market, target, note, status)"
        " VALUES (?,?,?,?,?,'pending')",
        ((env.clock.now() - timedelta(minutes=1)).isoformat(),
         (env.clock.now() - timedelta(minutes=1)).isoformat(), "cn_main", "sh600519", "看回落"))
    env.conn.commit()
    await env.main._once()
    row = env.conn.execute("SELECT status, fired_turn FROM agent_schedule").fetchone()
    assert row[0] == "fired" and row[1] is not None
    kinds = [r[0] for r in env.conn.execute("SELECT wake_kind FROM agent_turn").fetchall()]
    assert "alarm" in kinds
    turn_id = row[1]
    assert env.conn.execute("SELECT 1 FROM agent_turn WHERE id=?", (turn_id,)).fetchone() is not None


async def test_alarm_not_refired_on_next_loop(env):
    """60 秒的闹钟回合期间，2 秒后的下一轮不能把同一闹钟再取出一次。"""
    env.conn.execute(
        "INSERT INTO agent_schedule(created_at, due_at, market, note, status)"
        " VALUES (?,?,?,?,'pending')",
        ((env.clock.now() - timedelta(minutes=1)).isoformat(),
         (env.clock.now() - timedelta(minutes=1)).isoformat(), "cn_main", "看"))
    env.conn.commit()
    await env.main._once()
    env.llm.script = [_silent()]
    await env.main._once()
    n = env.conn.execute("SELECT count(*) FROM agent_turn WHERE wake_kind='alarm'").fetchone()[0]
    assert n == 1


# ---- 开盘回合 ------------------------------------------------------------

async def test_open_turn_enqueued_once_at_0926(env):
    from tests.agent.test_loop import Step, ToolCall

    def _emit():
        return Step(tool_calls=[ToolCall("emit_decision", {
            "speak": True, "body": "开盘（get_quote.data.price）",
            "direction": "none", "title": "开盘简报"})])

    env.clock._now = datetime(2026, 10, 30, 9, 25, tzinfo=SH)
    env.llm.script = [_silent()]
    await env.main._once()
    assert env.conn.execute("SELECT count(*) FROM agent_turn WHERE wake_kind='open'"
                            ).fetchone()[0] == 0

    env.clock._now = datetime(2026, 10, 30, 9, 26, tzinfo=SH)
    env.llm.script = [_emit()]
    await env.main._once()
    assert env.conn.execute("SELECT count(*) FROM agent_turn WHERE wake_kind='open'"
                            ).fetchone()[0] == 1
    assert env.pusher.open_second_segment_calls == 1

    env.clock._now = datetime(2026, 10, 30, 9, 27, tzinfo=SH)
    env.llm.script = [_silent()]
    await env.main._once()
    assert env.conn.execute("SELECT count(*) FROM agent_turn WHERE wake_kind='open'"
                            ).fetchone()[0] == 1        # 不重复


# ---- pending_retry 只走心跳 ----------------------------------------------

async def test_pending_retry_only_in_heartbeat_turn(env):
    _pending_retry(env, eid=5)
    # 先占掉开盘回合（否则 10:00 这一轮会先排一个 open 回合，把计数搅乱）
    env.conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback, market)"
        " VALUES (?,?,?,?,?,?,?)",
        (env.clock.now().isoformat(), "open", "d", 1, "silent", 0, "cn_main"))
    env.conn.commit()
    before = env.conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0]
    env.main.next_heartbeat_at = env.clock.now() + timedelta(hours=1)   # 心跳还没到
    await env.main._once()
    assert env.conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0] == before

    env.main.next_heartbeat_at = env.clock.now() - timedelta(seconds=1)
    env.llm.script = [_silent()]
    await env.main._once()
    rows = env.conn.execute(
        "SELECT wake_kind, decision FROM agent_turn ORDER BY id DESC LIMIT 1").fetchone()
    assert rows[0] == "heartbeat" and rows[1] == "silent"


# ---- 预算标记 ------------------------------------------------------------

def test_budget_exhausted_flag_and_reset(env):
    env.main.budget_exhausted = True
    env.conn.execute("INSERT OR REPLACE INTO service_heartbeat(service, last_beat_at, detail)"
                     " VALUES ('agent', ?, ?)",
                     (env.clock.now().isoformat(),
                      json.dumps({"budget_exhausted": env.main.budget_exhausted})))
    env.conn.commit()
    row = env.conn.execute("SELECT detail FROM service_heartbeat WHERE service='agent'").fetchone()
    assert json.loads(row[0])["budget_exhausted"] is True
    # 换日 → 重置
    env.clock._now = datetime(2026, 10, 31, 10, 0, tzinfo=SH)
    env.main._last_day = DAY
    asyncio.get_event_loop_policy()
    env.llm.script = [_silent()]
    asyncio.run(env.main._once())
    assert env.main.budget_exhausted is False
