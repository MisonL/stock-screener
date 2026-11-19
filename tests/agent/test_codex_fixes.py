"""Codex 复核 P1/P2 的回归钉（每条都对着一个「单测全绿、生产必挂」的失败模式）。"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm.client import LlmResult


# ---- P1-2：真实 SDK 返回的是 message，调用方读 result.content ----


def test_llm_result_exposes_content_and_tool_calls():
    """真实 `LlmResult.message` 是 pydantic 模型；没有这两个视图属性，
    生产路径每一次正常回复都会 AttributeError（假对象自带 content，永远发现不了）。"""
    msg = SimpleNamespace(content=" 你好 ", tool_calls=[{"id": "c1"}])
    r = LlmResult(message=msg, usage=None, attempts=1, latency_ms=10)
    assert r.content == " 你好 "
    assert r.tool_calls == [{"id": "c1"}]
    r2 = LlmResult(message=SimpleNamespace(content=None), usage=None, attempts=1, latency_ms=1)
    assert r2.content is None and r2.tool_calls == []


def test_tool_call_id_works_on_sdk_objects():
    """SDK 的工具调用对象没有 `.get()`：`c.get("id")` 在生产路径会 AttributeError。"""
    from agent.loop import _call_id

    class SdkCall:
        id = "call_abc"

    assert _call_id(SdkCall()) == "call_abc"
    assert _call_id({"id": "call_x"}) == "call_x"
    assert _call_id(SimpleNamespace()) is None


# ---- P1-1 / P1-3：agent 进程入口 ----


def test_agent_main_entry_awaits_run_and_starts_heartbeat():
    """compose 跑 `python -m agent.main`。

    两条都是「容器秒退、单测全绿」的类型：
    - `AgentMain(...).run()` 是协程，不 `asyncio.run` 就只是建了个协程对象；
    - 没有 Heartbeat 线程，watchdog 120 秒后判 agent_down。
    """
    src = Path("agent/main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    main_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    text = ast.get_source_segment(src, main_fn) or ""
    assert "asyncio.run(" in text, "入口没 await 主循环"
    assert "Heartbeat(" in text, "入口没启动心跳线程"
    assert "budget_exhausted" in text, "心跳没带 budget_exhausted（规则 3 抑制读不到）"


# ---- P1-4：通道 B 的开口必须走 agent_note ----


def test_loop_routes_channel_b_to_agent_note():
    """通道 B 事件由 jobs 模板发第一段，agent 没有第一段可追加。

    一律走 second_segment 的话，B 事件的开口决定会被静默丢弃。
    """
    src = Path("agent/loop.py").read_text(encoding="utf-8")
    assert 'trigger.channel == "A"' in src
    # B 分支必须存在，且不是二段
    idx = src.index('trigger.wake_kind == "event" and trigger.channel == "A"')
    tail = src[idx:]
    assert "agent_note" in tail[:1200]


# ---- P1-5：标题跟着方向走 ----


def test_push_title_follows_direction_not_model_title():
    from agent.decision import TurnDecision
    from agent.push import _title

    d = TurnDecision(kind="speak", speak=True, level="P1", direction="breakdown",
                     title="贵州茅台 放量突破")
    assert "跌破" in _title(d, "600519")
    assert "突破" not in _title(d, "600519"), "标题与正文方向矛盾"


# ---- P1-7：推送是同步阻塞，必须放线程 ----


def test_pushes_happen_off_the_event_loop():
    """ntfy 一次可阻塞 10 秒、westock 15 秒；在 wait_for 里同步调，取消就失效。"""
    src = Path("agent/loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    threaded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "to_thread"):
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Attribute):
            threaded.add(first.attr)
    for name in ("second_segment", "agent_note", "open_second_segment"):
        assert name in threaded, f"{name} 是同步阻塞调用，没放 asyncio.to_thread"


# ---- P1-8：失败尝试也要记账 ----


def test_failed_attempt_usage_is_charged():
    """失败的尝试也不能白花额度。

    普通扣减模型下"记账"发生在发请求**之前**（`deduct` 不可退），所以失败
    路径的正确断言是「扣过」而不是「事后补记一次」。
    """
    src = Path("agent/loop.py").read_text(encoding="utf-8")
    # 断言「接了扣减回调」用前缀匹配：签名后面可能带 state（轮数统计要用）
    assert "deduct=_deduct(deps" in src, "主回合没有接扣减回调"
    assert "_charge_usage" not in src, "旧的事后补记 helper 不该留着"


# ---- P2-4：回合级工具返回 ----


def test_grounding_uses_per_turn_returns_not_cumulative():
    """`ToolRegistry._returns` 跨回合累计：上一回合查过报价、本回合没查却引用同一个
    旧数字时，用累计值校验会「恰好通过」——正是出处校验要防的事。"""
    src = Path("agent/loop.py").read_text(encoding="utf-8")
    assert "tool_returns: dict[str, list[dict]] = {}" in src
    assert "returns_this_turn" not in src


# ---- P2-5：闹钟带回标的 ----


def test_alarm_wakeup_carries_target():
    src = Path("agent/main.py").read_text(encoding="utf-8")
    assert 'target=alarm.get("target")' in src
    assert "_first_target(trigger)" in src


# ---- P2-1：每轮都要查预算 ----


def test_budget_checked_every_round_not_just_once():
    """见 `tests/agent/test_token_accounting.py::test_budget_exhausted_stops_further_llm_calls`。

    这里原来是源码字符串断言（`"deps.budget.allow(" in loop_body`）：把条件改成
    `and False`（行为已失效）它仍然绿，而且新模型里 `budget.allow` 根本不在回合
    循环里——这条测试只是在「看起来有测试」。已换成真跑 `run_turn` 的行为测试。
    """
    pytest.skip("已由 test_budget_exhausted_stops_further_llm_calls 行为级覆盖")


# ---- P2-2 / P2-3：注意力层 ----


def test_merge_does_not_duplicate_single_events(tmp_path):
    """锚点重新起窗时不能把同一事件 append 两次（会多跑回合、日志里出现重复 id）。"""
    import json as _json
    from datetime import datetime as _dt, timedelta as _td

    from agent.attention import AttentionLayer
    from core.clock import FixedClock as _FC
    from core.db import connect as _connect, migrate as _migrate
    from tests.helpers import SH as _SH

    conn = _connect(tmp_path / "a.db")
    _migrate(conn)
    t0 = _dt(2026, 10, 30, 10, 30, tzinfo=_SH)
    cfg = SimpleNamespace(notify=SimpleNamespace(
        sector_merge=SimpleNamespace(window_s=300), dedupe_min=30, daily_cap=60),
        agent=SimpleNamespace(attention=SimpleNamespace(shadow_mode=False), channel_b_daily_cap=15,
                              fatigue=SimpleNamespace(free=10, step=10, **{"raise": 0.1})),
        strategy=SimpleNamespace(attention=SimpleNamespace(salience_min=0.1, base=0.5)))
    # 同行业 2 只（不足 3 只）→ 不进合并组，也不该被重复收进 singles
    for i in range(2):
        conn.execute(
            "INSERT INTO event_log(id, ts, trade_date, market, kind, scope, target, payload,"
            " salience, escalated, agent_status, dedupe_key) VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?)",
            (i + 1, (t0 + _td(seconds=i * 10)).isoformat(), 20261030, "cn_main", "near_breakout",
             "stock", f"sh60000{i}",
             _json.dumps({"code": f"sh60000{i}", "industry": "半导体"}), 0.6, 0, "new", f"k{i}"))
    conn.commit()
    groups = AttentionLayer(conn, cfg, _FC(t0)).poll()
    ids = [e.id for g in groups for e in g.events]
    assert sorted(ids) == [1, 2], f"事件被重复收进组：{ids}"


def test_merged_group_members_are_deduped(tmp_path):
    """合并组成员也要过 30 分钟去重，且去重后不足 3 只就不再是「板块级」。

    原来的做法只对 singles 去重：组里某只 20 分钟前刚说过，仍会被合并条带一遍
    ——同一只票一天说两遍，还占一次通道 B 额度。
    """
    from datetime import datetime as _dt

    from agent.attention import AttentionLayer
    from core.clock import FixedClock as _FC
    from core.db import connect as _connect, migrate as _migrate
    from core.config import load_config as _lc
    from pathlib import Path as _P
    from tests.helpers import SH as _SH

    CFG = _lc(_P("config"))
    conn = _connect(tmp_path / "b.db")
    _migrate(conn)
    t0 = _dt(2026, 10, 30, 10, 30, tzinfo=_SH)
    cfg = SimpleNamespace(notify=SimpleNamespace(
        sector_merge=SimpleNamespace(window_s=300), dedupe_min=30, daily_cap=60),
        agent=SimpleNamespace(attention=SimpleNamespace(shadow_mode=False), channel_b_daily_cap=15,
                              fatigue=SimpleNamespace(free=10, step=10, **{"raise": 0.1})),
        # 显著性来自真配置：手搓 SimpleNamespace 会一路漏字段（base/breadth/focus…）
        strategy=CFG.strategy)

    def ev(eid, code, ts, status="new"):
        conn.execute(
            "INSERT INTO event_log(id, ts, trade_date, market, kind, scope, target, payload,"
            " salience, escalated, agent_status, dedupe_key) VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, ts.isoformat(), 20261030, "cn_main", "breakdown", "stock", code,
             '{"industry": "半导体"}', 0.6, 1 if status != "new" else 0, status, f"k{eid}"))
        conn.commit()

    # 三只同行业：**同一批都是 new**，但 A 在 20 分钟前（另一批）已经说过。
    # 1 只留一个 done 行（同 code 同 kind 的「已说过」记录）+ 本批的 new 行。
    ev(1, "sh600001", t0.replace(minute=10), status="done")
    for eid, code in ((11, "sh600001"), (12, "sh600002"), (13, "sh600003")):
        ev(eid, code, t0)
    groups = AttentionLayer(conn, cfg, _FC(t0)).poll()
    ids = [e.id for g in groups for e in g.events]
    assert ids == [12, 13], f"去重后应只剩 12、13（各自成组），实际 {ids}"
    assert all(g.kind == "event" for g in groups), "不足 3 只不该再产出合并条"
    row = conn.execute("SELECT count(*) FROM agent_turn WHERE decision='deduped'").fetchone()[0]
    assert row == 1, "被去重的成员要有留痕"


# ---- 第四轮：记账参数类型（漏账根因）--------------------------------------


def test_auditor_records_usage_for_logs():
    """真实用量要进 `state["usage"]`（日志页 tokens 读它），且写进审计表。

    第四轮的教训：记账 helper 取错字段 → 用量静默漏记、日志 tokens 恒 NULL。
    这里用真实回放验证「记进去了」。
    """
    from agent.loop import _make_auditor

    class _Budget:
        def __init__(self):
            self.seen = []

        def audit(self, usage, **kw):
            self.seen.append(usage)

    budget = _Budget()
    state: dict = {"usage": None}
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    _make_auditor(SimpleNamespace(budget=budget), state)(1, usage, False, 100, 200)
    assert len(budget.seen) == 1, "用量没进审计"
    assert state["usage"] is usage


def test_main_round_charges_real_usage():
    """主回合三条出口（成功/失败/取消）各自记账一次，且都在构造结果之前。

    行为验证在 `tests/agent/test_token_accounting.py`；这里只钉住「不再用 finally」
    这件事——用 finally 时 except 分支先构造结果、后补账，导致日志 tokens 恒为 NULL。
    """
    src = Path("agent/loop.py").read_text(encoding="utf-8")
    start = src.index("result = await deps.llm.chat(msgs, tools, max_tokens=MAX_TOKENS")
    end = src.index('usage = state.get("usage") or usage')
    chat_block = src[start:end]
    # 额度在 chat 内部按 attempt 扣；审计在 on_attempt 回调里做
    assert "deduct=_deduct(deps" in chat_block, "主回合没接扣减回调"
    assert "on_attempt=" in chat_block, "没有审计回调"
    assert "finally" not in chat_block, "记账不能放在 finally 里（结果先构造、账后补）"
    assert "except (asyncio.CancelledError, GeneratorExit)" in chat_block, "取消路径没接"
    assert "def _charge(deps" not in src, "旧的 result 版 helper 不能留（会被误用）"




def test_alarm_context_reaches_the_model_messages():
    """闹钟的标的与备注必须**真的进到模型请求**里。

    原来的守卫只检查 `agent/main.py` 里有没有 `trig.target = ...` 这几行源码，
    绿着也证明不了模型收到了字段——实测 Trigger 上有 `sh601318` 和唯一备注，
    渲染出的 messages 里两者都不存在，不同闹钟产生完全相同的请求。
    """
    from agent.loop import Trigger, _messages
    from agent.loop import render_trigger  # noqa: F401  （确保能 import）

    t = Trigger(wake_kind="alarm", alarm_id=7, target="sh601318",
                note="AUDIT_ALARM_NOTE_77")
    msgs = _messages(t, type("R", (), {"text": "基准段落"})())

    joined = "\n".join(m["content"] for m in msgs)
    assert "sh601318" in joined, f"闹钟标的没进请求：{joined!r}"
    assert "AUDIT_ALARM_NOTE_77" in joined, "闹钟备注没进请求"

    # 另一个闹钟必须产生**不同**的请求
    other = _messages(Trigger(wake_kind="alarm", alarm_id=8, target="sz000001",
                              note="OTHER_NOTE"), type("R", (), {"text": "基准段落"})())
    assert "\n".join(m["content"] for m in other) != joined, "两个不同闹钟的请求完全一样"


def test_budget_exhaustion_mid_turn_is_recorded_as_error_not_timeout():
    """轮次**中途**预算耗尽要记 `error`，不能记 `timeout`。

    实测某天上午 11 条 timeout 里有 10 条其实是预算耗尽（耗时 0–5 秒，
    根本没等），真实超时率被虚报成 51%（实际约 5%）。
    入口层的预检一直记 error，这里必须一致。

    真跑 `run_turn`：让预算在首轮之后耗尽（allow 第一次 True、之后 False），
    断言产出的 decision 是 error 而不是 timeout。
    """
    import asyncio
    from types import SimpleNamespace as NS

    from agent.context import Rendered
    from agent.decision import TurnDecision
    from agent.loop import Deps, Trigger, run_turn
    from tests.helpers import SH
    from datetime import datetime

    t0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)

    class _Budget:
        """首轮放行、之后一律拒绝——模拟"轮次中途额度用完"。"""

        def __init__(self):
            self.calls = 0

        def allow(self, est_in=0, est_out=0):
            self.calls += 1
            return self.calls == 1

        def deduct(self, est_in=0, est_out=0):
            return True

        def audit(self, *a, **k):
            return None

    class _Llm:
        async def chat(self, messages, tools=None, **kw):
            return NS(tool_calls=[NS(id="c1", name="get_quote", arguments='{"code":"sh600519"}')],
                      content="", usage=NS(prompt_tokens=10, completion_tokens=5))

    class _Tools:
        def schema(self, scope):
            return []

        def run(self, *a, **k):
            return {"ok": True, "data": {"price": 10.0}}, []

    deps = Deps(conn=None, cfg=NS(llm=NS(timeouts=NS(agent_call_s=20),
                                        budget=NS(agent=1))),
                clock=NS(now=lambda: t0, today=lambda: 20261030),
                llm=_Llm(), budget=_Budget(), tools=_Tools(),
                render=lambda t: Rendered("x", 1, False, {}), pusher=None)
    trig = Trigger(wake_kind="heartbeat", rounds_budget=3, deadline_s=30)

    result = asyncio.run(run_turn(trig, deps, Rendered("x", 1, False, {}), {"rounds": 0}))
    assert result.decision.kind == "error", (
        f"预算中途耗尽被记成了 {result.decision.kind!r}：会把超时率统计搞脏")
    assert result.why == "agent 日预算已用完"


def test_agent_push_failures_flip_the_channel_flag(tmp_path):
    """**agent 侧推送失败也要被 `notify_ok` 看见**。

    `api/routes.py` 只认 `kind='push_channel_down'` 这一种行，而它原来只有
    `Dispatcher._channel_down()` 会写。agent 走的是 `AgentPusher` 这条独立路径
    （通道 B 便签、事件第二段、确认卡），失败时只把行标 `failed`——
    于是 agent 整天发不出去，首页仍显示"推送正常"。
    """
    from agent.push import AgentPusher
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from pathlib import Path
    from tests.helpers import SH
    from datetime import datetime

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "p.db")
    migrate(conn)
    clock = FixedClock(datetime(2026, 10, 30, 10, 30, tzinfo=SH))

    class _BoomNtfy:
        def publish(self, msg):
            raise RuntimeError("通道挂了")

    pusher = AgentPusher(conn, _BoomNtfy(), cfg, clock)
    for i in range(3):
        pusher._fail_streak = i          # 模拟已连续失败 i 次
        try:
            pusher._send(row_id=None, msg=SimpleNamespace(seq_id=f"x{i}", body="b"))
        except Exception:                                  # noqa: BLE001
            pass

    n = conn.execute(
        "SELECT count(*) FROM push_log WHERE kind='push_channel_down'").fetchone()[0]
    assert n >= 1, (
        "agent 连续 3 次发送失败后没有写 push_channel_down："
        "首页会一直显示推送正常，用户只能靠手机没响自己猜")


def test_confirmed_action_is_rendered_so_the_model_stops_reproposing(tmp_path):
    """**用户点过确认之后，模型下一轮必须知道"这件事已经办了"**。

    原来确认链路是断的：hook 把 `pending_status` 置 `confirmed`、写一条
    `confirm_result` 审计行就结束，agent 侧没有任何读取方，也不再产生可唤醒事件。
    于是模型会再提一次同样的 `pending_action`——用户被反复追问同一件事。
    """
    from agent.context import render_heartbeat
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from pathlib import Path
    from tests.helpers import SH
    from datetime import datetime
    import json as _json

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "c.db")
    migrate(conn)
    clock = FixedClock(datetime(2026, 10, 30, 10, 30, tzinfo=SH))

    conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback,"
        " pending_action, pending_status, market) VALUES (?,?,?,?,?,?,?,?,?)",
        (clock.now().isoformat(), "event", "x", 1, "speak", 0,
         _json.dumps({"tool": "mcp_add_watch", "args": {"code": "sh600519"}}),
         "confirmed", "cn_main"))
    conn.commit()

    rendered = render_heartbeat(clock.now(), conn, clock, cfg)
    assert "已确认执行" in rendered.text, (
        f"确认结果没有回流到上下文：模型会重复提议同一件事。实际渲染：{rendered.text!r}")
    assert "mcp_add_watch" in rendered.text


async def test_failed_alarm_turn_is_released_for_retry(tmp_path):
    """**闹钟回合失败必须把闹钟放回 `pending`**，否则"我 30 分钟后回来看"静默作废。

    `after_alarm` 在跑回合**之前**就把状态改成 `fired`（防重复回合），
    而 `_run` 失败时原来没有任何回滚逻辑；清理任务只处理 3 天前的 pending，
    不会补跑。
    """
    from agent.schedule import (ALARM_MAX_ATTEMPTS, ALARM_RETRY_BACKOFF_S, due_alarms,
                                release_alarm, schedule_alarm)
    from core.clock import FixedClock
    from core.db import connect, migrate
    from tests.helpers import SH
    from datetime import datetime, timedelta

    conn = connect(tmp_path / "a.db")
    migrate(conn)
    clock = FixedClock(datetime(2026, 10, 30, 10, 30, tzinfo=SH))

    schedule_alarm(conn, turn_id=1, due_at=clock.now().isoformat(), market="cn_main",
                   target="sh600519", note="回访")
    alarm = due_alarms(conn, clock.now())[0]

    # 认领（模拟 after_alarm）
    conn.execute("UPDATE agent_schedule SET status='fired' WHERE id=?", (alarm["id"],))
    conn.commit()
    assert due_alarms(conn, clock.now()) == [], "前提不成立：认领后不该再取到"

    # 回合失败 → 退回（**但要退避**：立刻退回会变成每 2 秒重跑一次的死循环）
    assert release_alarm(conn, alarm["id"], now=clock.now()) is True
    assert due_alarms(conn, clock.now()) == [], "退回后立刻又到期 = 生产事故那个死循环"
    later = clock.now() + timedelta(seconds=ALARM_RETRY_BACKOFF_S + 1)
    again = due_alarms(conn, later)
    assert [a["id"] for a in again] == [alarm["id"]], (
        "闹钟回合失败后没有放回 pending：这次回访被永久丢弃了")

    # **不能无限退回**：失败原因当天不恢复时（如日预算耗尽），
    # 退回 N 次后必须转 cancelled，否则会把整天耗在同一条闹钟上。
    conn.execute("UPDATE agent_schedule SET status='fired' WHERE id=?", (alarm["id"],))
    conn.commit()
    for i in range(2, ALARM_MAX_ATTEMPTS):
        assert release_alarm(conn, alarm["id"], now=later) is True, f"第 {i} 次不该放弃"
        conn.execute("UPDATE agent_schedule SET status='fired' WHERE id=?", (alarm["id"],))
        conn.commit()
    assert release_alarm(conn, alarm["id"], now=later) is False, "超过上限仍可重试"
    status = conn.execute("SELECT status FROM agent_schedule WHERE id=?",
                          (alarm["id"],)).fetchone()[0]
    assert status == "cancelled", f"超过重试上限后应当取消，实际是 {status}"


def test_scheduled_alarm_uses_the_injected_clock(tmp_path):
    """闹钟的 `created_at` 必须来自注入的时钟，不能用系统时间。

    `due_at` 一直取 `clock.now()`，而 `created_at` 原来是 `datetime.now(SH)`——
    生产用 `SystemClock` 无碍，但 `FixedClock`（测试/回放）下两个字段会差出真实
    日期，`get_own_history` 按 `created_at` 筛当日就查不到刚排的闹钟。
    """
    from agent.main import AgentMain
    from agent.schedule import schedule_alarm
    from core.clock import FixedClock
    from core.db import connect, migrate
    from tests.helpers import SH
    from datetime import datetime

    clock = FixedClock(datetime(2026, 10, 30, 10, 30, tzinfo=SH))
    conn = connect(tmp_path / "s.db")
    migrate(conn)

    schedule_alarm(conn, turn_id=1, due_at=clock.now().isoformat(), market="cn_main",
                   target="sh600519", note="回访", now=clock.now())
    created = conn.execute("SELECT created_at FROM agent_schedule").fetchone()[0]
    assert created.startswith("2026-10-30"), (
        f"created_at={created} 不是注入时钟的日期：模型按当日查不到这条闹钟")


def test_undelivered_confirm_card_does_not_leave_a_pending_action(tmp_path):
    """确认卡**没发出去**时不能留着 `pending`。

    `api/routes.py` 的确认入口只校验 `confirm-{turn_id}` 这行存在，不看它的
    status——留着一个用户根本没看到的 pending，24 小时内仍可被执行，
    而且一直占着"待确认"，模型会以为自己在等一个不会来的答复。
    """
    from agent.push import AgentPusher
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from pathlib import Path
    from tests.helpers import SH
    from datetime import datetime

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "cc.db")
    migrate(conn)
    clock = FixedClock(datetime(2026, 10, 30, 10, 30, tzinfo=SH))

    class _BoomNtfy:
        def publish(self, msg):
            raise RuntimeError("通道挂了")

    conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback,"
        " market) VALUES (?,?,?,?,?,?,?)",
        (clock.now().isoformat(), "event", "x", 1, "speak", 0, "cn_main"))
    conn.commit()
    turn_id = conn.execute("SELECT max(id) FROM agent_turn").fetchone()[0]

    pusher = AgentPusher(conn, _BoomNtfy(), cfg, clock)
    out = pusher.confirm_card(turn_id, {"tool": "mcp_add_watch", "args": {"code": "sh600519"}})
    assert out is None, "前提不成立：发送应当失败"

    status = conn.execute("SELECT pending_status FROM agent_turn WHERE id=?",
                          (turn_id,)).fetchone()[0]
    assert status != "pending", (
        "确认卡没发出去，却把回合标成 pending：用户看不到这张卡，24 小时内它仍可被执行")


def test_silent_turn_confirm_card_explains_why(tmp_path):
    """**沉默回合的确认卡必须带上理由**。

    `speak=false` 时不推正文，确认卡就是用户唯一看到的东西。原来卡上只有一行
    "加入自选 sh600519"，没有任何理由——用户不知道该不该点，只能无脑"忽略"。
    """
    from agent.push import action_desc

    desc = action_desc({"tool": "mcp_add_watch", "args": {"code": "sh600519"}},
                       "这只票连续三天贴顶尖但一直没进池")
    assert "理由" in desc, f"确认卡没有带理由：{desc!r}"
    assert "连续三天贴顶尖" in desc
    # 没有理由时不能凭空造一个"理由："
    assert "理由" not in action_desc({"tool": "mcp_add_watch",
                                      "args": {"code": "sh600519"}}, "")


def test_turn_deadline_allows_at_least_two_llm_calls():
    """**回合 deadline 必须够两次 LLM 调用**，否则每个回合都注定超时。

    一个回合至少调用两次模型：第一次决定查什么、拿到工具结果后再出决策。
    deadline 只够一次调用时，每轮都在"还没等回来就到点"上被掐断。

    这个不变式原来根本不成立：`EVENT_DEADLINE_S=25` 而单次上限 20 秒，
    25 < 20×2。中转站排队一超过 20 秒，**整个交易日 100% 回合超时**
    （实测单次最少 23 秒，连只回 1 个 token 的请求也是）。
    """
    from agent import main as m
    from core.config import load_config
    from pathlib import Path

    call_s = load_config(Path("config")).llm.timeouts.agent_call_s
    assert m.EVENT_DEADLINE_S >= call_s * 2, (
        f"事件回合 deadline {m.EVENT_DEADLINE_S}s < 单次调用 {call_s}s × 2："
        "回合根本没有机会跑完两轮")
    assert m.OTHER_DEADLINE_S >= call_s * 2, (
        f"心跳回合 deadline {m.OTHER_DEADLINE_S}s < 单次调用 {call_s}s × 2")
    assert m.OTHER_DEADLINE_S >= m.EVENT_DEADLINE_S, (
        "心跳/闹钟回合的 deadline 不该比事件回合更紧")


def test_no_deadline_still_terminates_by_rounds():
    """**不设 deadline 也不能无限跑**：轮数是真正的界。

    deadline 默认 0（不设上限）来自一次生产故障——上游网关排队时每轮要
    23–31 秒，任何固定 deadline 迟早被正常回合超过去，然后整天回合报废。
    安全性靠 `rounds_budget`：每一轮都会消耗掉一格，跑完必然退出。

    这里断言「三层约束」的取值语义，而不是某个具体秒数。
    """
    from agent.loop import _call_timeout
    import time

    from agent.loop import SAFETY_DEADLINE_S

    started = time.monotonic() - 1000          # 假装回合已经跑了 1000 秒
    # 都不设业务上限 → 落回兜底硬上限（**不是 None**：None 会永久挂住）
    assert _call_timeout(0, 0, started) == SAFETY_DEADLINE_S
    assert _call_timeout(0, None, started) == SAFETY_DEADLINE_S
    assert _call_timeout(None, None, started) == SAFETY_DEADLINE_S
    assert SAFETY_DEADLINE_S >= 600, "兜底上限太小，会误杀慢但正常的回合"
    # 只设单次上限
    assert _call_timeout(40, 0, started) == 40
    # 只设回合 deadline：用剩余时间，且不会算成负数
    assert 990 <= _call_timeout(0, 2000, started) <= 1000, "应当用剩余时间"
    assert _call_timeout(0, 500, started) == 1.0, "到点后至少给 1 秒，不能是 0/负数"
    # 两者都设，取小的
    assert _call_timeout(40, 2000, started) == 40
    assert _call_timeout(300, 2000, started) == 300


def test_safety_deadline_is_shorter_than_watchdog_stale():
    """兜底 deadline 必须**明显小于** watchdog 的判死阈值。

    两者都设 30 分钟时有个隐蔽问题：watchdog 的 `agent_stalled` 会先触发
    （15 分钟无新回合 → P1 告警），而回合自己还没收尾——用户收到"调度卡死"，
    实际只是上游慢。让回合先在 10 分钟时自己结束并记一行 `timeout`，
    证据更准确，也不会误报"进程卡死"。

    另一头也不能太小：那等于变相恢复固定超时，又回到"慢但正常被判死"的坑。
    实测上游最慢 31 秒，兜底必须是它的若干倍。
    """
    from agent.loop import SAFETY_DEADLINE_S
    from api.watchdog import AGENT_TURN_STALE_S
    from llm.client import SAFETY_TIMEOUT_S
    from chat.session import SAFETY_TIMEOUT_S as CHAT_SAFETY

    assert SAFETY_DEADLINE_S < AGENT_TURN_STALE_S, (
        f"兜底 deadline {SAFETY_DEADLINE_S}s 不小于 watchdog 判死阈值 "
        f"{AGENT_TURN_STALE_S}s：watchdog 会先报「卡死」")
    assert SAFETY_TIMEOUT_S == CHAT_SAFETY, "盯盘与对话的单次兜底应当一致"
    # 兜底要显著大于实测最慢（31 秒），否则会误杀正常调用
    assert SAFETY_TIMEOUT_S >= 31 * 5, "单次兜底太小，会误杀慢但正常的调用"
    # 但不该大到失去意义（最坏 = 单次兜底 × 轮数，不能超过太久）
    assert SAFETY_TIMEOUT_S <= 900, "单次兜底过大，卡住时恢复太慢"


def test_turn_budget_comes_from_config(tmp_path):
    """**回合预算必须真的从 YAML 读**——不能是死配置。

    这两段以前是死配置：真正的值写死在 `agent/main.py` 的常量里，运维改
    `config/agent.yaml` 完全不会有任何效果。而故障排查时第一反应恰恰是查 YAML，
    于是拿到的是假信息。用户明确要求"改成可配置参数"，这里把线接上。

    断言的是**端到端**：改配置 → 构造 AgentMain → 实例上的预算变了。
    """
    from agent.main import AgentMain
    from core.config import load_config
    from core.clock import FixedClock
    from core.db import connect, migrate
    from pathlib import Path
    from tests.helpers import SH
    from datetime import datetime
    import shutil

    src = Path("config")
    dst = tmp_path / "cfg"
    shutil.copytree(src, dst)
    yaml_path = dst / "agent.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    # 改成一组**不同于兜底默认**的值，确保读到的确实来自配置
    text = text.replace("""event:
  rounds: 3
  deadline_s: 0""", """event:
  rounds: 2
  deadline_s: 77""")
    text = text.replace("""heartbeat_turn:
  rounds: 6
  deadline_s: 0""", """heartbeat_turn:
  rounds: 5
  deadline_s: 88""")
    yaml_path.write_text(text, encoding="utf-8")

    cfg = load_config(dst)
    conn = connect(tmp_path / "b.db")
    migrate(conn)
    clock = FixedClock(datetime(2026, 10, 30, 10, 30, tzinfo=SH))

    class _Deps:
        def __init__(self):
            self.conn = conn
            self.cfg = cfg
            self.clock = clock
            self.pusher = None
            self.llm = None
            self.budget = None

    m = AgentMain(_Deps(), clock)
    assert (m.event_rounds, m.event_deadline) == (2, 77.0), (
        f"事件回合预算没从配置读：拿到的是 {(m.event_rounds, m.event_deadline)}")
    assert (m.other_rounds, m.other_deadline) == (5, 88.0), (
        f"心跳回合预算没从配置读：拿到的是 {(m.other_rounds, m.other_deadline)}")


async def test_budget_exhausted_does_not_rerun_alarms(tmp_path):
    """**预算耗尽后不能再空转跑闹钟**——这是一次真实事故。

    现场：`release_alarm()` 把失败闹钟退回 `pending`，`due_alarms()` 返回所有
    `due_at <= now` 的行，而 `_once()` 每 2 秒轮询一次。于是一条已过期的闹钟
    陷入「到期 → 认领 → 失败 → 退回 → 2 秒后又到期」的死循环。
    失败原因是「日预算已用完」——当天不可能恢复，循环一直转到收盘。

    结果：一个交易日内写了 **17885 行** `agent_turn`，全是
    `rounds=0, latency_ms=0` 的空转记录（根本没调模型），
    把日志页和 `agent_turn` 表彻底冲垮，真正的告警淹没在里面。

    修复：预算耗尽时 `_once()` 直接跳过闹钟循环。
    """
    from agent.main import AgentMain
    from agent.schedule import schedule_alarm
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from tests.helpers import SH
    from datetime import datetime, timedelta

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "bud.db")
    migrate(conn)
    clock = FixedClock(datetime(2026, 10, 30, 10, 30, tzinfo=SH))
    # 一条已到期的闹钟
    schedule_alarm(conn, turn_id=1, due_at=clock.now().isoformat(), market="cn_main",
                   target="sh600519", note="回访", now=clock.now())

    class _Deps:
        def __init__(self):
            self.conn = conn
            self.cfg = cfg
            self.clock = clock
            self.pusher = None
            self.llm = None
            self.budget = None
            self.alert = lambda *a, **k: None
            self.conn_factory = lambda: conn

    main = AgentMain(_Deps(), clock)
    main.budget_exhausted = True            # 当天额度已用完
    main._last_day = clock.today()
    main.next_heartbeat_at = clock.now() + timedelta(hours=1)   # 避开心跳路径
    # 开盘回合先占掉：本测试只关心闹钟，不该被 09:26 之后的开盘回合干扰
    conn.execute("INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
                 " fallback, market) VALUES (?,?,?,?,?,?,?)",
                 (clock.now().isoformat(), "open", "占位", 1, "silent", 0, "cn_main"))
    conn.commit()

    # 只看闹钟回合的增量：占位行与别的心跳行不该混进断言
    def alarm_turns():
        return conn.execute(
            "SELECT count(*) FROM agent_turn WHERE wake_kind = 'alarm'").fetchone()[0]

    before = alarm_turns()
    await main._once()
    after = alarm_turns()

    assert after == before, (
        f"预算耗尽后仍然跑了 {(after - before)} 个空转闹钟回合："
        "这正是生产事故里一天 17885 行的来源")


async def test_stale_alarms_from_previous_day_are_cancelled(tmp_path):
    """**跨交易日的 pending 闹钟必须作废**，不能隔天补跑。

    闹钟的语义是"过 N 分钟回来看看这个价位"——价位语境当天有效。隔天补跑
    昨天没跑成的回访，看到的是新一天的行情、回答的是昨天的问题。

    更糟的是它们会**一次性全部到期**：agent 停摆半天后，积累的十几条闹钟会在
    同一秒被 `due_alarms()` 全部取出，连跑十几个回合烧光预算，再推一串过时判断
    给用户。`jobs/cleanup.py` 只处理 3 天前的，救不了"昨天积压、今天开盘"这个
    最常见的情形。

    现场数据：2026-11-19 收盘时 16 条 pending 闹钟全部过期 2–3 小时，
    最早那条还是 11-18 排的——次日开盘会同时到期。
    """
    from agent.main import AgentMain
    from agent.schedule import due_alarms, schedule_alarm
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from tests.helpers import SH
    from datetime import datetime, timedelta

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "stale.db")
    migrate(conn)
    today = datetime(2026, 11, 19, 9, 20, tzinfo=SH)
    clock = FixedClock(today)

    # 昨天排的、今天已过期的一条；今天刚排的一条有效闹钟
    yesterday = (today - timedelta(days=1)).replace(hour=14, minute=1)
    schedule_alarm(conn, turn_id=1, due_at=yesterday.isoformat(), market="cn_main",
                   target="sh600519", note="昨天的回访", now=yesterday)
    fresh_due = today + timedelta(minutes=30)
    schedule_alarm(conn, turn_id=2, due_at=fresh_due.isoformat(), market="cn_main",
                   target="sh600161", note="今天的回访", now=today)

    class _Deps:
        def __init__(self):
            self.conn = conn
            self.cfg = cfg
            self.clock = clock
            self.pusher = None
            self.llm = None
            self.budget = None
            self.alert = lambda *a, **k: None
            self.conn_factory = lambda: conn

    main = AgentMain(_Deps(), clock)
    # 作废不再挂在 `heartbeat_housekeeping()` 上：那个方法只在心跳到期时跑，
    # 进程启动头一两分钟不会执行，昨天的闹钟会先开跑（已用真实路径复现）。
    main.sweep_stale_alarms(clock.now())

    rows = dict(conn.execute("SELECT note, status FROM agent_schedule").fetchall())
    assert rows["昨天的回访"] == "cancelled", "跨交易日的闹钟没有作废，明天开盘会补跑昨天的判断"
    assert rows["今天的回访"] == "pending", "今天的闹钟被误杀了"
    assert [a["note"] for a in due_alarms(conn, today)] == [], "过期闹钟仍会被取出"


async def test_stale_alarms_are_cancelled_before_the_alarm_loop(tmp_path):
    """**顺序很关键**：跨日作废必须发生在闹钟循环之前。

    `heartbeat_housekeeping()` 在心跳分支里跑，而闹钟循环在它之后——这个顺序是
    "昨天积压的十几条不会在开盘第一秒全部开跑"的唯一保证。把作废挪到闹钟循环
    之后，两条独立测试（作废测试 + 预算跳过测试）各自都还是绿的，只有这条会红。

    **必须让心跳回合真的成功**（用真实 `Budget` + 假 LLM）：心跳若因预算不足
    提前返回，会顺手把 `budget_exhausted` 置真，于是闹钟循环本来就被跳过——
    那样无论顺序对不对都看不到差异，测试就成了假绿。
    """
    from agent.context import Rendered
    from agent.loop import Deps
    from agent.main import AgentMain
    from agent.schedule import schedule_alarm
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from llm.budget import Budget
    from llm.tools import ToolRegistry
    from tests.agent.test_loop import FakeLlm
    from tests.agent.test_shadow_and_drill import FakeNtfy, _silent
    from agent.push import AgentPusher
    from tests.helpers import SH
    from datetime import datetime, timedelta

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "order.db")
    migrate(conn)
    open_day = datetime(2026, 11, 20, 9, 15, tzinfo=SH)   # 次日开盘前第一次心跳
    clock = FixedClock(open_day)

    # 昨天积累的两条过期闹钟
    for i, note in enumerate(("昨 1", "昨 2")):
        due = (open_day - timedelta(days=1)).replace(hour=14, minute=i)
        schedule_alarm(conn, turn_id=1, due_at=due.isoformat(), market="cn_main",
                       target="sh600519", note=note, now=due)

    llm = FakeLlm([_silent("盘前无变化"), _silent("回访完成")])
    deps = Deps(conn=conn, cfg=cfg, clock=clock, llm=llm,
                budget=Budget(conn, "agent", cfg.llm.budget.agent),
                tools=ToolRegistry(conn, cfg, clock, mcp=None),
                render=lambda t: Rendered("…", 1, False, {}),
                pusher=AgentPusher(conn, FakeNtfy(), cfg, clock),
                memory=None, westock=None)
    deps.conn_factory = lambda: conn
    deps.alert = lambda *a, **k: None

    main = AgentMain(deps, clock)
    # **不要手动设 `next_heartbeat_at`**：真实初始化留的是 None，`_once()` 会把它
    # 设成 `next_heartbeat(now)`——09:15 时给的是 09:17，于是这一轮心跳**不到期**。
    # 早期版本这条测试手动设成 `open_day` 让心跳立刻到期，反而掩盖了真实缺陷：
    # 把作废逻辑放进 `heartbeat_housekeeping()` 时测试照样绿，而生产会在启动后
    # 的头 2 分钟里让昨天的闹钟开跑（已用真实路径复现）。
    main._last_day = clock.today()

    await main._once()

    assert main.budget_exhausted is False, "前提不成立：心跳因预算不足提前返回了"
    assert conn.execute(
        "SELECT count(*) FROM agent_turn WHERE wake_kind = 'alarm'").fetchone()[0] == 0, (
        "昨天的闹钟在开盘第一时间就跑起来了：作废与闹钟循环的顺序不对")
    statuses = [r[0] for r in conn.execute("SELECT status FROM agent_schedule")]
    assert statuses == ["cancelled", "cancelled"], f"过期闹钟没有作废：{statuses}"


async def test_sweep_keeps_todays_alarms_even_if_late(tmp_path):
    """**作废判据是日期，不是"迟了多久"**（v0.2.90 修的 M3）。

    上一版用 `now - 1 小时` 当阈值，会误杀两类**最该跑**的闹钟（评审实测）：

    · 昨天排的、今天 09:20 的回访 —— 进程 10:30 才拉起（重启/盘后补拉）
    · 09:30 的正常回访 —— 盘中短暂重启，10:40 才恢复

    这两条的日期都是今天、语境完全有效，只是执行晚了。真正该杀的只有
    "日期属于更早交易日"的陈货：隔天回答的是昨天的问题。

    顺带钉住另一个坑：原来 `max(当天零点, now-1h)` 里零点分支是死代码
    （盘中 `now-1h` 恒晚于零点），写着两条判据实际只有一条在跑。
    """
    from agent.main import AgentMain
    from agent.schedule import due_alarms, schedule_alarm
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from tests.helpers import SH
    from datetime import datetime, timedelta

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "sweep.db")
    migrate(conn)
    now = datetime(2026, 11, 19, 10, 40, tzinfo=SH)      # 盘中重启，10:40 才恢复

    cases = {
        "昨天的陈货": datetime(2026, 11, 18, 14, 1, tzinfo=SH),
        "今天迟到 70 分钟（昨排今开）": datetime(2026, 11, 19, 9, 30, tzinfo=SH),
        "今天迟到 10 分钟（正常）": datetime(2026, 11, 19, 10, 30, tzinfo=SH),
        "今天稍后到期": datetime(2026, 11, 19, 11, 0, tzinfo=SH),
    }
    for note, due in cases.items():
        schedule_alarm(conn, turn_id=1, due_at=due.isoformat(), market="cn_main",
                       target="sh600519", note=note, now=due)

    class _Deps:
        def __init__(self):
            self.conn = conn
            self.cfg = cfg
            self.clock = FixedClock(now)

    cancelled = AgentMain(_Deps(), FixedClock(now)).sweep_stale_alarms(now)

    rows = dict(conn.execute("SELECT note, status FROM agent_schedule").fetchall())
    assert rows["昨天的陈货"] == "cancelled", "跨日的陈货没作废，明天开盘会补跑昨天的判断"
    assert cancelled == 1, f"只该作废 1 条，实际 {cancelled}"
    for note in ("今天迟到 70 分钟（昨排今开）", "今天迟到 10 分钟（正常）",
                 "今天稍后到期"):
        assert rows[note] == "pending", f"今天的闹钟被误杀了：{note}"


async def test_housekeeping_actually_persists(tmp_path):
    """**`heartbeat_housekeeping()` 的两条 UPDATE 必须真的落库**。

    抽 `sweep_stale_alarms()` 时，把原来函数末尾的 `conn.commit()` 一起搬走了——
    两条 UPDATE 于是在事务里悬着，连接关闭时被隐式回滚，**没有任何报错**：
    escalated 事件永不回收、确认卡永不过期。

    这类"忘了 commit"用普通断言抓不到（同一连接里查得到未提交的数据），
    必须另开一个连接去查。
    """
    from agent.main import AgentMain
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from tests.helpers import SH
    from datetime import datetime, timedelta

    cfg = load_config(Path("config"))
    db = tmp_path / "hk.db"
    conn = connect(db)
    migrate(conn)
    now = datetime(2026, 11, 19, 10, 30, tzinfo=SH)
    clock = FixedClock(now)

    # 一条早就该回收的 escalated 事件
    old_ts = (now - timedelta(minutes=90)).isoformat()
    conn.execute(
        "INSERT INTO event_log(ts, trade_date, market, kind, scope, target, payload,"
        " salience, escalated, agent_status, escalated_at, dedupe_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (old_ts, "20261119", "cn_main", "price", "target", "sh600519", "{}",
         1.0, 1, "escalated", old_ts, "k1"))
    # 一张早就该过期的确认卡（确认卡的有效期是 24 小时，比事件回收的 30 分钟长得多）
    card_ts = (now - timedelta(hours=30)).isoformat()
    conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
        " fallback, market, pending_status) VALUES (?,?,?,?,?,?,?,?)",
        (card_ts, "heartbeat", "卡", 1, "silent", 0, "cn_main", "pending"))
    conn.commit()

    class _Deps:
        def __init__(self):
            self.conn = conn
            self.cfg = cfg
            self.clock = clock
            self.conn_factory = lambda: db

    AgentMain(_Deps(), clock).heartbeat_housekeeping()

    # **另开连接查**：同连接能看到未提交的改动，测不出漏 commit
    fresh = connect(db)
    status = fresh.execute("SELECT agent_status FROM event_log").fetchone()[0]
    pending = fresh.execute("SELECT pending_status FROM agent_turn").fetchone()[0]
    assert status == "pending_retry", f"escalated 事件没被回收（状态 {status}）：housekeeping 少了 commit"
    assert pending == "expired", f"确认卡没被标记过期（状态 {pending}）：housekeeping 少了 commit"


async def test_event_retry_terminates_via_gave_up(tmp_path):
    """**事件重试路径的死循环防线是 `gave_up` 两振出局，不是心跳节奏。**

    闹钟死循环的根因是「失败→退回→立刻又到期」没有任何终止条件。
    事件路径不一样：`journal.record` 记住第一次失败（`new → pending_retry`），
    第二次再失败就转 `gave_up`，从此不再被取出。

    这条测试钉住的是**那个终止条件**。第一版把它写成"事件重试只在心跳分支里跑"，
    结果把 `_pending_retry()` 挪到心跳分支外，测试照样绿——因为真正拦住循环的是
    `gave_up`，迁移心跳位置并不会让循环失控。测错了防线就是假绿。

    所以这里直接断言：不管跑多少轮，事件最终都落到 `gave_up`，且回合总数有上界。
    """
    from agent.context import Rendered
    from agent.loop import Deps
    from agent.main import AgentMain
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from llm.budget import Budget
    from llm.tools import ToolRegistry
    from tests.helpers import SH
    from datetime import datetime, timedelta

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "ev.db")
    migrate(conn)
    now = datetime(2026, 11, 19, 10, 30, tzinfo=SH)
    clock = FixedClock(now)
    for i in range(10):
        conn.execute(
            "INSERT INTO event_log(ts, trade_date, market, kind, scope, target, payload,"
            " salience, escalated, agent_status, dedupe_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now.isoformat(), "20261119", "cn_main", "price", "target", "sh600519",
             '{"kind":"price"}', 3.0, 0, "new", f"k{i}"))
    conn.commit()

    class _BoomLlm:
        async def chat(self, *a, **k):
            raise RuntimeError("LLM 挂了")

    deps = Deps(conn=conn, cfg=cfg, clock=clock, llm=_BoomLlm(),
                budget=Budget(conn, "agent", cfg.llm.budget.agent),
                tools=ToolRegistry(conn, cfg, clock, mcp=None),
                render=lambda t: Rendered("…", 1, False, {}),
                pusher=None, memory=None, westock=None)
    deps.conn_factory = lambda: conn
    deps.alert = lambda *a, **k: None

    main = AgentMain(deps, clock)
    main.budget_exhausted = True                 # 与事故当天同状态

    # 逼着心跳每轮都到期，让事件重试路径**每轮都真的跑一次**
    counts = []
    for _ in range(8):
        before = conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0]
        main.next_heartbeat_at = main.clock.now()      # 心跳立即到期
        await main._once()
        after = conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0]
        counts.append(after - before)

    statuses = dict(conn.execute(
        "SELECT agent_status, count(*) FROM event_log GROUP BY agent_status"))
    assert statuses == {"gave_up": 10}, (
        f"事件没有收敛到 gave_up（{statuses}）："
        "两振出局的终止条件失效了，这就是闹钟死循环的同一形状")

    # **必须有上界**：10 条事件最多 2 振 = 20 个回合，不能随轮数线性增长。
    # 每轮那个固定的 +1 是心跳自己的回合（`trigger_id IS NULL`），不是事件重试。
    assert sum(counts) <= 30, (
        f"8 轮共写了 {sum(counts)} 个回合，事件重试没有终止：{counts}")
    # 最后一轮只该有心跳回合，不该再有带事件的重试回合
    last = conn.execute(
        "SELECT trigger_id FROM agent_turn ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert last is None, (
        f"最后一轮仍有事件重试回合（trigger_id={last}）：循环没有收敛，counts={counts}")


async def test_budget_exhausted_alarm_is_cancelled_not_requeued(tmp_path):
    """**预算耗尽取消闹钟的路径必须有断言级测试**（评审 M1）。

    原来的 `test_budget_exhausted_does_not_rerun_alarms` 只覆盖"跳过闹钟循环"，
    而 `_run()` 里"预算耗尽 → cancelled 而不是 release_alarm"这一条**没有任何测试**：
    把 `exhausted` 改回 `False`（恢复事故形态）跑全量仍然全绿。

    这里走真实 `_run()`：预算耗尽的结果 → 闹钟必须 `cancelled`，
    且**不能**退回 `pending`（那正是每 2 秒重跑一次的源头）。
    """
    from agent.main import AgentMain
    from agent.schedule import due_alarms, schedule_alarm
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from agent.context import Rendered
    from agent.loop import Deps
    from llm.budget import Budget
    from llm.tools import ToolRegistry
    from tests.helpers import SH
    from datetime import datetime

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "bx.db")
    migrate(conn)
    now = datetime(2026, 11, 19, 10, 30, tzinfo=SH)
    clock = FixedClock(now)
    schedule_alarm(conn, turn_id=1, due_at=now.isoformat(), market="cn_main",
                   target="sh600519", note="回访", now=now)
    alarm = due_alarms(conn, now)[0]
    conn.execute("UPDATE agent_schedule SET status='fired' WHERE id=?", (alarm["id"],))
    conn.commit()

    class _BoomLlm:
        async def chat(self, *a, **k):
            raise RuntimeError("不该被调用")

    deps = Deps(conn=conn, cfg=cfg, clock=clock, llm=_BoomLlm(),
                budget=Budget(conn, "agent", (0, 0)),      # 额度已用尽
                tools=ToolRegistry(conn, cfg, clock, mcp=None),
                render=lambda t: Rendered("…", 1, False, {}),
                pusher=None, memory=None, westock=None)
    deps.conn_factory = lambda: conn
    deps.alert = lambda *a, **k: None

    main = AgentMain(deps, clock)
    await main._run(main._trigger("alarm", rounds=3, deadline=0,
                                  alarm_id=alarm["id"], target="sh600519",
                                  note="回访"))

    status, fired_turn = conn.execute(
        "SELECT status, fired_turn FROM agent_schedule WHERE id = ?",
        (alarm["id"],)).fetchone()
    assert status == "cancelled", (
        f"预算耗尽后闹钟状态是 {status!r}：退回 pending 就是每 2 秒重跑一次的死循环")
    assert fired_turn is None
    assert due_alarms(conn, now) == [], "预算耗尽的闹钟又被排回队列了"


async def test_heartbeat_advances_even_when_a_post_journal_step_raises(
        tmp_path, monkeypatch):
    """**心跳的下一跳必须在跑回合之前推进**（评审 S2，最严重的一条新发现）。

    `_run` 的顺序是「跑回合 → 记日志 → 推送/确认卡/排闹钟」。任何一步抛异常都会
    被 `run()` 吞掉（只发个 alert 继续循环）。原来 `self.next_heartbeat_at = nxt`
    写在 `await self._run(...)` **之后**，于是只要**日志写成功之后**炸一次，
    下一跳就永远不推进 —— `now >= next_heartbeat_at` 恒成立，**每 2 秒跑一次
    心跳回合**。这与 11-19 那 17998 行是完全相同的形状，只是触发器不同。

    **必须让日志写成功之后再炸**：LLM 自己抛异常会被 `run_with_deadline` 转成
    `error` 结果、`journal_record` 照常写库、`_run` 正常返回——那种情况下
    测试根本看不到差异（我第一版就是这么写的，改回旧实现照样绿）。
    这里用 `schedule_next` 回合 + 打桩 `schedule_alarm` 制造"记账后抛异常"。
    """
    from agent.context import Rendered
    from agent.loop import Deps
    from agent.main import AgentMain
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from llm.budget import Budget
    from llm.tools import ToolRegistry
    from tests.agent.test_loop import Step, ToolCall
    from tests.helpers import SH
    from datetime import datetime, timedelta

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "hb.db")
    migrate(conn)
    now = datetime(2026, 11, 19, 10, 30, tzinfo=SH)
    clock = FixedClock(now)

    class _ScheduleLlm:
        """返回一个排了闹钟的回合 —— 记账之后才会执行 `schedule_alarm`。"""

        async def chat(self, messages, tools=None, max_tokens=None, timeout_s=None,
                       deduct=None, est_in=0, on_attempt=None):
            from types import SimpleNamespace

            step = Step(tool_calls=[ToolCall("emit_decision", {
                "speak": False, "body": "", "why_silent": "等下次",
                "schedule_next": 30, "schedule_note": "回访"})])
            return SimpleNamespace(content=step.content, tool_calls=step.tool_calls,
                                   attempts=1, usage=step.usage)

    deps = Deps(conn=conn, cfg=cfg, clock=clock, llm=_ScheduleLlm(),
                budget=Budget(conn, "agent", cfg.llm.budget.agent),
                tools=ToolRegistry(conn, cfg, clock, mcp=None),
                render=lambda t: Rendered("…", 1, False, {}),
                pusher=None, memory=None, westock=None)
    deps.conn_factory = lambda: conn
    deps.alert = lambda *a, **k: None

    main = AgentMain(deps, clock)
    main.next_heartbeat_at = now            # 心跳立即到期

    def _boom(*a, **k):
        raise RuntimeError("排闹钟阶段炸了（记账已经成功）")

    monkeypatch.setattr("agent.main.schedule_alarm", _boom)

    before = main.next_heartbeat_at
    try:
        await main._once()
    except Exception:                       # noqa: BLE001 —— `run()` 会在外层吞掉
        pass

    # 前提：这一轮确实记账了（否则测的就不是"记账后抛异常"）
    assert conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0] == 1, \
        "前提不成立：日志没写成功，测的不是 post-journal 阶段"
    assert main.next_heartbeat_at > before, (
        f"记账之后炸了一次，下一跳就没推进（仍是 {before}）："
        "`now >= next_heartbeat_at` 恒成立，会每 2 秒重跑一次——"
        "这就是 11-19 那 17998 行的同一形状")

    # 再跑一轮：不该又触发心跳（否则就是循环）
    nxt = main.next_heartbeat_at
    try:
        await main._once()
    except Exception:                       # noqa: BLE001
        pass
    assert main.next_heartbeat_at == nxt, "第二跳又触发了：心跳在异常后仍然每轮重跑"


async def test_open_turn_has_attempt_cap(tmp_path, monkeypatch):
    """**开盘回合也要有次数上限**（评审 S2 的同源问题）。

    `_open_turn_done()` 判断"当天有没有 open 行"。回合若在 `journal_record`
    **之前**就失败（磁盘满、写锁超时、连接被关），那一行永远写不出来 ——
    `now >= OPEN_TURN_AT` 恒成立，于是每 2 秒重跑一次开盘回合。
    心跳可以靠"推进下一跳"解决，开盘回合没有下一跳，只能用当日尝试次数兜住。

    **必须让记账本身失败**：LLM 抛异常会被 `run_with_deadline` 转成 `error` 结果、
    日志照常落库、`_open_turn_done()` 随即为真——那种情况下有没有次数上限都一样，
    测试看不到差异（我第一版就是这么写的，去掉上限照样绿）。
    """
    from agent.context import Rendered
    from agent.loop import Deps
    from agent.main import OPEN_TURN_MAX_ATTEMPTS, AgentMain
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from llm.budget import Budget
    from llm.tools import ToolRegistry
    from tests.helpers import SH
    from datetime import datetime, timedelta

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "open.db")
    migrate(conn)
    now = datetime(2026, 11, 19, 9, 40, tzinfo=SH)      # 过了 09:26
    clock = FixedClock(now)

    class _SilentLlm:
        async def chat(self, *a, **k):
            from types import SimpleNamespace

            return SimpleNamespace(content="", tool_calls=[
                {"id": "c1", "function": {"name": "emit_decision", "arguments":
                 '{"speak": false, "body": "", "why_silent": "无事"}'}}],
                attempts=1, usage=None)

    deps = Deps(conn=conn, cfg=cfg, clock=clock, llm=_SilentLlm(),
                budget=Budget(conn, "agent", cfg.llm.budget.agent),
                tools=ToolRegistry(conn, cfg, clock, mcp=None),
                render=lambda t: Rendered("…", 1, False, {}),
                pusher=None, memory=None, westock=None)
    deps.conn_factory = lambda: conn
    deps.alert = lambda *a, **k: None

    main = AgentMain(deps, clock)
    main.next_heartbeat_at = now + timedelta(hours=1)   # 心跳不在窗口内

    attempts = {"n": 0}

    def _boom(*a, **k):
        attempts["n"] += 1
        raise RuntimeError("记账失败（磁盘满/写锁超时）")

    monkeypatch.setattr("agent.main.journal_record", _boom)

    for _ in range(10):                     # 模拟 20 秒的轮询
        try:
            await main._once()
        except Exception:                   # noqa: BLE001
            pass

    assert attempts["n"] <= OPEN_TURN_MAX_ATTEMPTS, (
        f"开盘回合试了 {attempts['n']} 次（上限 {OPEN_TURN_MAX_ATTEMPTS}）："
        "记账失败时会每 2 秒重跑一次，直到收盘")
    assert main._open_attempts == OPEN_TURN_MAX_ATTEMPTS


async def test_timeout_turn_releases_the_alarm(tmp_path):
    """**超时回合也要把闹钟退回去**（评审 L2）。

    原来只有 `error`/`invalid` 会退，超时回合把闹钟永久留在 `fired`：
    模型说的"我 30 分钟后回来看"就静默作废，与最初那个"失败即永久丢失"
    是同一个毛病。`release_alarm` 的 docstring 本来就写明了
    "LLM 挂掉/预算耗尽/**超时**"，实现只覆盖了前两种。

    现在它有 60 秒退避与 3 次上限，退回不会成环。
    """
    from agent.main import AgentMain
    from agent.schedule import due_alarms, schedule_alarm
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from tests.helpers import SH
    from datetime import datetime, timedelta

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "to.db")
    migrate(conn)
    now = datetime(2026, 11, 19, 10, 30, tzinfo=SH)
    clock = FixedClock(now)
    schedule_alarm(conn, turn_id=1, due_at=now.isoformat(), market="cn_main",
                   target="sh600519", note="回访", now=now)
    alarm = due_alarms(conn, now)[0]
    conn.execute("UPDATE agent_schedule SET status='fired' WHERE id=?", (alarm["id"],))
    conn.commit()

    from types import SimpleNamespace

    class _Deps:
        def __init__(self):
            self.conn = conn
            self.cfg = cfg
            self.clock = clock
            self.pusher = None

    main = AgentMain(_Deps(), clock)

    from agent.decision import TurnDecision
    from agent.loop import TurnResult

    async def _fake_run_with_deadline(*a, **k):
        return TurnResult(decision=TurnDecision.timeout("回合超时"),
                          rounds=1, tool_calls=[], rendered=None,
                          usage=None, fallback=0, latency_ms=30000,
                          why="回合超时")

    import agent.main as m_mod
    orig = m_mod.run_with_deadline
    m_mod.run_with_deadline = _fake_run_with_deadline
    try:
        await main._run(main._trigger("alarm", rounds=3, deadline=0,
                                      alarm_id=alarm["id"], target="sh600519",
                                      note="回访"))
    finally:
        m_mod.run_with_deadline = orig

    status, attempts = conn.execute(
        "SELECT status, attempts FROM agent_schedule WHERE id = ?",
        (alarm["id"],)).fetchone()
    assert status == "pending", (
        f"超时之后闹钟停在 {status!r}：这次回访被永久丢弃了（L2）")
    assert attempts == 1
    later = now + timedelta(seconds=61)
    assert [a["id"] for a in due_alarms(conn, later)] == [alarm["id"]]
