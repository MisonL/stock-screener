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

    before = conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0]

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

    await main._once()

    after = conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0]
    assert after == before, (
        f"预算耗尽后仍然跑了 {(after - before)} 个空转回合："
        "这正是生产事故里一天 17885 行的来源")
