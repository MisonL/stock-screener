"""影子模式（spec §10）与演练隔离（RUNBOOK §11）。

**背景**：`attention.poll()` 只拦住了「事件」这一种唤醒。`AgentMain._once()` 还会排队
心跳 / 闹钟 / 开盘简报第二段，这三类在 `shadow_mode: true` 时照样调 LLM、照样推送——
带着影子配置上线观测，手机照样响、钱照样烧。

这里全部是行为断言：真实 `AgentMain` + 真实 `AgentPusher` + 假 LLM/假 ntfy，
数的是 `llm.chat` 的调用次数、`push_log` 的行数、`agent_turn` 的内容。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.attention import AttentionLayer
from agent.context import Rendered
from agent.main import AgentMain
from agent.push import AgentPusher
from core.clock import FixedClock
from core.config import load_config
from core.db import connect, migrate
from llm.budget import Budget
from llm.grounding import GroundedText
from llm.tools import ToolRegistry
from tests.agent.test_loop import Step, ToolCall
from tests.helpers import SH

PROD_CFG = load_config(Path("config"))
DAY = 20261030
T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)


# ---- 假体 ----------------------------------------------------------------

class FakeLlm:
    """记 `chat` 调用次数：影子模式下这个数必须是 0。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.sent = []

    async def chat(self, messages, tools=None, max_tokens=None, timeout_s=None,
                   deduct=None, est_in=0, on_attempt=None):
        # 模仿真实 client：先扣额度再发请求
        if deduct is not None and not deduct(est_in, max_tokens):
            from llm.client import BudgetExceeded

            raise BudgetExceeded("预算不足")
        self.sent.append(messages)
        step = self.script.pop(0) if self.script else _silent()
        return SimpleNamespace(content=step.content, tool_calls=step.tool_calls,
                               attempts=1, usage=step.usage)


class FakeNtfy:
    def __init__(self):
        self.published = []

    def publish(self, msg):
        self.published.append(msg)
        return f"ntfy-{len(self.published)}"


def _silent(why="无事"):
    return Step(tool_calls=[ToolCall("emit_decision",
                                     {"speak": False, "body": "", "why_silent": why})])


def _speak(body="开盘（get_quote.data.price）", title="盯盘提醒"):
    return Step(tool_calls=[ToolCall("emit_decision", {
        "speak": True, "body": body, "direction": "none", "title": title})])


def _agent_raw():
    import yaml

    return yaml.safe_load(Path("config/agent.yaml").read_text(encoding="utf-8"))


def _cfg(shadow=True):
    """真实配置对象，只覆盖 `agent.attention.shadow_mode`（其余阈值保持文件值）。"""
    from core.config import Config

    agent = _agent_raw()
    agent.setdefault("attention", {})["shadow_mode"] = shadow
    return Config(json.loads(json.dumps(PROD_CFG._strategy_raw)),
                  json.loads(json.dumps(PROD_CFG._markets_raw)),
                  json.loads(json.dumps(PROD_CFG.notify._d)), agent,
                  yaml_safe_load_llm())


def _cfg_without_shadow_key():
    from core.config import Config

    agent = _agent_raw()
    agent.pop("attention", None)
    return Config(json.loads(json.dumps(PROD_CFG._strategy_raw)),
                  json.loads(json.dumps(PROD_CFG._markets_raw)),
                  json.loads(json.dumps(PROD_CFG.notify._d)), agent, None)


def yaml_safe_load_llm():
    import yaml

    return yaml.safe_load(Path("config/llm.yaml").read_text(encoding="utf-8"))


def _seed_calendar(conn):
    for i in range(60):
        conn.execute("INSERT OR IGNORE INTO daily_bars VALUES"
                     " ('sh000300', ?, 3000,3000,3000,3000,0,0,'x')", (20261001 + i,))
    conn.commit()


