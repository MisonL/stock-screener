"""AI 对话（假 LLM 回放）。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from chat.session import ChatSession, context_snapshot, struct_answer
from core.clock import FixedClock
from core.config import load_config
from core.db import connect, migrate
from llm.prompts import CHAT_SYSTEM_PROMPT, SYSTEM_PROMPT
from tests.helpers import SH

CFG = load_config(Path("config"))
T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)
DAY = 20261030


class Call:
    def __init__(self, name, args=None):
        self.id = name
        self.function = SimpleNamespace(name=name,
                                        arguments=json.dumps(args or {}, ensure_ascii=False))


class Step:
    def __init__(self, content="", tool_calls=None, exc=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.exc = exc
        self.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)


class FakeLlm:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.sent = []

    async def chat(self, messages, tools=None, max_tokens=None, timeout_s=None,
                   deduct=None, est_in=0, on_attempt=None):
        self.sent.append((messages, tools))
        self.sent_max_tokens = getattr(self, "sent_max_tokens", [])
        self.sent_max_tokens.append(max_tokens)
        step = self.script.pop(0) if self.script else Step(content="（没有回答）")
        if step.exc:
            raise step.exc
        return step


class FakeTools:
    def __init__(self, returns=None):
        self.returns = returns or {}
        self.calls = []

    def schema(self, kind):
        assert kind == "chat"
        return [{"function": {"name": "get_quote"}}]

    def invoke(self, name, args, kind):
        assert kind == "chat"
        self.calls.append((name, args))
        return self.returns.get(name, {"ok": True, "data": {}})


class FakeNtfy:
    def __init__(self):
        self.published = []

    def publish(self, m):
        self.published.append(m)
        return f"ntfy-{len(self.published)}"


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "chat.db")
    migrate(conn)
    clock = FixedClock(T0)
    llm = FakeLlm()
    tools = FakeTools()
    ntfy = FakeNtfy()
    session = ChatSession(conn, CFG, clock, llm=llm, budget=FakeBudget(),
                          tools=tools, ntfy=ntfy)
    return SimpleNamespace(conn=conn, clock=clock, llm=llm, tools=tools,
                           ntfy=ntfy, session=session)


class FakeBudget:
    def allow(self, **kw):
        return True

    def charge(self, usage):
        pass


def ask(env, text, session_id=None):
    return asyncio.run(env.session.ask(session_id, text))


# ---- 基本对话 ------------------------------------------------------------

def test_prompt_has_no_emit_decision():
    """chat 工具集没有 emit_decision：注入盯盘提示词会让模型每轮调不存在的工具。"""
    assert "emit_decision" in SYSTEM_PROMPT
    assert "emit_decision" not in CHAT_SYSTEM_PROMPT


def test_second_system_is_context_snapshot(env):
    """F21：每轮第二条 system 是上下文快照，含日期/时段/池子规模。"""
    env.conn.execute(
        "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
        " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean,"
        " pos_in_box) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (DAY, "cn_main", "sh600519", 1, 80, "{}", 20, 1700.0, 1600.0, 1700.0, 1600.0,
         1680.0, 1000.0, 0.8))
    env.conn.commit()
    env.llm.script = [Step(content="好的")]
    ask(env, "观察池怎么样")
    messages = env.llm.sent[0][0]
    assert messages[0]["role"] == "system" and messages[0]["content"] == CHAT_SYSTEM_PROMPT
    assert messages[1]["role"] == "system"
    assert "2026-10-30" in messages[1]["content"] and "交易时段" in messages[1]["content"]
    assert "观察池 1 只" in messages[1]["content"]


def test_snapshot_updates_each_turn(env):
    env.llm.script = [Step(content="一"), Step(content="二")]
    ask(env, "第一问", session_id=1)
    env.conn.execute(
        "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
        " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean,"
        " pos_in_box) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (DAY, "cn_main", "sh600519", 1, 80, "{}", 20, 1700.0, 1600.0, 1700.0, 1600.0,
         1680.0, 1000.0, 0.8))
    env.conn.commit()
    ask(env, "第二问", session_id=1)
    first, second = env.llm.sent[0][0][1]["content"], env.llm.sent[1][0][1]["content"]
    assert "观察池 0 只" in first and "观察池 1 只" in second


def test_quote_is_grounded_against_tool_return(env):
    """答案里的价格必须与 `get_quote.data.price` 一致并通过出处校验。"""
    price = 1680.0
    env.tools.returns["get_quote"] = {"ok": True, "data": {"price": price},
                                      "as_of": T0.isoformat(), "reason": None, "adjust": None}
    env.llm.script = [
        Step(tool_calls=[Call("get_quote", {"code": "sh600519"})]),
        Step(content=f"现价 {price:.2f}（get_quote.data.price）"),
    ]
    out = ask(env, "600519 现在什么情况")
    assert f"{price:.2f}" in out.text
    assert out.grounded.failed is False
    assert "数据校验失败" not in out.text


def test_refusal_without_numbers_is_returned_verbatim(env):
    text = "我不能预测未来的涨跌。"
    env.llm.script = [Step(content=text)]
    out = ask(env, "明天会涨多少")
    assert out.text == text and out.grounded.failed is False


# ---- 写工具确认卡 --------------------------------------------------------

def test_write_tool_emits_real_confirm_card(env):
    """needs_confirm 必须变成一张推得出去的卡，否则 4 个写工具永远落不了地。"""
    env.tools.returns["mcp_add_watch"] = {"ok": False, "reason": "needs_confirm"}
    env.llm.script = [
        Step(tool_calls=[Call("mcp_add_watch", {"code": "sh600519"})]),
        Step(content="已加入自选（其实没有）"),
    ]
    out = ask(env, "把 600519 加自选")
    assert "已发出确认卡" in out.text
    assert len(env.ntfy.published) == 1
    msg = env.ntfy.published[0]
    assert len(msg.actions) == 2 and "?t=" in msg.actions[0].url
    row = env.conn.execute(
        "SELECT kind, seq_id, level, status FROM push_log WHERE kind='confirm_card'").fetchone()
    assert row[0] == "confirm_card" and row[2] == "P2" and row[3] == "accepted"
    turn = env.conn.execute(
        "SELECT pending_action, pending_status, wake_kind FROM agent_turn").fetchone()
    assert json.loads(turn[0])["args"]["code"] == "sh600519"
    assert turn[1] == "pending" and turn[2] == "chat"


# ---- 降级 ----------------------------------------------------------------

def test_llm_down_falls_back_to_structured_answer(env):
    from llm.client import RetriesExhausted

    env.llm.script = [Step(exc=RetriesExhausted("502", attempts=3))]
    out = ask(env, "600519 现在什么情况")
    assert out.fallback is True
    assert out.text.startswith("AI 暂时不可用，以下是直接查询结果")


def test_fallback_structured_queries(env):
    env.conn.execute("INSERT INTO signals(ts, trade_date, market, code, kind, level, event_id,"
                     " seq, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                     (T0.isoformat(), DAY, "cn_main", "sh600519", "breakout", "P1", 1, 1, "{}"))
    env.conn.commit()
    env.conn.execute(
        "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
        " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean,"
        " pos_in_box) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (DAY, "cn_main", "sh600519", 1, 80, "{}", 20, 1700.0, 1600.0, 1700.0, 1600.0,
         1680.0, 1000.0, 0.8))
    env.conn.commit()
    assert "sh600519" in struct_answer(env.conn, env.clock, "600519 呢")
    assert "观察池前 10" in struct_answer(env.conn, env.clock, "观察池")
    assert "最近信号" in struct_answer(env.conn, env.clock, "信号")


def test_stale_data_is_labelled_in_session(env):
    env.conn.execute("INSERT INTO snapshot_cache(scope, as_of, payload) VALUES (?,?,?)",
                     ("market", (T0 - timedelta(minutes=2)).isoformat(), "{}"))
    env.conn.commit()
    env.llm.script = [Step(content="一切正常")]
    out = ask(env, "还好吗", session_id=1)      # chat_message 有外键，会话行得先存在
    assert out.text.startswith("数据截至 ")


# ---- 会话历史 ------------------------------------------------------------

def test_session_row_created_with_title(env):
    env.llm.script = [Step(content="答")]
    out = ask(env, "今天大盘和板块怎么样，值得看看吗")
    row = env.conn.execute("SELECT created_at, title FROM chat_session WHERE id=?",
                           (out.session_id,)).fetchone()
    assert row[0] == T0.isoformat() and len(row[1]) <= 20
    msgs = env.conn.execute("SELECT role, content FROM chat_message ORDER BY id").fetchall()
    assert [r[0] for r in msgs] == ["user", "assistant"]


def test_history_is_first_round_plus_last_ten(env):
    """第 13 轮时送给模型的历史 = 首轮 + 最近 10 轮。"""
    sid = None
    for i in range(12):
        env.llm.script = [Step(content=f"答{i}")]
        out = ask(env, f"第{i + 1}问", sid)
        sid = out.session_id
    env.llm.script = [Step(content="答12")]
    ask(env, "第13问", sid)
    messages = env.llm.sent[-1][0]
    user_msgs = [m["content"] for m in messages
                 if m["role"] == "user" and m["content"] not in ("第13问",)]
    assert user_msgs[0] == "第1问"                 # 首轮保留
    assert "第2问" not in user_msgs                # 第 2 轮被裁掉
    assert "第12问" in user_msgs                  # 最近 10 轮保留
    assert len(user_msgs) == 11                    # 首轮 + 10


# ---- 审查复核：单例并发与异常边界 ---------------------------------------

def test_two_concurrent_asks_do_not_share_tool_returns(caplog):
    """P0：`ChatSession` 是单例，工具返回必须是**请求级**局部变量。

    A 查过报价、B 没查却引用同一路径 → B 的出处校验必须失败（不能借 A 的数字过关）。
    """
    import asyncio
    import logging

    from core.config import load_config as _cfg
    from core.db import connect as _connect, migrate as _migrate
    from llm.budget import Budget as _B
    from core.clock import FixedClock as _FC

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db") if False else None
    # 复用本模块的 fixture 形状
    import tempfile

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db")
    _migrate(conn)
    cfg = _cfg(Path("config"))
    clk = _FC(T0)

    class Scripted:
        def __init__(self):
            self.sent = []

        async def chat(self, messages, tools=None, max_tokens=None, timeout_s=None,
                   deduct=None, est_in=0, on_attempt=None):
            self.sent.append(messages)
            # 第一条消息含工具调用 → A；不含 → B
            if len(self.sent) in (1, 3):
                return Step(tool_calls=[Call("get_quote", {"code": "sh600519"})])
            # 引用的数字与工具返回不同（A 的返回值是 10.0）；若是 A 的返回值泄漏过来
            # 就会「恰好对上」，这正是要抓的串台
            return Step(content="B 报价 99.99（get_quote.data.price）")

    tools = FakeTools({"get_quote": {"ok": True, "data": {"price": 10.0}}})
    llm = Scripted()
    s = ChatSession(conn, cfg, clk, llm=llm, budget=SimpleNamespace(
        allow=lambda **k: True, charge=lambda u: None), tools=tools, ntfy=FakeNtfy())

    async def both():
        # 顺序执行：先 A（两步，工具调用 + 回答），再 B（一步，无工具）
        await s.ask(None, "查一下 600519")
        return await s.ask(None, "报个价")

    out = asyncio.run(both())
    assert "数据校验失败" in out.text or out.grounded.failed is True, (
        "B 没调工具却借到了 A 的 get_quote 返回值")


def test_programming_errors_are_not_masked_as_llm_down():
    """P0：`NameError`/`TypeError` 不能被 `except Exception` 吞成「AI 暂时不可用」。"""
    import asyncio
    import tempfile

    from core.clock import FixedClock as _FC
    from core.config import load_config as _cfg
    from core.db import connect as _connect, migrate as _migrate

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db")
    _migrate(conn)
    cfg = _cfg(Path("config"))

    class BoomLlm:
        async def chat(self, *a, **k):
            raise NameError("STALE_TYPO")

    s = ChatSession(conn, cfg, _FC(T0), llm=BoomLlm(),
                    budget=SimpleNamespace(allow=lambda **k: True, charge=lambda u: None),
                    tools=FakeTools(), ntfy=FakeNtfy())
    with pytest.raises(NameError, match="STALE_TYPO"):
        asyncio.run(s.ask(None, "你好"))
    # 反过来：真正的外部故障必须降级
    from llm.client import RetriesExhausted

    class Down:
        async def chat(self, *a, **k):
            raise RetriesExhausted("502", attempts=3)

    s.llm = Down()
    out = asyncio.run(s.ask(None, "你好"))
    assert out.fallback is True


def test_ntfy_failure_closes_pending_and_changes_answer():
    """P0：确认卡推送失败时不能还答「已发出」，`pending_status` 也不能停 pending。

    否则用户虽没收到卡，那个 turn 仍可被确认端点执行——等于执行一个用户从未见过的
    写操作（审查者复现过）。"""
    import asyncio
    import tempfile

    from core.clock import FixedClock as _FC
    from core.config import load_config as _cfg
    from core.db import connect as _connect, migrate as _migrate

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db")
    _migrate(conn)
    cfg = _cfg(Path("config"))

    class DeadNtfy:
        def publish(self, m):
            raise RuntimeError("ntfy 502")

    class Llm:
        def __init__(self):
            self.n = 0

        async def chat(self, *a, **k):
            self.n += 1
            if self.n == 1:
                return Step(tool_calls=[Call("mcp_add_watch", {"code": "sh600519"})])
            return Step(content="好")

    tools = FakeTools({"mcp_add_watch": {"ok": False, "reason": "needs_confirm"}})
    s = ChatSession(conn, cfg, _FC(T0), llm=Llm(),
                    budget=SimpleNamespace(allow=lambda **k: True, charge=lambda u: None),
                    tools=tools, ntfy=DeadNtfy())
    out = asyncio.run(s.ask(None, "把 600519 加自选"))
    assert "发送失败" in out.text and "已发出" not in out.text
    row = conn.execute(
        "SELECT status, pending_status FROM push_log, agent_turn"
        " WHERE push_log.kind='confirm_card'").fetchone()
    assert row[0] == "failed"
    pend = conn.execute("SELECT pending_status FROM agent_turn").fetchone()[0]
    assert pend == "expired", "pending 没关掉，回调仍可执行一个用户没见过的写操作"


def test_concurrent_asks_leak_returns_to_later_request():
    """真并发：B 超时退出后，A 才暴露自己的工具调用。

    若工具返回挂在 `self` 上，A 在建 `_returns` **之前**就已经开始跑（它先过了
    `await asyncio.sleep`），B 清空的 `_returns` 之后 A 再 `setdefault(...).append(...)`
    写进去——而 B 已经渲染完，于是 B 的校验读到的是 A 的数字。
    没有实例级 `_returns` 时这条恒不成立。
    """
    import asyncio
    import tempfile

    from core.clock import FixedClock as _FC
    from core.config import load_config as _cfg
    from core.db import connect as _connect, migrate as _migrate

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db")
    _migrate(conn)
    cfg = _cfg(Path("config"))

    class Slow:
        def __init__(self):
            self.n = 0

        async def chat(self, messages, *a, **k):
            self.n += 1
            if self.n == 1:                        # A：先睡，让 B 跑完
                await asyncio.sleep(0.15)
                return Step(tool_calls=[Call("get_quote", {"code": "sh600519"})])
            if self.n == 2:                        # A 收尾
                return Step(content="A 报价 10.00（get_quote.data.price）")
            return Step(content="B 报价 99.99（get_quote.data.price）")

    s = ChatSession(conn, cfg, _FC(T0), llm=Slow(),
                    budget=SimpleNamespace(allow=lambda **k: True, charge=lambda u: None),
                    tools=FakeTools({"get_quote": {"ok": True, "data": {"price": 10.0}}}),
                    ntfy=FakeNtfy())

    async def go():
        async def a():
            return await s.ask(None, "查一下 600519")

        async def b():
            await asyncio.sleep(0.05)              # A 已进 LLM，B 插入
            return await s.ask(None, "报个价")

        return await asyncio.gather(a(), b())

    a_out, b_out = asyncio.run(go())
    assert a_out.grounded.failed is False          # A 自己有出处
    assert (b_out.grounded.failed is True or "数据校验失败" in b_out.text), \
        "B 没调工具却引用了 get_quote.data.price，说明借到了 A 的返回值"


def test_chat_deducts_non_refundable_before_each_request():
    """chat 走保守扣减：发请求前扣，结束后**没有任何退款**。

    旧模型（预留→对账）的漏洞已由第九轮实测确认：在途取消能把未知请求整笔退款。
    现在额度只减不增，预算表就是「已扣保守值」。
    """
    import asyncio
    import tempfile

    from core.clock import FixedClock as _FC
    from core.config import load_config as _cfg
    from core.db import connect as _connect, migrate as _migrate

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db")
    _migrate(conn)
    cfg = _cfg(Path("config"))
    seen = {}

    class _Budget:
        def allow(self, est_in, est_out):
            return True

        def deduct(self, est_in, est_out):
            seen["deducted"] = (est_in, est_out)
            return True

        def audit(self, usage, **kw):
            seen.setdefault("audited", []).append(usage)

        def settle(self, *a, **kw):                # pragma: no cover
            raise AssertionError("不该再调用 settle")

        def charge(self, *a, **kw):                # pragma: no cover
            raise AssertionError("不该再调用 charge")

    class _Llm:
        async def chat(self, messages, tools=None, max_tokens=None, timeout_s=None,
                       deduct=None, est_in=0, on_attempt=None):
            assert deduct is not None, "chat 没把扣减回调传下去"
            assert deduct(1200, max_tokens) is True
            from types import SimpleNamespace

            usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=50, total_tokens=1050)
            if on_attempt is not None:
                on_attempt(1, usage, False, 1200, max_tokens)
            return SimpleNamespace(content="好了", tool_calls=[], usage=usage)

    s = ChatSession(conn, cfg, _FC(T0), llm=_Llm(), budget=_Budget(),
                    tools=FakeTools(), ntfy=FakeNtfy())
    out = asyncio.run(s.ask(None, "你好"))
    assert out.text == "好了"
    assert seen["deducted"][0] == 1200
    assert seen["audited"][0].prompt_tokens == 1000


class _Orphan:
    def invoke(self, name, args, scope):
        self.calls.append((name, args))
        return self.returns.get(name, {"ok": True, "data": {}})


class FakeNtfy:
    def __init__(self):
        self.published = []

    def publish(self, m):
        self.published.append(m)
        return f"ntfy-{len(self.published)}"


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "chat.db")
    migrate(conn)
    clock = FixedClock(T0)
    llm = FakeLlm()
    tools = FakeTools()
    ntfy = FakeNtfy()
    session = ChatSession(conn, CFG, clock, llm=llm, budget=FakeBudget(),
                          tools=tools, ntfy=ntfy)
    return SimpleNamespace(conn=conn, clock=clock, llm=llm, tools=tools,
                           ntfy=ntfy, session=session)


class FakeBudget:
    def allow(self, **kw):
        return True

    def charge(self, usage):
        pass


def ask(env, text, session_id=None):
    return asyncio.run(env.session.ask(session_id, text))


# ---- 基本对话 ------------------------------------------------------------

def test_prompt_has_no_emit_decision():
    """chat 工具集没有 emit_decision：注入盯盘提示词会让模型每轮调不存在的工具。"""
    assert "emit_decision" in SYSTEM_PROMPT
    assert "emit_decision" not in CHAT_SYSTEM_PROMPT


def test_second_system_is_context_snapshot(env):
    """F21：每轮第二条 system 是上下文快照，含日期/时段/池子规模。"""
    env.conn.execute(
        "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
        " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean,"
        " pos_in_box) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (DAY, "cn_main", "sh600519", 1, 80, "{}", 20, 1700.0, 1600.0, 1700.0, 1600.0,
         1680.0, 1000.0, 0.8))
    env.conn.commit()
    env.llm.script = [Step(content="好的")]
    ask(env, "观察池怎么样")
    messages = env.llm.sent[0][0]
    assert messages[0]["role"] == "system" and messages[0]["content"] == CHAT_SYSTEM_PROMPT
    assert messages[1]["role"] == "system"
    assert "2026-10-30" in messages[1]["content"] and "交易时段" in messages[1]["content"]
    assert "观察池 1 只" in messages[1]["content"]


def test_snapshot_updates_each_turn(env):
    env.llm.script = [Step(content="一"), Step(content="二")]
    ask(env, "第一问", session_id=1)
    env.conn.execute(
        "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
        " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean,"
        " pos_in_box) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (DAY, "cn_main", "sh600519", 1, 80, "{}", 20, 1700.0, 1600.0, 1700.0, 1600.0,
         1680.0, 1000.0, 0.8))
    env.conn.commit()
    ask(env, "第二问", session_id=1)
    first, second = env.llm.sent[0][0][1]["content"], env.llm.sent[1][0][1]["content"]
    assert "观察池 0 只" in first and "观察池 1 只" in second


def test_quote_is_grounded_against_tool_return(env):
    """答案里的价格必须与 `get_quote.data.price` 一致并通过出处校验。"""
    price = 1680.0
    env.tools.returns["get_quote"] = {"ok": True, "data": {"price": price},
                                      "as_of": T0.isoformat(), "reason": None, "adjust": None}
    env.llm.script = [
        Step(tool_calls=[Call("get_quote", {"code": "sh600519"})]),
        Step(content=f"现价 {price:.2f}（get_quote.data.price）"),
    ]
    out = ask(env, "600519 现在什么情况")
    assert f"{price:.2f}" in out.text
    assert out.grounded.failed is False
    assert "数据校验失败" not in out.text


def test_refusal_without_numbers_is_returned_verbatim(env):
    text = "我不能预测未来的涨跌。"
    env.llm.script = [Step(content=text)]
    out = ask(env, "明天会涨多少")
    assert out.text == text and out.grounded.failed is False


# ---- 写工具确认卡 --------------------------------------------------------

def test_write_tool_emits_real_confirm_card(env):
    """needs_confirm 必须变成一张推得出去的卡，否则 4 个写工具永远落不了地。"""
    env.tools.returns["mcp_add_watch"] = {"ok": False, "reason": "needs_confirm"}
    env.llm.script = [
        Step(tool_calls=[Call("mcp_add_watch", {"code": "sh600519"})]),
        Step(content="已加入自选（其实没有）"),
    ]
    out = ask(env, "把 600519 加自选")
    assert "已发出确认卡" in out.text
    assert len(env.ntfy.published) == 1
    msg = env.ntfy.published[0]
    assert len(msg.actions) == 2 and "?t=" in msg.actions[0].url
    row = env.conn.execute(
        "SELECT kind, seq_id, level, status FROM push_log WHERE kind='confirm_card'").fetchone()
    assert row[0] == "confirm_card" and row[2] == "P2" and row[3] == "accepted"
    turn = env.conn.execute(
        "SELECT pending_action, pending_status, wake_kind FROM agent_turn").fetchone()
    assert json.loads(turn[0])["args"]["code"] == "sh600519"
    assert turn[1] == "pending" and turn[2] == "chat"


# ---- 降级 ----------------------------------------------------------------

def test_llm_down_falls_back_to_structured_answer(env):
    from llm.client import RetriesExhausted

    env.llm.script = [Step(exc=RetriesExhausted("502", attempts=3))]
    out = ask(env, "600519 现在什么情况")
    assert out.fallback is True
    assert out.text.startswith("AI 暂时不可用，以下是直接查询结果")


def test_fallback_structured_queries(env):
    env.conn.execute("INSERT INTO signals(ts, trade_date, market, code, kind, level, event_id,"
                     " seq, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                     (T0.isoformat(), DAY, "cn_main", "sh600519", "breakout", "P1", 1, 1, "{}"))
    env.conn.commit()
    env.conn.execute(
        "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
        " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean,"
        " pos_in_box) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (DAY, "cn_main", "sh600519", 1, 80, "{}", 20, 1700.0, 1600.0, 1700.0, 1600.0,
         1680.0, 1000.0, 0.8))
    env.conn.commit()
    assert "sh600519" in struct_answer(env.conn, env.clock, "600519 呢")
    assert "观察池前 10" in struct_answer(env.conn, env.clock, "观察池")
    assert "最近信号" in struct_answer(env.conn, env.clock, "信号")


def test_stale_data_is_labelled_in_session(env):
    env.conn.execute("INSERT INTO snapshot_cache(scope, as_of, payload) VALUES (?,?,?)",
                     ("market", (T0 - timedelta(minutes=2)).isoformat(), "{}"))
    env.conn.commit()
    env.llm.script = [Step(content="一切正常")]
    out = ask(env, "还好吗", session_id=1)      # chat_message 有外键，会话行得先存在
    assert out.text.startswith("数据截至 ")


# ---- 会话历史 ------------------------------------------------------------

def test_session_row_created_with_title(env):
    env.llm.script = [Step(content="答")]
    out = ask(env, "今天大盘和板块怎么样，值得看看吗")
    row = env.conn.execute("SELECT created_at, title FROM chat_session WHERE id=?",
                           (out.session_id,)).fetchone()
    assert row[0] == T0.isoformat() and len(row[1]) <= 20
    msgs = env.conn.execute("SELECT role, content FROM chat_message ORDER BY id").fetchall()
    assert [r[0] for r in msgs] == ["user", "assistant"]


def test_history_is_first_round_plus_last_ten(env):
    """第 13 轮时送给模型的历史 = 首轮 + 最近 10 轮。"""
    sid = None
    for i in range(12):
        env.llm.script = [Step(content=f"答{i}")]
        out = ask(env, f"第{i + 1}问", sid)
        sid = out.session_id
    env.llm.script = [Step(content="答12")]
    ask(env, "第13问", sid)
    messages = env.llm.sent[-1][0]
    user_msgs = [m["content"] for m in messages
                 if m["role"] == "user" and m["content"] not in ("第13问",)]
    assert user_msgs[0] == "第1问"                 # 首轮保留
    assert "第2问" not in user_msgs                # 第 2 轮被裁掉
    assert "第12问" in user_msgs                  # 最近 10 轮保留
    assert len(user_msgs) == 11                    # 首轮 + 10


# ---- 审查复核：单例并发与异常边界 ---------------------------------------

def test_two_concurrent_asks_do_not_share_tool_returns(caplog):
    """P0：`ChatSession` 是单例，工具返回必须是**请求级**局部变量。

    A 查过报价、B 没查却引用同一路径 → B 的出处校验必须失败（不能借 A 的数字过关）。
    """
    import asyncio
    import logging

    from core.config import load_config as _cfg
    from core.db import connect as _connect, migrate as _migrate
    from llm.budget import Budget as _B
    from core.clock import FixedClock as _FC

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db") if False else None
    # 复用本模块的 fixture 形状
    import tempfile

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db")
    _migrate(conn)
    cfg = _cfg(Path("config"))
    clk = _FC(T0)

    class Scripted:
        def __init__(self):
            self.sent = []

        async def chat(self, messages, tools=None, max_tokens=None, timeout_s=None,
                   deduct=None, est_in=0, on_attempt=None):
            self.sent.append(messages)
            # 第一条消息含工具调用 → A；不含 → B
            if len(self.sent) in (1, 3):
                return Step(tool_calls=[Call("get_quote", {"code": "sh600519"})])
            # 引用的数字与工具返回不同（A 的返回值是 10.0）；若是 A 的返回值泄漏过来
            # 就会「恰好对上」，这正是要抓的串台
            return Step(content="B 报价 99.99（get_quote.data.price）")

    tools = FakeTools({"get_quote": {"ok": True, "data": {"price": 10.0}}})
    llm = Scripted()
    s = ChatSession(conn, cfg, clk, llm=llm, budget=SimpleNamespace(
        allow=lambda **k: True, charge=lambda u: None), tools=tools, ntfy=FakeNtfy())

    async def both():
        # 顺序执行：先 A（两步，工具调用 + 回答），再 B（一步，无工具）
        await s.ask(None, "查一下 600519")
        return await s.ask(None, "报个价")

    out = asyncio.run(both())
    assert "数据校验失败" in out.text or out.grounded.failed is True, (
        "B 没调工具却借到了 A 的 get_quote 返回值")


def test_programming_errors_are_not_masked_as_llm_down():
    """P0：`NameError`/`TypeError` 不能被 `except Exception` 吞成「AI 暂时不可用」。"""
    import asyncio
    import tempfile

    from core.clock import FixedClock as _FC
    from core.config import load_config as _cfg
    from core.db import connect as _connect, migrate as _migrate

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db")
    _migrate(conn)
    cfg = _cfg(Path("config"))

    class BoomLlm:
        async def chat(self, *a, **k):
            raise NameError("STALE_TYPO")

    s = ChatSession(conn, cfg, _FC(T0), llm=BoomLlm(),
                    budget=SimpleNamespace(allow=lambda **k: True, charge=lambda u: None),
                    tools=FakeTools(), ntfy=FakeNtfy())
    with pytest.raises(NameError, match="STALE_TYPO"):
        asyncio.run(s.ask(None, "你好"))
    # 反过来：真正的外部故障必须降级
    from llm.client import RetriesExhausted

    class Down:
        async def chat(self, *a, **k):
            raise RetriesExhausted("502", attempts=3)

    s.llm = Down()
    out = asyncio.run(s.ask(None, "你好"))
    assert out.fallback is True


def test_ntfy_failure_closes_pending_and_changes_answer():
    """P0：确认卡推送失败时不能还答「已发出」，`pending_status` 也不能停 pending。

    否则用户虽没收到卡，那个 turn 仍可被确认端点执行——等于执行一个用户从未见过的
    写操作（审查者复现过）。"""
    import asyncio
    import tempfile

    from core.clock import FixedClock as _FC
    from core.config import load_config as _cfg
    from core.db import connect as _connect, migrate as _migrate

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db")
    _migrate(conn)
    cfg = _cfg(Path("config"))

    class DeadNtfy:
        def publish(self, m):
            raise RuntimeError("ntfy 502")

    class Llm:
        def __init__(self):
            self.n = 0

        async def chat(self, *a, **k):
            self.n += 1
            if self.n == 1:
                return Step(tool_calls=[Call("mcp_add_watch", {"code": "sh600519"})])
            return Step(content="好")

    tools = FakeTools({"mcp_add_watch": {"ok": False, "reason": "needs_confirm"}})
    s = ChatSession(conn, cfg, _FC(T0), llm=Llm(),
                    budget=SimpleNamespace(allow=lambda **k: True, charge=lambda u: None),
                    tools=tools, ntfy=DeadNtfy())
    out = asyncio.run(s.ask(None, "把 600519 加自选"))
    assert "发送失败" in out.text and "已发出" not in out.text
    row = conn.execute(
        "SELECT status, pending_status FROM push_log, agent_turn"
        " WHERE push_log.kind='confirm_card'").fetchone()
    assert row[0] == "failed"
    pend = conn.execute("SELECT pending_status FROM agent_turn").fetchone()[0]
    assert pend == "expired", "pending 没关掉，回调仍可执行一个用户没见过的写操作"


def test_concurrent_asks_leak_returns_to_later_request():
    """真并发：B 超时退出后，A 才暴露自己的工具调用。

    若工具返回挂在 `self` 上，A 在建 `_returns` **之前**就已经开始跑（它先过了
    `await asyncio.sleep`），B 清空的 `_returns` 之后 A 再 `setdefault(...).append(...)`
    写进去——而 B 已经渲染完，于是 B 的校验读到的是 A 的数字。
    没有实例级 `_returns` 时这条恒不成立。
    """
    import asyncio
    import tempfile

    from core.clock import FixedClock as _FC
    from core.config import load_config as _cfg
    from core.db import connect as _connect, migrate as _migrate

    conn = _connect(Path(tempfile.mkdtemp()) / "c.db")
    _migrate(conn)
    cfg = _cfg(Path("config"))

    class Slow:
        def __init__(self):
            self.n = 0

        async def chat(self, messages, *a, **k):
            self.n += 1
            if self.n == 1:                        # A：先睡，让 B 跑完
                await asyncio.sleep(0.15)
                return Step(tool_calls=[Call("get_quote", {"code": "sh600519"})])
            if self.n == 2:                        # A 收尾
                return Step(content="A 报价 10.00（get_quote.data.price）")
            return Step(content="B 报价 99.99（get_quote.data.price）")

    s = ChatSession(conn, cfg, _FC(T0), llm=Slow(),
                    budget=SimpleNamespace(allow=lambda **k: True, charge=lambda u: None),
                    tools=FakeTools({"get_quote": {"ok": True, "data": {"price": 10.0}}}),
                    ntfy=FakeNtfy())

    async def go():
        async def a():
            return await s.ask(None, "查一下 600519")

        async def b():
            await asyncio.sleep(0.05)              # A 已进 LLM，B 插入
            return await s.ask(None, "报个价")

        return await asyncio.gather(a(), b())

    a_out, b_out = asyncio.run(go())
    assert a_out.grounded.failed is False          # A 自己有出处
    assert (b_out.grounded.failed is True or "数据校验失败" in b_out.text), \
        "B 没调工具却引用了 get_quote.data.price，说明借到了 A 的返回值"


def test_budget_exceeded_falls_back_not_raises():
    """对话额度用满时返回结构化 fallback，**不能抛到 API 层变成 500**。

    第十轮 P1：`allow()` 通过但 `deduct()` 在真正发请求前判定扣不动，
    `BudgetExceeded` 不在 `_expected_errors()` 里，直接冒泡成 500。
    这是正常的日配额耗尽，不是故障。
    """
    import asyncio
    import tempfile

    from core.clock import FixedClock as _FC
    from core.config import load_config as _cfg
    from core.db import connect as _connect, migrate as _migrate

    conn = _connect(Path(tempfile.mkdtemp()) / "b.db")
    _migrate(conn)
    cfg = _cfg(Path("config"))

    class _Budget:
        def allow(self, est_in, est_out):
            return True

        def deduct(self, est_in, est_out):
            return False                 # 扣不动

        def audit(self, usage, **kw):
            pass

    class _Llm:
        async def chat(self, *a, deduct=None, est_in=0, on_attempt=None, **k):
            from llm.client import BudgetExceeded

            if deduct is not None and not deduct(est_in, k.get("max_tokens") or 800):
                raise BudgetExceeded("预算不足")
            raise AssertionError("扣不动还是发了请求")

    s = ChatSession(conn, cfg, _FC(T0), llm=_Llm(), budget=_Budget(),
                    tools=FakeTools(), ntfy=FakeNtfy())
    out = asyncio.run(s.ask(None, "查一下 600519"))
    assert out.fallback is True, "额度耗尽没有走 fallback"
    assert "额度" in (out.text or ""), f"没说清是额度问题：{out.text!r}"


async def test_max_tokens_config_reaches_the_chat_request(env):
    """**对话池也必须真的把 `llm.max_tokens.tools` 发出去**（v0.2.105）。

    第七轮评审：`ChatSession._max_out()` 本身有测试，但**生产调用点**没测。
    把 `out_tokens = self._max_out()` 改回 `MAX_TOKENS` —— 17 个测试全绿。
    和 agent 侧是同一个假绿形状。
    """
    from types import SimpleNamespace

    llm_d = dict(env.session.cfg.llm._d)
    llm_d["max_tokens"] = SimpleNamespace(tools=4321, plain=1111)
    object.__setattr__(env.session.cfg.llm, "_d", llm_d)

    await env.session.ask(None, "你好")
    assert env.llm.sent_max_tokens, "这一轮根本没发请求"
    assert env.llm.sent_max_tokens[0] == 4321, (
        f"实发 max_tokens={env.llm.sent_max_tokens[0]}，配置里是 4321：调用点没走配置")
