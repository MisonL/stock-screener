"""预渲染上下文的裁剪顺序与 6000 字符预算。"""
import json
from datetime import datetime, timedelta

import pytest

from agent import memory
from agent.attention import EventRow, WakeGroup
from agent.context import render_event, render_heartbeat
from core.clock import FixedClock
from core.config import load_config
from core.db import connect, migrate
from llm.prompts import CHAT_SYSTEM_PROMPT, SYSTEM_PROMPT
from tests.helpers import SH

T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)
DAY = 20261030
CFG = load_config(__import__("pathlib").Path("config"))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "c.db")
    migrate(c)
    return c


def _ev(i, *, kind="breakdown", industry="半导体", price=10.0, code=None, day=DAY,
        salience=0.9, ts=None):
    code = code or f"sh6005{i:02d}"
    return EventRow(id=i, ts=(ts or T0).isoformat(), trade_date=day, market="cn_main", kind=kind,
                    scope="stock", target=code,
                    payload={"code": code, "industry": industry, "price": price},
                    salience=salience, dedupe_key=f"k{i}")


def _insert(conn, e, status="new"):
    conn.execute(
        "INSERT INTO event_log(id, ts, trade_date, market, kind, scope, target, payload,"
        " salience, agent_status, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (e.id, e.ts, e.trade_date, e.market, e.kind, e.scope, e.target,
         json.dumps(e.payload), e.salience, status, e.dedupe_key))
    conn.commit()


def test_sector_events_capped_at_eight_highest(conn):
    group_ev = _ev(0)
    _insert(conn, group_ev)
    for i in range(1, 21):                    # 20 条同板块其它事件，显著度递减
        e = _ev(i, salience=1.0 - i * 0.01)
        _insert(conn, e)
    group = WakeGroup(id="g", market="cn_main", kind="merged", events=[group_ev],
                      channel="A", salience=0.585)
    out = render_event(group, conn, FixedClock(T0), CFG)
    assert len(out.ctx["sector_events"]) == 8
    sals = [d["salience"] for d in out.ctx["sector_events"]]
    assert sals == sorted(sals, reverse=True)
    assert out.chars <= CFG.agent.context.max_chars


def test_own_history_trimmed_to_five(conn):
    for i in range(15):
        conn.execute(
            "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, why,"
            " fallback, market) VALUES (?,?,?,?,?,?,?,?)",
            ((T0 - timedelta(minutes=i)).isoformat(), "event", "d", 1, "speak",
             f"理由{i}", 0, "cn_main"))
    conn.commit()
    group = WakeGroup(id="g", market="cn_main", kind="event",
                      events=[_ev(0)], channel="A", salience=0.45)
    out = render_event(group, conn, FixedClock(T0), CFG)
    # 预算内先取 10 条；超预算时整段丢弃
    assert len(out.ctx["own_history"]) <= 10


def test_oversized_event_keeps_first_two_sections_and_marks_truncated(conn):
    """本身就 7000 字符的事件：truncated=True 且只剩前两段（板块/记忆/历史全丢）。"""
    big = _ev(0, price=10.0)
    big.payload["note"] = "很" * 7000
    big.target = "sh600519" + str(big.payload["note"])
    _insert(conn, big)
    group = WakeGroup(id="g", market="cn_main", kind="event", events=[big],
                      channel="A", salience=0.45)
    out = render_event(group, conn, FixedClock(T0), CFG)
    assert out.truncated is True
    # 丢的是可丢的三段，**第一段不裁**（模型拿半截上下文会一本正经讲另一件事）
    assert out.ctx["sector_events"] == [] and out.ctx["own_history"] == []
    assert out.text.startswith("【事件】")
    assert "【同板块事件】" not in out.text and "【相关记忆】" not in out.text


def test_ctx_contains_every_price_in_text(conn):
    """ctx 是校验用的 `ctx` 工具返回：text 里出现的价格必须能在 ctx 里按路径取到。"""
    group = WakeGroup(id="g", market="cn_main", kind="event", events=[_ev(0, price=12.34)],
                      channel="A", salience=0.45)
    out = render_event(group, conn, FixedClock(T0), CFG)
    assert "12.34" in out.text
    assert out.ctx["event"][0]["price"] == pytest.approx(12.34)


def test_memory_block_absent_when_empty(conn):
    group = WakeGroup(id="g", market="cn_main", kind="event", events=[_ev(0)],
                      channel="A", salience=0.45)
    out = render_event(group, conn, FixedClock(T0), CFG)
    assert "【相关记忆】" not in out.text and out.ctx["memory"] == []


def test_memory_at_most_three(conn):
    for i in range(5):
        memory.write(conn, kind="judgment", scope="stock", target="sh600500",
                     content=f"记忆{i}", turn_id=None,
                     created_at=(T0 - timedelta(minutes=i)).isoformat())
    group = WakeGroup(id="g", market="cn_main", kind="event", events=[_ev(0, code="sh600500")],
                      channel="A", salience=0.45)
    out = render_event(group, conn, FixedClock(T0), CFG)
    assert len(out.ctx["memory"]) <= 3