class Env(SimpleNamespace):
    def turn_count(self, decision="shadow"):
        return self.conn.execute(
            "SELECT count(*) FROM agent_turn WHERE decision = ?", (decision,)).fetchone()[0]

    def push_rows(self):
        return self.conn.execute("SELECT count(*) FROM push_log").fetchone()[0]

    def turns(self, wake_kind):
        return self.conn.execute(
            "SELECT trigger_id, context_digest, rounds, decision FROM agent_turn"
            " WHERE wake_kind = ? ORDER BY id", (wake_kind,)).fetchall()


def _env(tmp_path, *, shadow=True, slot=" ", llm=None, occupy_open=True):
    conn = connect(tmp_path / f"s{slot.strip() or 'x'}_{int(shadow)}.db")
    migrate(conn)
    _seed_calendar(conn)
    clock = FixedClock(T0)
    cfg = _cfg(shadow=shadow)
    ntfy = FakeNtfy()
    pusher = AgentPusher(conn, ntfy, cfg, clock)
    llm = llm or FakeLlm()
    from agent.loop import Deps

    deps = Deps(conn=conn, cfg=cfg, clock=clock, llm=llm,
                budget=Budget(conn, "agent", cfg.llm.budget.agent),
                tools=ToolRegistry(conn, cfg, clock, mcp=None),
                render=lambda t: Rendered("…", 1, False, {}),
                pusher=pusher, memory=None, westock=None)
    deps.conn_factory = lambda: conn
    deps.alert = lambda *a, **k: None
    main = AgentMain(deps, clock)
    if occupy_open:
        # 开盘回合先占掉：心跳/闹钟的断言不该被 10:00 的开盘简报搅乱
        conn.execute("INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
                     " fallback, market) VALUES (?,?,?,?,?,?,?)",
                     (clock.now().isoformat(), "open", "占位", 1, "silent", 0, "cn_main"))
        conn.commit()
    return Env(conn=conn, clock=clock, llm=llm, ntfy=ntfy, pusher=pusher, cfg=cfg,
               deps=deps, main=main)


def _alarm(env, *, minutes_due=1, target="sh600519", note="看回落"):
    due = (env.clock.now() - timedelta(minutes=minutes_due)).isoformat()
    cur = env.conn.execute(
        "INSERT INTO agent_schedule(created_at, due_at, market, target, note, status)"
        " VALUES (?,?,?,?,?,'pending')", (due, due, "cn_main", target, note))
    env.conn.commit()
    return cur.lastrowid


def _escalated(env, *, minutes_ago=31, eid=1):
    """一条过期的 escalated 事件：心跳家务应把它回收成 pending_retry。"""
    when = env.clock.now() - timedelta(minutes=minutes_ago)
    env.conn.execute(
        "INSERT INTO event_log(id, ts, trade_date, market, kind, scope, target, payload,"
        " salience, agent_status, escalated, escalated_at, dedupe_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, when.isoformat(), DAY, "cn_main", "index_move", "market", "sh000001",
         json.dumps({"code": "sh000001"}), 0.7, "escalated", 1, when.isoformat(), f"k{eid}"))
    env.conn.commit()


# ---- 配置读取 ------------------------------------------------------------

async def test_missing_shadow_key_turns_agent_main_shadow(tmp_path):
    """漏配 `attention.shadow_mode` 时 `AgentMain` 自己就是影子的（不是只靠函数默认值）。"""
    env = _env(tmp_path, shadow=True)
    env.deps.cfg = _cfg_without_shadow_key()
    env.main = AgentMain(env.deps, env.clock)
    env.main.next_heartbeat_at = env.clock.now() - timedelta(seconds=1)
    await env.main._once()
    assert env.main.shadow_mode is True
    assert env.llm.sent == [] and env.push_rows() == 0


def test_shadow_flag_defaults_to_true_when_key_missing():
    """配置缺 `attention.shadow_mode` 时安全方向是「不发」（spec §10）。"""
    from agent.main import shadow_enabled
    from core.config import Config

    agent = _agent_raw()
    agent.pop("attention", None)
    bare = Config(PROD_CFG._strategy_raw, PROD_CFG._markets_raw, PROD_CFG.notify._d, agent, None)
    assert shadow_enabled(bare) is True
    assert shadow_enabled(_cfg(shadow=True)) is True
    assert shadow_enabled(_cfg(shadow=False)) is False


