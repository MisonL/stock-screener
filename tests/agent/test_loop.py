"""Task 10：回合循环（整回合硬截止）与工作日志。用回放脚本的假客户端。"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.context import Rendered
from agent.decision import EMIT_DECISION_TOOL, TurnDecision
from agent.journal import record
from agent.loop import Deps, Trigger, run_with_deadline
from core.clock import FixedClock
from core.config import load_config
from core.db import connect, migrate
from llm.budget import Budget
from llm.tools import ToolRegistry
from tests.helpers import SH

CFG = load_config(Path("config"))
T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)


# ---- 假体 ----------------------------------------------------------------

class Step:
    def __init__(self, content="", tool_calls=None, delay=0.0, exc=None, usage=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.delay = delay
        self.exc = exc
        self.usage = usage or SimpleNamespace(prompt_tokens=10, completion_tokens=5)


class ToolCall:
    """既支持属性访问也支持 dict 访问：`_name/_raw_args` 两种都走。"""

    def __init__(self, name, args):
        self.id = name
        self.name = name
        self.function = SimpleNamespace(name=name,
                                        arguments=json.dumps(args, ensure_ascii=False))

    def get(self, key, default=None):
        return getattr(self, key, default)


class FakeLlm:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.sent = []

    async def chat(self, messages, tools=None, max_tokens=None, timeout_s=None,
                   deduct=None, est_in=0, on_attempt=None):
        self.sent.append((messages, tools, max_tokens))
        step = self.script.pop(0) if self.script else Step(content="")
        if step.delay:
            await asyncio.sleep(step.delay)
        if step.exc is not None:
            raise step.exc
        return SimpleNamespace(content=step.content, tool_calls=step.tool_calls,
                               attempts=1, usage=step.usage)


class FakePusher:
    def __init__(self):
        self.second_segment_calls = 0
        self.open_second_segment_calls = 0
        self.agent_note_calls = 0
        self.confirm_card_calls = 0
        self.agent_note_args = []
        self.grounded = []

    def second_segment(self, event_id, decision, grounded):
        self.second_segment_calls += 1
        self.grounded.append(grounded)
        return "push-1"

    def open_second_segment(self, trade_date, decision, grounded):
        self.open_second_segment_calls += 1
        self.grounded.append(grounded)
        return "push-2"

    def agent_note(self, decision, grounded, code=None, target=None):
        self.agent_note_calls += 1
        self.grounded.append(grounded)
        self.agent_note_args.append((code, target))
        return "push-3"

    def confirm_card(self, turn_id, pending_action, grounded):
        self.confirm_card_calls += 1
        self.grounded.append(grounded)
        return "push-4"


class MemoryStub:
    def recall(self, conn, target, now):
        return []

    def write(self, *a, **k):
        return 1

    def supersede(self, *a, **k):
        pass


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "l.db")
    migrate(c)
    return c


def deps_for(conn, script=None, pusher=None, rendered=None):
    clock = FixedClock(T0)
    return Deps(conn=conn, cfg=CFG, clock=clock, llm=FakeLlm(script),
                budget=Budget(conn, "agent", CFG.llm.budget.agent),
                tools=ToolRegistry(conn, CFG, clock, mcp=None),
                render=rendered or (lambda t: Rendered("…", 1, False, {})),
                pusher=pusher if pusher is not None else FakePusher(),
                memory=MemoryStub(), westock=None)


def emit(speak=True, body=None, why_silent=None, **extra):
    args = {"speak": speak, "body": body if body is not None else ("现价 32.90（ctx.event.price）" if speak else "")}
    if why_silent is not None:
        args["why_silent"] = why_silent
    args.update(extra)
    return Step(tool_calls=[ToolCall("emit_decision", args)])


def text_only(text):
    return Step(content=text)


def event_trigger(deadline=25):
    class _Ev:
        id = 1
        kind = "breakout"
        scope = "stock"
        target = "sh600519"
        agent_status = "escalated"

    group = SimpleNamespace(channel="A", id="g1", market="cn_main", events=[_Ev()])
    return Trigger(wake_kind="event", group=group, events=group.events, channel="A",
                   market="cn_main", rounds_budget=3, deadline_s=deadline)


def hb_trigger(deadline=60):
    return Trigger(wake_kind="heartbeat", events=[], channel=None, market="cn_main",
                   rounds_budget=6, deadline_s=deadline)


async def run(conn, trigger=None, script=None, deadline=None, pusher=None, rendered=None):
    d = deps_for(conn, script, pusher, rendered)
    tr = trigger or event_trigger()
    if deadline is not None:
        tr = dataclasses.replace(tr, deadline_s=deadline)
    return await run_with_deadline(tr, d)


# ---- 回合基本行为 --------------------------------------------------------

async def test_one_round_when_model_emits_directly(conn):
    r = await run(conn, script=[emit(speak=True)])
    assert r.rounds == 1 and r.tool_calls == []
    assert r.decision.kind == "speak"


async def test_emit_decision_tool_in_schema(conn):
    d = deps_for(conn, script=[emit(speak=True)])
    await run_with_deadline(event_trigger(), d)
    sent_tools = d.llm.sent[0][1]
    assert any(t["function"]["name"] == "emit_decision" for t in sent_tools)
    assert sent_tools[-1] is EMIT_DECISION_TOOL


async def test_no_tool_calls_is_invalid(conn):
    assert (await run(conn, script=[text_only("好的")])).decision.kind == "invalid"


async def test_deadline_returns_timeout_with_rendered(conn):
    """超时回合仍带 rendered（wait_for 取消后 run_turn 局部变量全丢）。"""
    r = await run(conn, script=[Step(delay=30)], deadline=0.2)
    assert r.decision.kind == "timeout" and r.rendered.chars > 0


async def test_retries_exhausted_maps_to_timeout(conn):
    from llm.client import RetriesExhausted

    exc = RetriesExhausted("502", attempts=3)
    r = await run(conn, script=[Step(exc=exc)])
    assert r.decision.kind == "timeout"


async def test_non_retryable_maps_to_error(conn):
    from llm.client import NonRetryable

    r = await run(conn, script=[Step(exc=NonRetryable(400, "bad"))])
    assert r.decision.kind == "error"


async def test_event_turn_pushes_second_segment(conn):
    p = FakePusher()
    await run(conn, trigger=event_trigger(), script=[emit(speak=True)], pusher=p)
    assert p.second_segment_calls == 1 and p.agent_note_calls == 0


async def test_heartbeat_turn_pushes_agent_note(conn):
    p = FakePusher()
    await run(conn, trigger=hb_trigger(), script=[emit(speak=True)], pusher=p)
    assert p.agent_note_calls == 1 and p.agent_note_args == [(None, None)]


async def test_silent_turn_does_not_crash_grounding(conn):
    r = await run(conn, script=[emit(speak=False, why_silent="无异常")])
    assert r.decision.kind == "silent"


async def test_single_round_tool_calls_capped_at_6(conn):
    """一轮 20 个 tool_calls → 只执行 6 个，其余标 capped。"""
    many = Step(tool_calls=[ToolCall("get_quote", {"code": "sh600519"}) for _ in range(20)])
    r = await run(conn, script=[many, emit(speak=False, why_silent="够了")])
    assert len([c for c in r.tool_calls if not c.get("capped")]) == 6
    assert len([c for c in r.tool_calls if c.get("capped")]) == 14


async def test_ctx_grounding_without_tool_calls(conn):
    p = FakePusher()
    await run(conn, script=[emit(speak=True, body="现价 32.90（ctx.event.price）")], pusher=p,
              rendered=lambda t: Rendered("现价 32.90", 10, False, {"event": {"price": 32.90}}))
    assert p.grounded[-1].failed is False
    assert "数据校验失败" not in p.grounded[-1].text


async def test_budget_exhausted_skips_model(conn):
    d = deps_for(conn, script=[emit(speak=True)])
    # 新模型没有"事后 charge"：直接把额度扣光（扣到限额）
    d.budget.deduct(CFG.llm.budget.agent[0], CFG.llm.budget.agent[1])
    r = await run_with_deadline(event_trigger(), d)
    assert r.decision.kind == "error" and "预算" in (r.why or "")
    assert d.llm.sent == []                                   # 一次模型都没调


async def test_pending_action_surfaces_to_main_loop(conn):
    """D21：run_turn 不发确认卡，只把 pending_action 带在结果里。"""
    p = FakePusher()
    r = await run(conn, script=[emit(speak=True,
                                     pending_action={"tool": "mcp_set_alert",
                                                     "args": {"code": "sh600519"}})],
                  pusher=p)
    assert r.decision.pending_action["tool"] == "mcp_set_alert"
    assert p.confirm_card_calls == 0


# ---- 工作日志 ------------------------------------------------------------

def _seed_event(conn, eid=1, status="escalated"):
    conn.execute("INSERT INTO event_log(id, ts, trade_date, market, kind, scope, target, payload,"
                 " salience, agent_status, escalated, dedupe_key)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (eid, T0.isoformat(), 20261030, "cn_main", "breakout", "stock", "sh600519",
                  "{}", 0.5, status, 1 if status == "escalated" else 0, f"k{eid}"))
    conn.commit()


def _result(kind="speak", **kw):
    from agent.loop import TurnResult

    return TurnResult(decision=TurnDecision(kind=kind, speak=(kind == "speak")),
                      rounds=1, rendered=Rendered("…", 1, False, {}), **kw)


def test_record_writes_turn_and_updates_event(conn):
    _seed_event(conn)
    tr = event_trigger()
    turn_id = record(conn, tr, _result(), FixedClock(T0))
    row = conn.execute("SELECT wake_kind, context_digest, rounds, decision, trigger_id"
                       " FROM agent_turn WHERE id=?", (turn_id,)).fetchone()
    assert row[0] == "event" and row[1] and row[3] == "speak" and row[4] == 1
    assert conn.execute("SELECT agent_status FROM event_log WHERE id=1").fetchone()[0] == "done"


@pytest.mark.parametrize("kind", ["timeout", "error", "invalid"])
def test_failed_event_turn_goes_pending_retry(conn, kind):
    _seed_event(conn)
    record(conn, event_trigger(), _result(kind), FixedClock(T0))
    assert conn.execute("SELECT agent_status FROM event_log WHERE id=1").fetchone()[0] == "pending_retry"


def test_context_digest_never_empty(conn):
    _seed_event(conn)
    record(conn, event_trigger(), _result("silent"), FixedClock(T0))
    row = conn.execute("SELECT context_digest FROM agent_turn").fetchone()
    assert row[0] and len(row[0]) > 0


def test_pending_action_stored_as_json(conn):
    _seed_event(conn)
    r = _result()
    r.decision.pending_action = {"tool": "mcp_set_alert", "args": {"code": "sh600519"}}
    record(conn, event_trigger(), r, FixedClock(T0))
    row = conn.execute("SELECT pending_action, pending_status FROM agent_turn").fetchone()
    assert json.loads(row[0])["tool"] == "mcp_set_alert" and row[1] == "pending"


def test_turn_and_event_update_atomic(conn):
    """事件更新失败时 agent_turn 也不能留下——否则下次 poll 又看到同一个 new。

    `sqlite3.Connection.execute` 是只读属性，patch 不了；改用子类包一层。
    """
    import sqlite3

    _seed_event(conn)

    class Boom(Exception):
        pass

    class Failing(sqlite3.Connection):
        def execute(self, sql, *a):
            if "UPDATE event_log" in sql:
                raise Boom()
            return super().execute(sql, *a)

    conn.row_factory = sqlite3.Row
    raw = conn
    bad = sqlite3.connect(":memory:", factory=Failing, check_same_thread=False)
    raw.backup(bad)                                  # 同一份库，换成会炸的连接
    with pytest.raises(Boom):
        record(bad, event_trigger(), _result(), FixedClock(T0))
    # 事务回滚：agent_turn 里一行都不该留下
    assert bad.execute("SELECT count(*) FROM agent_turn").fetchone()[0] == 0
