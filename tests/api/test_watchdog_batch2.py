"""Task 13：watchdog 规则 3（回合停滞）与决定 B3（LLM 连续不可用）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from api import watchdog
from api.watchdog import _llm_down
from core.clock import FixedClock
from core.config import load_config
from core.db import connect, migrate
from tests.helpers import SH

CFG = load_config(__import__("pathlib").Path("config"))
T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)
DAY = 20261030


class FakeDispatcher:
    def __init__(self):
        self.alerts = []

    def alert(self, kind, text, level="P1"):
        self.alerts.append((kind, text, level))


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "w.db")
    migrate(conn)
    _seed_calendar(conn)
    # 两条心跳都新鲜 → 不触发 agent_down / jobs_down
    for svc in ("agent", "jobs"):
        conn.execute(
            "INSERT OR REPLACE INTO service_heartbeat(service, last_beat_at, detail)"
            " VALUES (?,?,?)", (svc, T0.isoformat(), "{}"))
    conn.commit()
    return type("E", (), dict(conn=conn, clock=FixedClock(T0), disp=FakeDispatcher(),
                              cfg=CFG))


def _seed_calendar(conn):
    for i in range(60):
        conn.execute("INSERT OR IGNORE INTO daily_bars VALUES"
                     " ('sh000300', ?, 3000,3000,3000,3000,0,0,'x')", (20261001 + i,))
    conn.commit()


def _check(env):
    return watchdog.check(env.conn, env.clock, env.disp, cfg=env.cfg)


def _turn(conn, *, decision="silent", ts=None):
    conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback, market)"
        " VALUES (?,?,?,?,?,?,?)",
        ((ts or T0).isoformat(), "heartbeat", "d", 1, decision, 0, "cn_main"))
    conn.commit()


def _alert_rows(conn, kind, *, age_min=0):
    conn.execute(
        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ((T0 - timedelta(minutes=age_min)).isoformat(), None, kind, "P0",
         f"wd-{kind}-{age_min}", 1, DAY, "accepted"))
    conn.commit()


# ---- 规则 3 --------------------------------------------------------------

def test_stalled_turns_fire_p1(env):
    """心跳正常但 agent_turn 15 分钟无新行 → P1 调度卡死。"""
    _turn(env.conn, ts=T0 - timedelta(minutes=16))
    out = _check(env)
    assert "agent_stalled" in out
    k, text, level = env.disp.alerts[-1]
    assert k == "agent_stalled" and level == "P1" and "回合" in text


def test_fresh_turns_do_not_fire(env):
    _turn(env.conn, ts=T0 - timedelta(minutes=5))
    assert "agent_stalled" not in _check(env)


def test_budget_exhausted_suppresses_rule3(env):
    """预算耗尽是预期停止：暴跌日下午每 30 分钟一条误报 P1 会让告警变成噪声。"""
    _turn(env.conn, ts=T0 - timedelta(minutes=40))
    env.conn.execute(
        "UPDATE service_heartbeat SET detail = ? WHERE service = 'agent'",
        (json.dumps({"budget_exhausted": True}),))
    env.conn.commit()
    assert "agent_stalled" not in _check(env)
    assert env.disp.alerts == []


def test_stalled_only_in_trading_window(env, monkeypatch):
    _turn(env.conn, ts=T0 - timedelta(minutes=40))
    monkeypatch.setattr(watchdog, "in_watch_window", lambda now: False)
    assert _check(env) == []


def test_stalled_dedup_30min(env):
    _turn(env.conn, ts=T0 - timedelta(minutes=40))
    _alert_rows(env.conn, "agent_stalled", age_min=10)
    assert "agent_stalled" not in _check(env)


# ---- 决定 B3：LLM 连续不可用 ---------------------------------------------

def test_llm_down_p0_once(env):
    """连续 5 个 timeout → 一次 P0；再来 3 个不重复（30 分钟去重）。"""
    need = int(CFG.agent.llm_down.consecutive)
    for i in range(need):
        _turn(env.conn, decision="timeout", ts=T0 - timedelta(minutes=need - i))
    out = _check(env)
    assert "llm_down" in out
    k, text, level = env.disp.alerts[-1]
    assert (k, level) == ("llm_down", "P0")
    env.disp.alerts = []
    _alert_rows(env.conn, "llm_down", age_min=0)      # 30 分钟去重
    for i in range(3):
        _turn(env.conn, decision="timeout", ts=T0 - timedelta(seconds=3 - i))
    assert "llm_down" not in _check(env)
    assert env.disp.alerts == []


def test_llm_down_requires_consecutive_failures(env):
    """中途成功一次就清零：不能把「偶发失败」报成 P0。"""
    for i, decision in enumerate(("timeout", "timeout", "speak", "timeout", "timeout")):
        _turn(env.conn, decision=decision, ts=T0 - timedelta(minutes=5 - i))
    assert "llm_down" not in _check(env)


def test_llm_down_ignores_budget_exhaustion(env):
    """预算耗尽写的是 `error`（why=预算），不是 LLM 故障。"""
    need = int(CFG.agent.llm_down.consecutive)
    for i in range(need):
        _turn(env.conn, decision="error", ts=T0 - timedelta(minutes=need - i))
    env.conn.execute(
        "UPDATE service_heartbeat SET detail = ? WHERE service = 'agent'",
        (json.dumps({"budget_exhausted": True}),))
    env.conn.commit()
    assert "llm_down" not in _check(env)


# ---- 复核补充：三条边角判据 ----------------------------------------------

def test_rule3_fires_when_no_turn_ever_created(env):
    """心跳正常但一次回合都没有：`max(ts)` 是 NULL，原来直接返回 None → 永不告警。

    这是最该报的情形（进程活着、什么都不做），口径要与 /api/status 一致。
    """
    env.conn.execute("DELETE FROM agent_turn")
    env.conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES"
                     " ('watchdog.first_check', ?)",
                     ((T0 - timedelta(minutes=40)).isoformat(),))
    env.conn.commit()
    assert "agent_stalled" in _check(env)


def test_rule3_uses_heartbeat_health_not_alert_dedup(env):
    """agent 心跳已死、agent_down 又被 30 分钟去重挡住时，不能反过来说「心跳正常」。

    用「本次是否发出告警」判健康，会对着一个死进程喊「心跳正常但没回合」。
    """
    _turn(env.conn, ts=T0 - timedelta(minutes=40))
    env.conn.execute("UPDATE service_heartbeat SET last_beat_at = ? WHERE service='agent'",
                     ((T0 - timedelta(minutes=10)).isoformat(),))
    _alert_rows(env.conn, "agent_down", age_min=5)      # 已告警 → 本次不重复发
    env.conn.commit()
    out = _check(env)
    assert "agent_down" not in out
    assert "agent_stalled" not in out, "心跳已死却被判成「心跳正常但没回合」"


def test_llm_down_fires_after_minutes_without_success(env):
    """`minutes` 必须生效：失败次数不够 5 次，但已经 10 分钟没有成功回合也要报。"""
    span = int(CFG.agent.llm_down.minutes)
    _turn(env.conn, decision="speak", ts=T0 - timedelta(minutes=span + 1))
    _turn(env.conn, decision="timeout", ts=T0 - timedelta(minutes=2))
    _turn(env.conn, decision="timeout", ts=T0 - timedelta(minutes=1))
    assert _llm_down(env.conn, env.cfg, T0) is True


def test_llm_down_not_fired_without_failures(env):
    span = int(CFG.agent.llm_down.minutes)
    _turn(env.conn, decision="speak", ts=T0 - timedelta(minutes=span + 1))
    _turn(env.conn, decision="silent", ts=T0 - timedelta(minutes=1))
    assert _llm_down(env.conn, env.cfg, T0) is False