def test_production_config_records_the_current_stage():
    """把当前部署阶段钉住，改配置时要有意识地同步这里。

    v0.2.71 已过 shadow 观测期（11-10~11-18 共 7 个交易日）切到正式模式。
    **要退回 shadow，必须同时改 `config/agent.yaml` 和这里**——否则
    "yaml 改了但容器没重建"这类事故会在正式模式下悄悄发生。
    """
    assert PROD_CFG.agent.attention.shadow_mode is False


# ---- 心跳 ----------------------------------------------------------------

async def test_shadow_heartbeat_does_not_call_llm_but_keeps_housekeeping(tmp_path):
    """影子期心跳：不调 LLM、不排队，但家务照做、下一跳照排。"""
    env = _env(tmp_path, shadow=True)
    _escalated(env, minutes_ago=31, eid=1)
    env.main.next_heartbeat_at = env.clock.now() - timedelta(seconds=1)
    await env.main._once()

    assert env.llm.sent == []
    assert env.push_rows() == 0
    assert env.ntfy.published == []
    # 家务仍执行：escalated 过期回收
    assert env.conn.execute(
        "SELECT agent_status FROM event_log WHERE id=1").fetchone()[0] == "pending_retry"
    # 下一跳推进
    assert env.main.next_heartbeat_at > env.clock.now()


async def test_shadow_heartbeat_writes_one_audit_row_idempotent(tmp_path):
    """同一个到期时刻只留一行：主循环 2 秒一次，不能每跳重写。"""
    env = _env(tmp_path, shadow=True)
    env.main.next_heartbeat_at = env.clock.now() - timedelta(seconds=1)
    await env.main._once()
    first = env.turns("heartbeat")
    assert len(first) == 1
    trigger_id, digest, rounds, decision = first[0]
    assert decision == "shadow" and rounds == 0 and trigger_id is None
    assert "heartbeat" in digest

    # 再跑一轮（心跳未到期）→ 不重复写
    await env.main._once()
    assert len(env.turns("heartbeat")) == 1

    # 下一个真实到期点（时钟真的走到那里）→ 允许再写一行（不是永久去重）
    env.clock._now = env.main.next_heartbeat_at
    await env.main._once()
    assert len(env.turns("heartbeat")) == 2
    assert env.llm.sent == [] and env.push_rows() == 0


# ---- 闹钟 ----------------------------------------------------------------

async def test_shadow_alarm_claimed_without_llm_or_push(tmp_path):
    env = _env(tmp_path, shadow=True)
    alarm_id = _alarm(env)
    await env.main._once()

    assert env.llm.sent == []
    assert env.push_rows() == 0 and env.ntfy.published == []
    row = env.conn.execute("SELECT status, fired_turn FROM agent_schedule WHERE id=?",
                           (alarm_id,)).fetchone()
    assert row[0] == "fired", "影子期闹钟没被认领 → 每轮都到期，死循环"
    assert row[1] is not None
    turn = env.conn.execute("SELECT wake_kind, trigger_id, rounds, decision, context_digest"
                            " FROM agent_turn WHERE id=?", (row[1],)).fetchone()
    assert turn[0] == "alarm" and turn[4].startswith("shadow")
    assert turn[1] is None, "trigger_id 语义是 event_log.id，闹钟没有对应事件"
    assert turn[2] == 0 and turn[3] == "shadow"
    assert f"alarm={alarm_id}" in turn[4]


async def test_shadow_alarm_not_retriggered_on_next_loop(tmp_path):
    env = _env(tmp_path, shadow=True)
    alarm_id = _alarm(env)
    await env.main._once()
    await env.main._once()
    await env.main._once()
    assert len(env.turns("alarm")) == 1
    assert env.conn.execute("SELECT count(*) FROM agent_schedule WHERE status='pending'"
                            ).fetchone()[0] == 0
    assert env.llm.sent == [] and env.push_rows() == 0


# ---- 开盘简报第二段 ------------------------------------------------------