def test_heartbeat_excludes_yesterday(conn):
    _insert(conn, _ev(1, day=DAY - 1))        # 昨天的
    _insert(conn, _ev(2, day=DAY))
    out = render_heartbeat(T0 - timedelta(minutes=5), conn, FixedClock(T0), CFG)
    ids = {e["id"] for e in out.ctx["events"]}
    assert ids == {2}
    assert "600501" not in out.text           # 昨天的票不能出现在「刚发生」里


def test_heartbeat_includes_pending_retry(conn):
    _insert(conn, _ev(3), status="pending_retry")
    out = render_heartbeat(T0 - timedelta(minutes=5), conn, FixedClock(T0), CFG)
    assert [r["id"] for r in out.ctx["pending_retry"]] == [3]


def test_system_prompt_has_no_threshold_literals():
    """阈值改了提示词里的数字就成了谎言；模型会把它当事实讲出去。"""
    for bad in ("0.55", "1.5", "15%", "0.45", "0.70"):
        assert bad not in SYSTEM_PROMPT and bad not in CHAT_SYSTEM_PROMPT


def test_chat_prompt_does_not_demand_emit_decision():
    """chat 工具集没有 emit_decision：照抄盯盘提示词会让模型每轮调不存在的工具。"""
    assert "emit_decision" in SYSTEM_PROMPT
    assert "emit_decision" not in CHAT_SYSTEM_PROMPT


def test_prompts_share_hard_rules():
    for rule in ("出处标记", "不复权", "阿拉伯数字", "不确定"):
        assert rule in SYSTEM_PROMPT and rule in CHAT_SYSTEM_PROMPT


def test_sector_events_config_reaches_the_rendered_output(conn):
    """**`context.sector_events` 必须真的影响渲染结果**（v0.2.105）。

    第七轮评审：`_limit()` 本身有测试，但 `render_event` 里
    `_sector_block(..., cfg)` 这个**生产调用点**没测——把它改回不传 cfg，
    11 个测试全绿。假绿形状与 heartbeat / max_tokens 完全一致。
    """
    from types import SimpleNamespace
    from core.clock import FixedClock
    from tests.helpers import SH
    from datetime import datetime
    import agent.context as C

    now = datetime(2026, 11, 20, 10, 0, tzinfo=SH)
    clock = FixedClock(now)

    # 造 6 条同板块其它事件（用现成的 `_ev` / `_insert` / `WakeGroup`）
    group_ev = _ev(0)
    _insert(conn, group_ev)
    for i in range(1, 7):
        _insert(conn, _ev(i, salience=1.0 - i * 0.01))   # 默认 industry=半导体，与 group_ev 同板块

    group = WakeGroup(id="g", market="cn_main", kind="merged", events=[group_ev],
                      channel="A", salience=0.585)

    cfg = SimpleNamespace(agent=SimpleNamespace(context=SimpleNamespace(
        sector_events=2, own_history=10, max_chars=6000)))

    # **必须走 `render_event`**：直接调 `_sector_block` 验不到生产调用点
    # （把 `render_event` 里那行改回不传 cfg，直调版本照样绿——实测过）。
    out = render_event(group, conn, FixedClock(T0), cfg)
    assert len(out.ctx["sector_events"]) <= 2, (
        f"配置 sector_events=2 但渲染了 {len(out.ctx['sector_events'])} 条")

    # 生产配置（8）应当拿到更多
    out8 = render_event(group, conn, FixedClock(T0), CFG)
    assert len(out8.ctx["sector_events"]) > len(out.ctx["sector_events"]), (
        "生产配置应当取到比 2 条更多")


def test_own_history_config_reaches_the_rendered_output(conn):
    """**`context.own_history` 必须真的影响渲染结果**（v0.2.105）。

    与 sector_events 同批接线；同样必须走 `render_event`——
    把 `render_event` 里那行改回 `OWN_HISTORY` 常量，直调 `_history_block`
    的版本照样绿。
    """
    from types import SimpleNamespace

    # 造 10 条"我说过"的回合
    for i in range(10):
        conn.execute(
            "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
            " fallback, market, why) VALUES (?, 'heartbeat', 'd', 1, 'silent', 0,"
            " 'cn_main', ?)", (T0.isoformat(), f"我说过第 {i} 句"))
    conn.commit()

    group_ev = _ev(0)
    _insert(conn, group_ev)
    group = WakeGroup(id="g", market="cn_main", kind="event",
                      events=[group_ev], channel="A", salience=0.9)

    cfg = SimpleNamespace(agent=SimpleNamespace(context=SimpleNamespace(
        sector_events=8, own_history=3, max_chars=6000)))
    out = render_event(group, conn, FixedClock(T0), cfg)
    assert len(out.ctx["own_history"]) == 3, (
        f"配置 own_history=3 但渲染了 {len(out.ctx['own_history'])} 条")

    out8 = render_event(group, conn, FixedClock(T0), CFG)
    assert len(out8.ctx["own_history"]) == 10, (
        f"生产配置（10）应当渲染 10 条，实际 {len(out8.ctx['own_history'])}")
