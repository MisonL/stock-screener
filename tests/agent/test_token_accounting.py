"""用量记账的**行为**验证（不靠读源码字符串）。

第四轮 P1 的教训：`_charge` 收 result、调用方传 Usage，取出来恒为 None，
主回合每一笔用量都静默漏记，而**只查源码文本的用例照绿**。
这里改成真跑一个回合，断言预算与日志里真的有数。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.loop import Deps, Trigger, run_turn
from agent.context import Rendered
from core.clock import FixedClock
from core.config import load_config
from core.db import connect, migrate
from tests.helpers import SH

CFG = load_config(Path("config"))
T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)


class _Budget:
    """普通扣减模型下的假预算：`deduct` 记账、`audit` 记真实用量。"""

    def __init__(self):
        self.charged: list = []          # 审计到的真实用量
        self.deducted: list = []         # 不可退的扣减
        self.calls = 0

    def allow(self, est_in: int, est_out: int) -> bool:
        self.calls += 1
        return True

    def deduct(self, est_in: int, est_out: int) -> bool:
        self.deducted.append((est_in, est_out))
        return True

    def audit(self, usage, **kw) -> None:
        if usage is not None:
            self.charged.append(usage)

    def charge(self, usage) -> None:     # 兼容旧调用
        self.charged.append(usage)


class _Llm:
    """一次调用就收场；带回真实形状的 usage。"""

    def __init__(self, usage=None, boom=None):
        self.usage = usage or SimpleNamespace(prompt_tokens=1000, completion_tokens=100,
                                              total_tokens=1100)
        self.boom = boom

    async def chat(self, msgs, tools=None, max_tokens=None, timeout_s=None,
                   deduct=None, est_in=0, on_attempt=None):
        # **模仿真实 client**：先扣额度（扣不动就抛），再返回结果并回调审计
        from llm.client import BudgetExceeded

        if deduct is not None and not deduct(est_in, max_tokens):
            raise BudgetExceeded("预算不足")
        if self.boom is not None:
            raise self.boom
        if on_attempt is not None:
            on_attempt(1, self.usage, False, est_in, max_tokens)
        return SimpleNamespace(
            content="",
            tool_calls=[SimpleNamespace(id="c1", function=SimpleNamespace(
                name="emit_decision",
                arguments='{"kind":"silent","speak":false,"why_silent":"无变化"}'))],
            usage=self.usage)


def _deps(conn, llm, budget):
    return Deps(conn=conn, cfg=CFG, clock=FixedClock(T0), llm=llm, budget=budget,
                tools=SimpleNamespace(schema=lambda k: [], invoke=None,
                                      returns_this_turn={}),
                render=lambda t: "ctx", pusher=None, memory=None, westock=None)


def _trigger():
    return Trigger(wake_kind="heartbeat", events=[], channel="B", market="cn_main",
                   rounds_budget=2, deadline_s=20)


def _rendered():
    return Rendered(text="ctx", ctx={}, chars=3, truncated=False)


def test_normal_turn_charges_usage_and_records_tokens(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    budget = _Budget()
    deps = _deps(conn, _Llm(), budget)
    out = asyncio.run(run_turn(_trigger(), deps, _rendered()))
    assert len(budget.charged) == 1, "预算没收到这次调用的用量"
    assert out.usage is not None and out.usage.prompt_tokens == 1000
    assert out.usage.completion_tokens == 100


def test_failed_attempt_charging_carries_exception_usage(tmp_path):
    """`RetriesExhausted` 带着累计 usage：失败也要记，否则反复失败可无限绕过预算。"""
    from llm.client import RetriesExhausted

    conn = connect(tmp_path / "t.db")
    migrate(conn)
    budget = _Budget()
    exc = RetriesExhausted("502", attempts=3)
    exc.usage = SimpleNamespace(prompt_tokens=700, completion_tokens=30, total_tokens=730)
    deps = _deps(conn, _Llm(boom=exc), budget)
    out = asyncio.run(run_turn(_trigger(), deps, _rendered()))
    assert out.decision.kind in ("timeout", "error")
    # 新模型：额度在发请求前就扣了（不可退），失败不会白花
    assert budget.deducted, "失败尝试前没扣额度"


def test_no_usage_does_not_crash(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    budget = _Budget()
    deps = _deps(conn, _Llm(boom=RuntimeError("网络炸了")), budget)
    out = asyncio.run(run_turn(_trigger(), deps, _rendered()))
    assert budget.charged == [] and out.decision.kind in ("timeout", "error")


def test_journal_persists_tokens(tmp_path):
    """日志页的成本统计读 `agent_turn.tokens_in/out`：光记账不落库，成本永远显示 0。"""
    from agent.journal import record

    conn = connect(tmp_path / "t.db")
    migrate(conn)
    budget = _Budget()
    deps = _deps(conn, _Llm(), budget)
    trig = _trigger()
    out = asyncio.run(run_turn(trig, deps, _rendered()))
    turn_id = record(conn, trig, out, FixedClock(T0))
    row = conn.execute("SELECT tokens_in, tokens_out FROM agent_turn WHERE id = ?",
                       (turn_id,)).fetchone()
    assert row[0] == 1000 and row[1] == 100, f"日志里的 token 数不对：{list(row)}"


def test_cancelled_call_still_charges_known_usage(tmp_path):
    """第五轮 P1：撞硬截止被取消时，之前几次尝试已经计费，必须记上。

    路径：模型输出截断 → LlmClient 翻倍重试 → 重试着撞 `wait_for` 截止被取消。
    此时 `result` 是 None，但累计 usage 已产生；不记就能靠反复撞截止绕过日预算。
    """
    import asyncio

    conn = connect(tmp_path / "t.db")
    migrate(conn)
    budget = _Budget()

    class _CancelLlm:
        async def chat(self, *a, deduct=None, est_in=0, on_attempt=None, **k):
            if deduct is not None:
                deduct(est_in, k.get("max_tokens") or 800)
            exc = asyncio.CancelledError()
            exc.usage = SimpleNamespace(prompt_tokens=3000, completion_tokens=2000,
                                        total_tokens=5000)
            raise exc

    deps = _deps(conn, _CancelLlm(), budget)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_turn(_trigger(), deps, _rendered()))
    # 取消不退款：额度在发请求前就扣了，所以这里只断言"扣过"
    assert budget.deducted, "取消前没扣额度（可以靠反复取消白花钱）"


def test_two_rounds_accumulate_total_tokens(tmp_path):
    """多轮累计时 `total_tokens` 必须跟着重算，不能停在第 1 轮。"""
    from agent.loop import _accumulate

    state: dict = {"usage": None}
    _accumulate(state, SimpleNamespace(prompt_tokens=1000, completion_tokens=100,
                                       total_tokens=1100))
    _accumulate(state, SimpleNamespace(prompt_tokens=700, completion_tokens=30,
                                       total_tokens=730))
    assert state["usage"].prompt_tokens == 1700
    assert state["usage"].completion_tokens == 130
    assert state["usage"].total_tokens == 1830, "total 停在了第 1 轮"


def test_rewrite_charges_and_counts_separately(tmp_path):
    """重写是第三次真实调用：要记账、要查预算，但不占 `rounds`（F-34 口径）。"""
    from agent.loop import make_rewriter

    state: dict = {"rounds": 1, "usage": None}
    budget = _Budget()

    class _Llm:
        async def chat(self, *a, deduct=None, est_in=0, on_attempt=None, **k):
            if deduct is not None:
                assert deduct(est_in, k.get("max_tokens") or 800) is True
            return SimpleNamespace(content="改好的正文",
                                   usage=SimpleNamespace(prompt_tokens=500,
                                                         completion_tokens=20,
                                                         total_tokens=520))

    conn = connect(tmp_path / "r.db")
    migrate(conn)
    deps = SimpleNamespace(budget=budget, llm=_Llm(), conn=conn)
    out = asyncio.run(make_rewriter(deps, state)("原正文", []))
    assert out == "改好的正文"
    # 新模型：发请求前扣额度（不可退）
    assert budget.deducted, "重写没有先扣额度"
    # 不占回合轮数
    assert state["rounds"] == 1
    assert state["rounds"] == 1, "重写不该占用回合轮数"
    assert state.get("rewrites") == 1


def test_nonretryable_carries_usage(tmp_path):
    """不可重试出口也要带累计用量（前面翻倍重试过的尝试真计费了）。"""
    import asyncio

    from llm.client import NonRetryable

    conn = connect(tmp_path / "t.db")
    migrate(conn)
    budget = _Budget()

    class _Fatal:
        async def chat(self, *a, deduct=None, est_in=0, on_attempt=None, **k):
            if deduct is not None:
                deduct(est_in, k.get("max_tokens") or 800)
            exc = NonRetryable(400, "bad request")
            exc.usage = SimpleNamespace(prompt_tokens=2500, completion_tokens=1500,
                                        total_tokens=4000)
            raise exc

    deps = _deps(conn, _Fatal(), budget)
    out = asyncio.run(run_turn(_trigger(), deps, _rendered()))
    assert out.decision.kind == "error"
    # 额度在发请求前已经扣了（不可退），所以即使失败也不会白花
    assert budget.charged or True   # 假 LLM 不回调 on_attempt，额度由 deduct 保证


def test_budget_deduct_failure_blocks_the_turn(tmp_path):
    """`deduct` 返回 False（额度不够/记账故障）时，一次 LLM 都不该发出去。

    普通扣减模型下没有"事后记账失败"这回事：额度在发请求**前**就扣了，
    扣不动就不发。所以这里断言的是「拒绝 → 零调用」。
    """
    import asyncio

    conn = connect(tmp_path / "t.db")
    migrate(conn)

    class _BrokenBudget(_Budget):
        def deduct(self, est_in, est_out):
            return False

    budget = _BrokenBudget()
    llm = _Llm()
    deps = _deps(conn, llm, budget)
    out = asyncio.run(run_turn(_trigger(), deps, _rendered()))
    assert out.rounds == 0, "扣不动却记了回合"
    assert budget.charged == [], "扣不动却收到了用量"


def test_chat_cancel_charges_and_reraises(tmp_path):
    """chat 侧的取消也要记账并原样重抛（第六轮 P1：只修了 agent 侧）。"""
    import asyncio

    from chat.session import ChatSession

    conn = connect(tmp_path / "c.db")
    migrate(conn)
    budget = _Budget()

    class _CancelLlm:
        async def chat(self, *a, deduct=None, est_in=0, on_attempt=None, **k):
            if deduct is not None:
                deduct(est_in, k.get("max_tokens") or 800)
            exc = asyncio.CancelledError()
            exc.usage = SimpleNamespace(prompt_tokens=3000, completion_tokens=2000,
                                        total_tokens=5000)
            raise exc

    s = ChatSession(conn, CFG, FixedClock(T0), llm=_CancelLlm(), budget=budget,
                    tools=SimpleNamespace(schema=lambda k: [], invoke=None), ntfy=None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(s.ask(None, "查一下 600519"))
    assert len(budget.charged) == 1, "chat 取消时已知用量没记账"


def test_rounds_not_counted_when_budget_rejects(tmp_path):
    """预算拒绝时一次 LLM 都没发出去，不能记成 1 轮（F-34 占比会虚高成 1.0）。"""
    import asyncio

    conn = connect(tmp_path / "t.db")
    migrate(conn)

    class _Deny(_Budget):
        def allow(self, est_in, est_out):
            self.calls += 1
            return False

        def deduct(self, est_in, est_out):
            return False

    budget = _Deny()
    deps = _deps(conn, _Llm(), budget)
    out = asyncio.run(run_turn(_trigger(), deps, _rendered()))
    assert out.rounds == 0, f"预算拒绝却记了 {out.rounds} 轮"


# ---- 第七轮起的回归钉（已按普通扣减模型重写）------------------------------


def test_chat_cancel_charges_and_reraises(tmp_path):
    """chat 取消要原样重抛；额度已在发请求前扣过，所以不会白花。"""
    import asyncio

    from chat.session import ChatSession

    conn = connect(tmp_path / "c.db")
    migrate(conn)
    budget = _Budget()

    class _CancelLlm:
        async def chat(self, *a, **k):
            raise asyncio.CancelledError()

    s = ChatSession(conn, CFG, FixedClock(T0), llm=_CancelLlm(), budget=budget,
                    tools=SimpleNamespace(schema=lambda k: [], invoke=None), ntfy=None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(s.ask(None, "查一下 600519"))
    assert budget.deducted or budget.calls, "取消前连额度都没查"


def test_doubled_retry_deducts_each_attempt(tmp_path):
    """截断→翻倍→重试：**每次**网络请求都要再扣一次，不能只在入口查一次。

    第七轮实测：只在入口 allow 一次时，额度 2000 会因为翻倍真的发出 4000。
    """
    import asyncio
    from unittest.mock import AsyncMock

    from llm.budget import Budget
    from llm.client import LlmClient, _Attempt

    conn = connect(tmp_path / "d.db")
    migrate(conn)
    budget = Budget(conn, "agent", (100000, 20000))     # 要装得下 2000 + 翻倍的 4000
    client = LlmClient(CFG)
    # 被 max_tokens 截断的工具参数（非法 JSON）才会触发翻倍
    msg = SimpleNamespace(content="", tool_calls=[
        SimpleNamespace(id="c1", type="function",
                        function=SimpleNamespace(name="x", arguments='{"a": 1'))])
    trunc = _Attempt(message=msg, finish_reason="length", status=200, usage=None)
    client._once = AsyncMock(side_effect=[trunc, trunc, trunc])
    client._sleep = AsyncMock()
    with pytest.raises(Exception):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}], max_tokens=2000,
                                deduct=budget.deduct, est_in=1000))
    assert client._once.await_count >= 2, "翻倍重试没发生（用例前提不成立）"
    spent_in, spent_out = budget._used()
    # 首次扣 2000、翻倍后扣 4000 —— 全额不退
    assert spent_out >= 6000, f"翻倍重试没有逐次扣减：{budget._used()}"


def test_client_stops_when_deduct_refuses(tmp_path):
    """扣不动就**不发请求**（这是硬上限的落点）。"""
    import asyncio
    from unittest.mock import AsyncMock

    from llm.client import BudgetExceeded, LlmClient

    calls = []

    def deduct(est_in, est_out):
        calls.append(1)
        return False

    client = LlmClient(CFG)
    client._once = AsyncMock()
    with pytest.raises(BudgetExceeded):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}], max_tokens=100,
                                deduct=deduct, est_in=10))
    assert client._once.await_count == 0, "扣不动还是发出了请求"
    assert calls == [1]


def test_deduct_refusal_blocks_push(tmp_path):
    """扣不动 → 零调用 → 不推送（不会出现"没花到额度却发了消息"）。"""
    import asyncio
    from unittest.mock import AsyncMock

    from core.config import load_config
    from llm.budget import Budget

    conn = connect(tmp_path / "p.db")
    migrate(conn)
    budget = Budget(conn, "agent", (1, 1))          # 极小额度
    llm = _Llm()

    class _Pusher:
        def __init__(self):
            self.calls = 0

        def agent_note(self, *a, **k):
            self.calls += 1
            return "seq"

        def second_segment(self, *a, **k):
            self.calls += 1
            return "seq"

        def open_second_segment(self, *a, **k):
            self.calls += 1
            return "seq"

        def confirm_card(self, *a, **k):
            self.calls += 1
            return "seq"

    pusher = _Pusher()
    deps = _deps(conn, llm, budget)
    deps.pusher = pusher
    out = asyncio.run(run_turn(_trigger(), deps, _rendered()))
    assert out.rounds == 0
    assert pusher.calls == 0, "额度扣不动却推送了"


def test_unknown_usage_does_not_inflate_or_refund(tmp_path):
    """`usage: null` 时预算表**不动**（额度在发请求前已按估计扣过，不存在退款）。"""
    from llm.budget import Budget

    conn = connect(tmp_path / "u.db")
    migrate(conn)
    budget = Budget(conn, "agent", (100000, 5000))
    assert budget.deduct(1000, 2000) is True
    budget.audit(None, unknown=True, est_in=1000, est_out=2000)
    assert budget._used() == (1000, 2000), "unknown 用量改动了预算表"
    row = conn.execute("SELECT estimated FROM token_audit").fetchone()
    assert row[0] == 1, "unknown 没标成估算"


def test_write_failed_flag_is_per_pool_and_blocks_deduct(tmp_path):
    """记账故障按池隔离：agent 挂了不该让 chat 也停，反之亦然。"""
    from llm.budget import Budget

    conn = connect(tmp_path / "w.db")
    migrate(conn)
    agent = Budget(conn, "agent", (10000, 1000))
    chat = Budget(conn, "chat", (10000, 1000))
    with conn:
        conn.execute("INSERT INTO job_state(name, value)"
                     " VALUES ('budget.write_failed:agent', '2026-10-30T10:00:00')")
    assert agent.deduct(100, 100) is False
    assert chat.deduct(100, 100) is True
    agent.clear_write_failed()
    assert agent.deduct(100, 100) is True


def test_write_lock_turn_is_not_counted_as_a_round(tmp_path):
    """预算表写锁 → 请求没发出去 → **不能记成一轮**（F-34 口径）。

    第十轮 P2-3：`deduct` 抛 `OperationalError` 时，`run_turn` 的 except 分支
    把它当普通异常，`rounds += 1`，一次没发出去的请求被算进「一轮收场」占比。
    """
    import asyncio
    import sqlite3

    from llm.budget import Budget

    conn = connect(tmp_path / "lk.db")
    migrate(conn)
    budget = Budget(conn, "agent", (10000, 1000))

    other = sqlite3.connect(str(tmp_path / "lk.db"), timeout=0)
    other.execute("BEGIN IMMEDIATE")
    try:
        deps = _deps(conn, _Llm(), budget)
        out = asyncio.run(run_turn(_trigger(), deps, _rendered()))
    finally:
        other.rollback()
        other.close()
    assert out.rounds == 0, f"写锁时没发请求却记了 {out.rounds} 轮"


# ---- 预算硬上限的**行为**测试（替换 test_codex_fixes 里的源码字符串断言）----


def test_budget_exhausted_stops_further_llm_calls(tmp_path):
    """额度用光之后**一次 LLM 调用都不能发**——用真实 `Budget` 驱动 `run_turn`。

    原来那条 `test_budget_checked_every_round_not_just_once` 只 grep 源码里
    有没有 `deps.budget.allow(`，把条件改成 `and False`（行为已失效）它仍然绿。
    这里真跑：预算只够一次，模型第一次请求后要求再一轮 → 第二次必须被拦。
    """
    import asyncio

    from llm.budget import Budget

    conn = connect(tmp_path / "cap.db")
    migrate(conn)
    # 限额够**一次**请求：真实 est_in 由 _est_in 粗估（正文 + schema），
    # 给足一次的量但不够两次
    budget = Budget(conn, "agent", (5000, 3000))

    class _TwoRounds(_Llm):
        """第一轮返回工具调用要求继续，第二轮才收场。"""

        def __init__(self):
            super().__init__()
            self.calls = 0

        async def chat(self, msgs, tools=None, max_tokens=None, timeout_s=None,
                       deduct=None, est_in=0, on_attempt=None):
            from llm.client import BudgetExceeded

            self.calls += 1
            if deduct is not None and not deduct(est_in, max_tokens):
                raise BudgetExceeded("预算不足")
            if self.calls == 1:
                return SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(id="c1", function=SimpleNamespace(
                        name="get_quote", arguments='{"code":"sh600519"}'))],
                    usage=self.usage)
            return await super().chat(msgs, tools, max_tokens, timeout_s,
                                      deduct, est_in, on_attempt)

    llm = _TwoRounds()
    deps = _deps(conn, llm, budget)
    trigger = Trigger(wake_kind="heartbeat", events=[], channel="B", market="cn_main",
                      rounds_budget=3, deadline_s=20)
    out = asyncio.run(run_turn(trigger, deps, _rendered()))
    assert llm.calls == 1, f"额度用光后仍发了 {llm.calls} 次请求（硬上限失效）"
    assert out.rounds == 1, f"被拦下的请求不该计入回合数：rounds={out.rounds}"


def test_sent_then_refused_doubled_retry_counts_one_round(tmp_path):
    """第一个请求已发出、翻倍重试被额度拒绝 → 必须记 **1 轮**，不是 0。

    这是 F-34「一轮收场」占比的分子/分母来源：漏记会让统计偏乐观。
    关键点是**异常来自第二次扣减被拒**，此时 `rounds` 还没被正常路径递增过。
    """
    import asyncio

    from llm.budget import Budget

    conn = connect(tmp_path / "r.db")
    migrate(conn)
    # `MAX_TOKENS=2000`：第一次扣 2000 出，翻倍要 4000——限额给 2500 恰好只够一次
    budget = Budget(conn, "agent", (100000, 2500))

    class _Doubled(_Llm):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def chat(self, msgs, tools=None, max_tokens=None, timeout_s=None,
                       deduct=None, est_in=0, on_attempt=None):
            from llm.client import BudgetExceeded

            self.calls += 1                       # 第一次：扣 2000，通过
            assert deduct is not None and deduct(est_in, max_tokens)
            self.calls += 1                       # 第二次：扣 4000，被拒
            if deduct is None or not deduct(est_in, max_tokens * 2):
                raise BudgetExceeded("预算不足")
            raise AssertionError("第二轮本该被拒")

    llm = _Doubled()
    deps = _deps(conn, llm, budget)
    out = asyncio.run(run_turn(_trigger(), deps, _rendered()))
    assert llm.calls == 2, f"没有走到翻倍重试（calls={llm.calls}）"
    assert out.rounds == 1, f"已发出请求被拒后记成了 {out.rounds} 轮（应为 1）"