async def test_shadow_open_turn_not_queued(tmp_path):
    """09:26 的影子期开盘回合：不排队、不推送、每天一行留痕且不重复。"""
    env = _env(tmp_path, shadow=True, occupy_open=False)
    env.clock._now = datetime(2026, 10, 30, 9, 26, tzinfo=SH)
    await env.main._once()
    assert env.llm.sent == []
    assert env.push_rows() == 0 and env.ntfy.published == []
    rows = env.turns("open")
    shadow_rows = [r for r in rows if r[3] == "shadow"]
    assert len(shadow_rows) == 1, shadow_rows
    trigger_id, digest, rounds, decision = shadow_rows[0]
    assert trigger_id is None and rounds == 0 and decision == "shadow"
    assert "2026-10-30" in digest

    env.clock._now = datetime(2026, 10, 30, 9, 27, tzinfo=SH)
    await env.main._once()
    shadow_rows = [r for r in env.turns("open") if r[3] == "shadow"]
    assert len(shadow_rows) == 1


# ---- 事件（已有行为，钉住整条链路） --------------------------------------

async def test_shadow_event_chain_neither_llm_nor_push(tmp_path):
    env = _env(tmp_path, shadow=True)
    env.conn.execute(
        "INSERT INTO event_log(ts, trade_date, market, kind, scope, target, payload,"
        " salience, agent_status, escalated, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (env.clock.now().isoformat(), DAY, "cn_main", "breakdown", "stock", "sh600519",
         json.dumps({"code": "sh600519"}), 0.9, "new", 0, "ev1"))
    env.conn.commit()
    await env.main._once()
    assert env.llm.sent == []
    assert env.push_rows() == 0
    assert env.turn_count("shadow") >= 1


# ---- 非影子模式：三类唤醒照常 --------------------------------------------

async def test_non_shadow_heartbeat_still_runs_llm_turn(tmp_path):
    env = _env(tmp_path, shadow=False)
    env.main.next_heartbeat_at = env.clock.now() - timedelta(seconds=1)
    await env.main._once()
    assert env.llm.sent != []
    rows = env.turns("heartbeat")
    assert rows and rows[0][3] == "silent"


async def test_non_shadow_heartbeat_speaks_and_pushes(tmp_path):
    """非影子心跳真的开口：push_log 一行 agent_note、ntfy 收到一条。"""
    env = _env(tmp_path, shadow=False)
    env.main.next_heartbeat_at = env.clock.now() - timedelta(seconds=1)
    env.llm.script = [_speak("半导体回暖（get_fund_flow.data.net）", "盘面")]
    await env.main._once()
    assert env.llm.sent != []
    assert env.push_rows() == 1
    assert env.conn.execute("SELECT kind FROM push_log").fetchone()[0] == "agent_note"
    assert len(env.ntfy.published) == 1


async def test_non_shadow_alarm_still_runs_and_pushes(tmp_path):
    env = _env(tmp_path, shadow=False)
    alarm_id = _alarm(env)
    env.llm.script = [_speak("茅台回落（get_quote.data.price）", "回访")]
    await env.main._once()
    assert env.llm.sent != []
    row = env.conn.execute("SELECT status, fired_turn FROM agent_schedule WHERE id=?",
                           (alarm_id,)).fetchone()
    assert row[0] == "fired" and row[1] is not None
    assert env.push_rows() == 1 and env.conn.execute(
        "SELECT kind FROM push_log").fetchone()[0] == "agent_note"
    assert len(env.ntfy.published) == 1


async def test_non_shadow_open_turn_still_pushes_second_segment(tmp_path):
    env = _env(tmp_path, shadow=False, occupy_open=False)
    env.conn.execute(
        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, event_id,"
        " trade_date, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (T0.isoformat(), None, "open_brief", "P2", "open-20261030", 1, "开盘简报正文",
         None, DAY, "accepted"))
    env.conn.commit()
    env.clock._now = datetime(2026, 10, 30, 9, 26, tzinfo=SH)
    env.llm.script = [_speak("开盘第二段（get_quote.data.price）", "开盘简报")]
    await env.main._once()
    assert env.llm.sent != []
    rows = env.conn.execute(
        "SELECT segment, body FROM push_log WHERE seq_id='open-20261030' ORDER BY segment"
    ).fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert rows[1][1].startswith("开盘简报正文")


# ---- 演练隔离（SS_DRILL=1） ----------------------------------------------

def _drill_env(tmp_path, monkeypatch, value="1"):
    monkeypatch.setenv("SS_DRILL", value)
    conn = connect(tmp_path / f"drill{value}.db")
    migrate(conn)
    clock = FixedClock(T0)
    ntfy = FakeNtfy()
    return conn, clock, ntfy, AgentPusher(conn, ntfy, PROD_CFG, clock)


def _decision():
    from agent.decision import TurnDecision

    return TurnDecision(kind="speak", speak=True, level="P1", title="解读",
                        body="半导体资金流入（get_fund_flow.data.net）")


def test_drill_mode_pushes_nothing_and_writes_no_push_log(tmp_path, monkeypatch, caplog):
    conn, clock, ntfy, pusher = _drill_env(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, event_id,"
        " trade_date, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (T0.isoformat(), "600519", "breakout", "P0", "sig-600519-20261030-1", 1, "突破原文",
         1234, DAY, "accepted"))
    conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback,"
        " market) VALUES (?,?,?,?,?,?,?)", (T0.isoformat(), "event", "d", 1, "speak", 0,
                                            "cn_main"))
    conn.commit()
    before = conn.execute("SELECT count(*) FROM push_log").fetchone()[0]

    with caplog.at_level("INFO"):
        out_note = pusher.agent_note(_decision(), GroundedText(text="说一句", ungrounded=False, failed=False), code="sh600519")
        out_second = pusher.second_segment(1234, _decision(), GroundedText(text="追一句", ungrounded=False, failed=False))
        out_open = pusher.open_second_segment(DAY, _decision(), GroundedText(text="第二段", ungrounded=False, failed=False))
        out_card = pusher.confirm_card(1, {"tool": "mcp_add_watch", "args": {"code": "sh600519"}})

    assert (out_note, out_second, out_open, out_card) == (None, None, None, None)
    assert conn.execute("SELECT count(*) FROM push_log").fetchone()[0] == before, \
        "演练数据会污染真实推送统计"
    assert ntfy.published == [], "演练不许真的发到手机"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for word in ("agent_note", "second_segment", "open_second_segment", "confirm_card"):
        assert word in logged, f"演练要留日志：{word} 不在 {logged!r}"
    # 确认卡也不能把回合标成 pending（否则会等一个永远不会来的点击）
    assert conn.execute("SELECT pending_status FROM agent_turn WHERE id=1").fetchone()[0] is None


def test_drill_mode_off_restores_real_push(tmp_path, monkeypatch):
    conn, clock, ntfy, pusher = _drill_env(tmp_path, monkeypatch, value="0")
    assert pusher.agent_note(_decision(), GroundedText(text="说", ungrounded=False, failed=False), code="sh600519") is not None
    assert conn.execute("SELECT count(*) FROM push_log").fetchone()[0] == 1
    assert len(ntfy.published) == 1


# ---- 心跳服务名 ----------------------------------------------------------

def test_boot_wires_heartbeat_service_from_env(tmp_path, monkeypatch):
    """`main()` 把 `HEARTBEAT_SERVICE` 传给心跳线程（RUNBOOK §11 的 agent-drill）。"""
    from agent import main as agent_main

    seen = {}
    monkeypatch.setenv("HEARTBEAT_SERVICE", "agent-drill")
    monkeypatch.setenv("NTFY_TOKEN", "dummy")

    class Stop(Exception):
        pass

    class FakeHeartbeat:
        def __init__(self, conn_factory, service="agent", every_s=30, detail_provider=None,
                     clock=None):
            seen["service"] = service
            seen["detail_provider"] = detail_provider

        def start(self):
            raise Stop

    class FakeNtfy:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(agent_main, "Heartbeat", FakeHeartbeat, raising=False)
    monkeypatch.setattr("notify.ntfy.NtfyClient", FakeNtfy)
    monkeypatch.setattr("mcp.westock.WestockMcp", lambda: None)
    monkeypatch.setattr("core.db.make_conn_factory", lambda: _factory)
    monkeypatch.setattr("core.config.load_config", lambda *a, **k: _cfg(shadow=True))
    conn = connect(tmp_path / "boot.db")
    migrate(conn)

    def _factory():
        return conn

    with pytest.raises(Stop):
        agent_main.main()
    assert seen["service"] == "agent-drill"


def test_boot_wires_default_service_without_env(tmp_path, monkeypatch):
    from agent import main as agent_main

    seen = {}
    monkeypatch.delenv("HEARTBEAT_SERVICE", raising=False)
    monkeypatch.setenv("NTFY_TOKEN", "dummy")

    class Stop(Exception):
        pass

    class FakeHeartbeat:
        def __init__(self, conn_factory, service="agent", every_s=30, detail_provider=None,
                     clock=None):
            seen["service"] = service

        def start(self):
            raise Stop

    class FakeNtfy:
        def __init__(self, *a, **k):
            pass

    conn = connect(tmp_path / "boot2.db")
    migrate(conn)
    monkeypatch.setattr(agent_main, "Heartbeat", FakeHeartbeat, raising=False)
    monkeypatch.setattr("notify.ntfy.NtfyClient", FakeNtfy)
    monkeypatch.setattr("mcp.westock.WestockMcp", lambda: None)
    monkeypatch.setattr("core.db.make_conn_factory", lambda: (lambda: conn))
    monkeypatch.setattr("core.config.load_config", lambda *a, **k: _cfg(shadow=True))
    with pytest.raises(Stop):
        agent_main.main()
    assert seen["service"] == "agent"


def test_heartbeat_service_defaults_to_agent(monkeypatch):
    from agent.main import heartbeat_service

    monkeypatch.delenv("HEARTBEAT_SERVICE", raising=False)
    assert heartbeat_service() == "agent"
    monkeypatch.setenv("HEARTBEAT_SERVICE", "agent-drill")
    assert heartbeat_service() == "agent-drill"
    monkeypatch.setenv("HEARTBEAT_SERVICE", "   ")
    assert heartbeat_service() == "agent"


def test_event_wake_has_hard_gate_in_once(env):
    """影子期的事件唤醒必须有**显式分支**，不能只靠 `poll()` 返回空。

    现在 `poll()` 在 shadow 下先标 `escalated=1` 再返回 `[]`。一旦这个内部实现
    被改动（或协程被取消导致状态已提交），`_once()` 会直接 `await self._run(...)`
    调模型花钱。硬闸放在 `_once()` 里才是与 alarm/open 同级的保险。
    """
    import inspect

    from agent import main as agent_main

    src = inspect.getsource(agent_main.AgentMain._once)
    # 事件循环体内必须出现 shadow 分支
    seg = src[src.index("for group in self.attention.poll()"):]
    seg = seg[:seg.index("if now >= self.next_heartbeat_at")]
    assert "shadow" in seg, "事件唤醒路径没有 shadow 硬闸（只靠 poll() 返回空）"


def test_agent_logging_suppresses_httpx_urls():
    """agent 进程也要把 httpx 的 INFO 压到 WARNING。

    `agent/main.py` 有自己的一份 `_setup_logging`（不复用 jobs 的）。
    `tests/jobs/test_main_scheduler.py::test_logging_does_not_spew_httpx_urls`
    只调 jobs 那份——把 agent 的还原成旧行为，那条测试照样绿（Codex 第十三轮指出）。
    实测 agent 日志被完整 URL 淹没时同样没法排障。
    """
    import logging
    from agent.main import _setup_logging

    _setup_logging()
    for name in ("httpx", "httpcore", "urllib3", "apscheduler"):
        assert logging.getLogger(name).level >= logging.WARNING, f"{name} 仍是 INFO"
