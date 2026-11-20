"""网页 AI 对话（计划 Task 14，spec §7.5.3 / §7.4 / D18）。

**`Deps` 不跨进程**：Task 11 的 `Deps` 是 agent 进程的组装点，api 进程拿不到它。
`ChatSession(conn, cfg, clock)` 在 api 进程里**自建依赖**，且必须在 app 启动时构造
一次并作为单例注入路由——每次请求新建会重建 `RateLimiter` 与 `httpx.Client`，
两个并发对话各自从零计时，westock 的 ≥300ms 间隔形同虚设（D18）。

历史保留**首轮 + 最近 10 轮**：首轮是「用户为什么进来」，丢掉它后面每轮都答非所问。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from llm.budget import CHARS_TO_TOKENS, Budget
from llm.client import LlmClient
from llm.grounding import GroundedText, enforce
from llm.prompts import CHAT_SYSTEM_PROMPT
from llm.tools import ToolRegistry

HISTORY_KEEP = 10                 # 最近 10 轮
ROUNDS = 6
#: 兜底值；真实来源是 `config/llm.yaml::llm.max_tokens.tools`（v0.2.105 接线）。
MAX_TOKENS = 2000
DEADLINE_S = 60
STALE_S = 60                      # 数据超过 60 秒且盘中 → 回答首行标注截止时刻                      # 数据超过 60 秒且盘中 → 回答首行标注截止时刻
WRITE_TOOLS = ("mcp_add_watch", "mcp_remove_watch", "mcp_set_alert", "mcp_cancel_alert")
NEEDS_CONFIRM_TEXT = "已发出确认卡，请在手机上确认"


#: 兜底硬上限（秒）。配置写 0（不设业务上限）时用它——上游彻底不回包时才会撞上。
#: 300 秒 ≈ 实测最慢（31 秒）的 10 倍，正常调用撞不到。
SAFETY_TIMEOUT_S = 300.0


def _call_timeout(remaining: float, configured: float) -> float:
    """单次调用超时（秒）。

    配置 >0 时取「本轮剩余」与配置的较小值；配置 <=0 表示不设业务上限，
    此时只用本轮剩余时间，但**仍不超过 `SAFETY_TIMEOUT_S`**。
    绝不能返回 0/负数/None：0 会被 SDK 当成立刻超时，None 则是"永远等"。
    """
    remaining = max(float(remaining), 1.0)
    configured = float(configured)
    limit = remaining if configured <= 0 else min(remaining, configured)
    return min(limit, SAFETY_TIMEOUT_S)


def _expected_errors() -> tuple[type[BaseException], ...]:
    """只有这些算「外部依赖坏了」，可以降级成结构化快捷指令。

    **不能写 `except Exception`**：`NameError`/`TypeError` 被吞掉后表现和「中转站挂了」
    一模一样，用户会一直去查网络，而真正的 bug 在自己的代码里。
    """
    from llm.client import BudgetExceeded, NonRetryable, RetriesExhausted

    # **`BudgetExceeded` 必须在这里**：日配额耗尽是完全正常的事（每天都会发生），
    # 不在这个集合里就会冒泡成 API 500，用户看到的是"服务出错"而不是
    # "今天聊够了"。它和"中转站挂了"是同一类：外部约束，应降级成快捷指令。
    return (RetriesExhausted, NonRetryable, BudgetExceeded, TimeoutError, asyncio_timeout())


def asyncio_timeout() -> type[BaseException]:
    import asyncio

    return asyncio.TimeoutError


@dataclass
class ChatAnswer:
    session_id: int
    text: str
    grounded: GroundedText
    as_of: str | None
    market: str
    fallback: bool = False


def context_snapshot(conn, clock) -> str:
    """一句话上下文（约 500 tokens），**每轮重算**。

    这是第二条 system（F21）：今天日期、是否交易时段、观察池规模、最近一次信号。
    模型不自己翻库——它看不到「今天几号」就只能瞎猜。
    """
    now = clock.now()
    today = clock.today()
    hm = now.strftime("%H:%M")
    in_session = "09:15" <= hm <= "15:05" and not ("11:30" <= hm < "13:00")
    pool = conn.execute("SELECT count(*) FROM pool WHERE trade_date = ?", (today,)).fetchone()[0]
    if not pool:
        row = conn.execute("SELECT max(trade_date) FROM pool").fetchone()
        if row and row[0]:
            pool = conn.execute("SELECT count(*) FROM pool WHERE trade_date = ?",
                                (row[0],)).fetchone()[0]
    last = conn.execute(
        "SELECT ts, code, kind, level FROM signals ORDER BY ts DESC LIMIT 1").fetchone()
    parts = [f"今天 {now.strftime('%Y-%m-%d')}（周{'一二三四五六日'[now.weekday()]}）",
             f"当前 {hm}，{'交易时段内' if in_session else '非交易时段'}",
             f"观察池 {int(pool)} 只"]
    if last is not None and last[0]:
        parts.append(f"最近一次信号：{last[1]} {last[2]} {last[3]}（{last[0]}）")
    else:
        parts.append("最近没有信号")
    return "；".join(parts) + "。"


class ChatSession:
    """api 进程里的对话会话。**单例**：由 `create_app` 构造一次并注入路由。"""


    def _max_out(self) -> int:
        """对话这次请求实际要发的 `max_tokens`：**配置优先**（v0.2.105）。

        此前三个 `llm.chat()` 调用点全部显式传 2000，而 YAML 里的
        `max_tokens.tools/plain` 从来没人读——改了完全不生效。
        """
        mt = getattr(getattr(self.cfg, "llm", None), "max_tokens", None)
        try:
            val = getattr(mt, "tools", None)
            if val and not isinstance(val, bool):
                got = int(val)
                # 与 `agent/loop.py::_max_out` 同样的校验：正数 + 整数
                if got > 0 and float(val) == got:
                    return got
        except (TypeError, ValueError):
            pass
        return MAX_TOKENS

    def __init__(self, conn, cfg, clock, llm=None, budget=None, tools=None,
                 ntfy=None, mcp=None):
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.llm = llm or LlmClient(cfg)
        # **池级子配置 `budget.chat`**：Budget 只认 tokens_in/out 两个限值，
        # 把整个 cfg.llm.budget 传进去会 KeyError
        self.budget = budget or Budget(conn, "chat", cfg.llm.budget.chat,
                                       clock=clock)
        if tools is not None:
            self.tools = tools
        else:
            from mcp.westock import WestockMcp

            self.tools = ToolRegistry(conn, cfg, clock, mcp=mcp or WestockMcp())
        self.ntfy = ntfy
        # **没有实例级 `_returns`**：`ChatSession` 是 api 进程里的单例，把「本轮工具
        # 返回」挂在 self 上，两个并发对话会互相污染——A 请求查过报价，B 请求没查也能
        # 拿 A 的数字通过出处校验，用户看到一条看起来正确的假数字。
        # 工具返回必须是 `ask()` 的局部变量；顺带用锁把同一会话的回合串起来，
        # 否则两轮的历史会交错写入。
        self._lock = None

    # ---- 主入口 ----------------------------------------------------------

    async def ask(self, session_id: int | None, text: str) -> ChatAnswer:
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        # `_ask` 里会先建会话并写 user 消息；超时时必须**复用它已分配的 id**，
        # 否则会再建一个会话：用户的问题在 A 会话、答案在 B 会话，历史直接断开。
        holder: dict = {"session_id": session_id}
        async with self._lock:                  # 同一实例的回合串行：历史不交错
            try:
                # **整回合硬截止**：`timeout_s` 是**每次尝试**的超时，连续三次网络超时
                # 加上退避能跑 180 秒以上，60 秒的回合上限就没了
                return await asyncio.wait_for(
                    self._ask(session_id, text, holder), timeout=DEADLINE_S)
            except (asyncio.TimeoutError, TimeoutError):
                sid = holder.get("session_id")
                if sid is None:
                    sid = self._session(None, text)
                return self._fallback(sid, text, f"对话超过 {DEADLINE_S} 秒")

    async def _ask(self, session_id: int | None, text: str,
                   holder: dict | None = None) -> ChatAnswer:
        import asyncio

        text = (text or "").strip()
        session_id = self._session(session_id, text)
        if holder is not None:
            holder["session_id"] = session_id      # 让超时路径复用这个会话
        history = self._history(session_id)
        snap = context_snapshot(self.conn, self.clock)
        messages = ([{"role": "system", "content": CHAT_SYSTEM_PROMPT},
                     {"role": "system", "content": snap}]
                    + history + [{"role": "user", "content": text}])
        self._write_message(session_id, "user", text)

        # 首轮就查一次：额度不够时不必进循环
        chat_tools = self.tools.schema("chat")
        out_tokens = self._max_out()
        if not self.budget.allow(est_in=_est_in(messages, chat_tools), est_out=out_tokens):
            return self._fallback(session_id, text, "对话额度已用完")

        started = time.monotonic()
        tool_names: list[str] = []
        tools = self.tools.schema("chat")
        pending = None
        returns: dict[str, list[dict]] = {}     # 请求级：本轮的校验依据
        try:
            for _ in range(ROUNDS):
                remaining = DEADLINE_S - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("对话超时")
                # **每一轮都要查**：只在回合开始时查的话，首轮就把额度用光后，
                # 后续工具轮仍然继续发请求（预算形同虚设）
                if not self.budget.allow(est_in=_est_in(messages, self.tools.schema("chat")),
                                 est_out=out_tokens):
                    return self._fallback(session_id, text, "对话额度已用完")
                result = await self.llm.chat(
                    messages, tools, max_tokens=out_tokens,
                    # 配置为 0 表示不限单次调用时长：此时只用本轮的剩余时间，
                    # 不能把 0 传下去（SDK 会当成"立刻超时"）。
                    timeout_s=_call_timeout(remaining, self.cfg.llm.timeouts.chat_s),
                    deduct=_deduct_of(self.budget),
                    est_in=_est_in(messages, chat_tools),
                    on_attempt=_audit_of(self.budget))
                calls = list(getattr(result, "tool_calls", None) or [])
                if not calls:
                    if pending is not None:
                        # 写操作必须变成确认卡：只回一句 needs_confirm 等于什么都没做
                        text = await self._confirm_card(*pending)
                        return await self._answer(session_id, text, tool_names, returns,
                                                  grounded=False)
                    return await self._answer(session_id, (result.content or "").strip(),
                                              tool_names, returns)
                messages.append({"role": "assistant", "content": result.content or "",
                                 "tool_calls": [_as_payload(c) for c in calls]})
                for call in calls:
                    name = _name(call)
                    args = _json_args(call)
                    try:
                        # **必须放线程**：ToolRegistry.invoke 是同步的，westock 子进程
                        # 最长 15 秒；在事件循环里直接调会阻塞整个 api 进程的 HTTP。
                        env = await asyncio.to_thread(self.tools.invoke, name, args, "chat")
                    except Exception as exc:               # noqa: BLE001
                        env = {"ok": False, "reason": f"tool_error:{type(exc).__name__}"}
                    tool_names.append(name)
                    returns.setdefault(name, []).append(env)
                    if name in WRITE_TOOLS and (env or {}).get("reason") == "needs_confirm":
                        pending = (name, args)
                    messages.append({"role": "tool",
                                     "tool_call_id": getattr(call, "id", None) or name,
                                     "content": json.dumps(env, ensure_ascii=False, default=str)})
            if pending is not None:
                card = await self._confirm_card(*pending)
                return await self._answer(session_id, card, tool_names, returns, grounded=False)
            return await self._answer(session_id, "", tool_names, returns, why="对话轮数用尽")
        except (asyncio.CancelledError, GeneratorExit):
            raise                                   # 额度已扣不退，无需再记账
        except _expected_errors() as exc:
            # **只把「外部依赖坏了」降级**：中转站重试耗尽、不可重试、超时、额度用完。
            # 其余异常一律重抛——`NameError`/`TypeError` 被吞成「AI 暂时不可用」时，
            # 用户会一直以为中转站挂了，而真实故障在自己的代码里。
            import logging

            # **这里不再补审计**：`LlmClient` 已经为每个 attempt 回调过
            # `on_attempt`，异常上的 `usage` 是**累计值**，再记一遍会让
            # `token_audit` 多出一行汇总（实测 3 次重试写出 4 行），成本报表失真。
            # 额度在发请求前就扣过了，失败不退款，所以这里无事可做。
            logging.getLogger("chat").warning("对话降级（外部依赖）：%s", exc)
            # **额度用尽要说成额度**：说成"AI 暂时不可用"会让用户以为服务坏了，
            # 而真实的下一步是"明天再来"或"少问几句"。
            from llm.client import BudgetExceeded as _BE

            prefix = "今日对话额度已用完" if isinstance(exc, _BE) else "AI 暂时不可用"
            return self._fallback(session_id, text, prefix)

    # ---- 落库 ------------------------------------------------------------

    def _session(self, session_id: int | None, text: str) -> int:
        if session_id is not None:
            row = self.conn.execute("SELECT 1 FROM chat_session WHERE id = ?",
                                    (session_id,)).fetchone()
            if row is not None:
                return int(session_id)
        cur = self.conn.execute(
            "INSERT INTO chat_session(created_at, title) VALUES (?, ?)",
            (self.clock.now().isoformat(), text[:20]))
        self.conn.commit()
        return int(cur.lastrowid)

    def _write_message(self, session_id: int, role: str, content: str,
                       tool_calls: list | None = None, grounded: bool | None = None) -> None:
        self.conn.execute(
            "INSERT INTO chat_message(session_id, ts, role, content, tool_calls, grounded)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, self.clock.now().isoformat(), role, content,
             json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
             None if grounded is None else (1 if grounded else 0)))
        self.conn.commit()

    def _history(self, session_id: int) -> list[dict]:
        """首轮 + 最近 10 轮（= 最多 22 条消息：11 问 11 答）。"""
        rows = self.conn.execute(
            "SELECT id, role, content FROM chat_message WHERE session_id = ? ORDER BY id",
            (session_id,)).fetchall()
        if not rows:
            return []
        keep = HISTORY_KEEP * 2
        head = list(rows[:2])                       # 首轮问答
        tail = list(rows[-keep:])
        seen, merged = set(), []
        for r in head + tail:
            if r[0] in seen:
                continue
            seen.add(r[0])
            merged.append({"role": r[1], "content": r[2]})
        return merged

    # ---- 出口 ------------------------------------------------------------

    def _as_of(self) -> str | None:
        for scope in ("market", "pool"):
            row = self.conn.execute(
                "SELECT as_of FROM snapshot_cache WHERE scope = ?", (scope,)).fetchone() \
                if _has_snapshot_cache(self.conn) else None
            if row and row[0]:
                return str(row[0])
        return None

    async def _answer(self, session_id: int, text: str, tool_names: list[str],
                      returns: dict[str, list[dict]] | None = None, why: str = "",
                      grounded: bool = True) -> ChatAnswer:
        # **校验必须用本轮工具返回**：拿空 dict 去校验会把每个数字都判成「无出处」，
        # 用户看到的全是「数据校验失败，请查网页」
        returns = dict(returns or {})
        if text and grounded:
            # **必须 await**：`ask` 本身就在事件循环里，`asyncio.run` 会直接抛
            # RuntimeError: asyncio.run() cannot be called from a running event loop
            check = await enforce(text, returns, rewrite=_keep_original)
        else:
            check = GroundedText(text=text, ungrounded=False, failed=False)
        text = _stale_prefix(check.text or text, self._as_of(), self.clock)
        if not text:
            text = f"这一轮没有得出结论（{why or '模型没有给出回答'}）。"
        self._write_message(session_id, "assistant", text, tool_names,
                            grounded=False if why else not check.failed)
        return ChatAnswer(session_id=session_id, text=text, grounded=check,
                          as_of=self._as_of(), market=self._market(), fallback=False)

    def _fallback(self, session_id: int, text: str, why: str) -> ChatAnswer:
        """LLM 不可用 → 结构化快捷指令（§7.4）。**前缀必须出现**：用户要知道这是降级。

        `why` 区分「额度用完」与「服务故障」：两者对用户的含义完全不同。
        """
        body = struct_answer(self.conn, self.clock, text)
        out = f"{why}，以下是直接查询结果\n{body}"
        self._write_message(session_id, "assistant", out, [], grounded=None)
        return ChatAnswer(session_id=session_id, text=out,
                          grounded=GroundedText(text=out, ungrounded=False, failed=False),
                          as_of=self._as_of(), market=self._market(), fallback=True)

    def _market(self) -> str:
        enabled = self.cfg.enabled_markets()
        return enabled[0] if enabled else "cn_main"

    async def _confirm_card(self, tool: str, args: dict) -> str:
        """把 `needs_confirm` 变成一张**真的推得出去**的确认卡。

        没有这一步，4 个写工具在对话里永远只得到一句 `needs_confirm`、确认卡永远发不出去
        ——批1 的 `render_confirm_card` 与 Task 6 的确认端点都白建。

        `turn_id` 用一个新建的 `agent_turn` 行承载：Task 6 的回调按
        `seq_id='confirm-{turn_id}'` 反查，`pending_action` 必须是 `{"tool","args"}` 的
        JSON 串（存描述文本则回调还原不出参数）。
        """
        import os as _os

        from agent.push import action_desc
        from notify.hmac_sign import ensure_secret
        from notify.ntfy import NtfyClient
        from notify.templates import render_confirm_card

        payload = json.dumps({"tool": tool, "args": args}, ensure_ascii=False)
        cur = self.conn.execute(
            "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback,"
            " market, pending_action, pending_status) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.clock.now().isoformat(), "chat", f"chat 写操作 {tool}", 0, "speak", 0,
             self._market(), payload, "pending"))
        self.conn.commit()
        turn_id = int(cur.lastrowid)

        ttl = int(getattr(self.cfg.agent, "pending_action_ttl_h", 24) or 24)
        exp_ts = int(self.clock.now().timestamp()) + ttl * 3600
        msg = render_confirm_card(turn_id, action_desc(payload), self.cfg.notify.public_host,
                                  ensure_secret(self.conn), self.cfg, exp_ts)
        ntfy = self.ntfy or NtfyClient(self.cfg.notify.ntfy_url, self.cfg.notify.topic,
                                       _os.environ.get("NTFY_TOKEN"))
        seq = f"confirm-{turn_id}"
        self.conn.execute(
            "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, event_id,"
            " signal_id, trade_date, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.clock.now().isoformat(), None, "confirm_card", "P2", seq, 1, msg.body,
             None, None, self.clock.today(), "pending"))
        self.conn.commit()
        try:
            # **必须走 to_thread**：`ntfy.publish` 是同步 httpx（timeout=10s），
            # 直接在事件循环里调会把整个 api 卡住——同一个函数里的 `tools.invoke`
            # 专门 to_thread 就是为了这个。
            from notify.drill import drill_skip as _drill

            if _drill("chat_confirm_card", turn_id=turn_id):
                # 演练：不发、不标 accepted、也不留下可被确认的 pending 操作
                self.conn.execute(
                    "UPDATE push_log SET status='suppressed', error='drill' WHERE seq_id=?", (seq,))
                self.conn.execute("UPDATE agent_turn SET pending_status='expired'"
                                  " WHERE id=? AND pending_status='pending'", (turn_id,))
                self.conn.commit()
                return NEEDS_CONFIRM_TEXT
            import asyncio as _aio
            ntfy_id = await _aio.to_thread(ntfy.publish, msg)
            self.conn.execute("UPDATE push_log SET status='accepted', ntfy_id=?"
                              " WHERE seq_id=?", (ntfy_id, seq))
        except Exception as exc:                              # noqa: BLE001
            # **推送失败必须把 pending 关掉**：留着 `pending_status='pending'` 的话，
            # 用户虽然没收到卡，但那个 turn 仍可被确认端点执行——等于执行一个用户
            # 从未见过的写操作。同时回答不能再说「已发出」。
            self.conn.execute("UPDATE push_log SET status='failed', error=? WHERE seq_id=?",
                              (str(exc)[:200], seq))
            self.conn.execute("UPDATE agent_turn SET pending_status='expired'"
                              " WHERE id=? AND pending_status='pending'", (turn_id,))
            self.conn.commit()
            return "确认卡发送失败，请稍后重试"
        self.conn.commit()
        return NEEDS_CONFIRM_TEXT


# ---------------------------------------------------------------- 快捷指令


CODE = __import__("re").compile(pattern=r"(?<!\d)(\d{6})(?!\d)")


def struct_answer(conn, clock, text: str) -> str:
    """§7.4 的三条快捷指令：代码 → 报价 + 形态状态；「观察池」→ Top 10；「信号」→ 最近列表。"""
    match = CODE.search(text or "")
    if match:
        code = match.group(1)
        row = conn.execute(
            "SELECT code, close_raw, box_high_raw, box_low_raw, pos_in_box FROM pool"
            " WHERE code LIKE ? ORDER BY trade_date DESC LIMIT 1", (f"%{code}",)).fetchone()
        if row is None:
            return f"{code} 不在观察池里，无法给出形态状态。"
        return (f"{row[0]} 收盘 {row[1]}，箱体 {row[3]}–{row[2]}，"
                f"位于箱体 {float(row[4] or 0):.2f} 位置。")
    if "观察池" in (text or ""):
        rows = conn.execute(
            "SELECT code, score FROM pool WHERE trade_date = (SELECT max(trade_date) FROM pool)"
            " ORDER BY rank LIMIT 10").fetchall()
        if not rows:
            return "观察池当前为空。"
        return "观察池前 10：" + "、".join(f"{r[0]}({r[1]:.1f})" for r in rows)
    if "信号" in (text or ""):
        rows = conn.execute(
            "SELECT code, kind, level FROM signals ORDER BY ts DESC LIMIT 10").fetchall()
        if not rows:
            return "最近没有信号。"
        return "最近信号：" + "、".join(f"{r[0]} {r[1]} {r[2]}" for r in rows)
    return "可以发股票代码、或问「观察池」「信号」。"


# ---------------------------------------------------------------- 小工具


def _deduct_of(budget):
    """给 `LlmClient` 的**非退款扣减**回调（chat 池）。

    同 `agent.loop._deduct`：`hasattr` 前置探测，**不吞** `deduct` 内部的异常。
    """
    has_deduct = hasattr(budget, "deduct")

    def deduct(est_in, est_out):
        if has_deduct:
            return bool(budget.deduct(est_in, est_out))
        return bool(budget.allow(est_in, est_out))
    return deduct


def _audit_of(budget):
    """给 `LlmClient` 的审计回调。

    **审计失败不影响调用**：`token_audit` 是成本报表用的旁路数据，写不进去不该
    让一次已经扣过额度的对话失败（额度已在 `deduct` 时扣掉，不会因此超支）。
    agent 侧的 `_make_auditor` 是同一约定。
    """
    def on_attempt(seq, usage, unknown, est_in, est_out):
        try:
            budget.audit(usage, unknown=unknown, est_in=est_in, est_out=est_out,
                         attempt_seq=seq)
        except Exception:                              # noqa: BLE001
            import logging

            logging.getLogger("chat").warning("用量审计写入失败（不影响本次对话）",
                                              exc_info=True)
    return on_attempt


def _est_in(messages: list[dict], tools: list[dict] | None = None) -> int:
    """粗估本轮输入 token：**正文 + 工具 schema + 历史 tool_calls 参数**。

    只算正文会低估：schema 一两千 token，历史工具调用的 `arguments` 也是正文级，
    漏掉它们会让临近日预算的请求被放行。
    """
    chars = 0
    for m in messages:
        chars += len(str(m.get("content") or ""))
        for c in m.get("tool_calls") or []:
            chars += len(str(c))
    if tools is not None:
        chars += len(str(tools))
    return int(chars * CHARS_TO_TOKENS)


def _as_payload(call) -> dict:
    return {"id": getattr(call, "id", None) or _name(call), "type": "function",
            "function": {"name": _name(call), "arguments": _raw_args(call)}}


def _name(call) -> str:
    fn = getattr(call, "function", None)
    return str(getattr(fn, "name", "") or "")


def _raw_args(call) -> str:
    fn = getattr(call, "function", None)
    return str(getattr(fn, "arguments", "") or "")


def _json_args(call) -> dict:
    try:
        value = json.loads(_raw_args(call) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _stale_prefix(text: str, as_of: str | None, clock) -> str:
    """数据超过 60 秒且盘中 → 首行标注截止时刻（§7.4）。"""
    if not text or not as_of:
        return text
    try:
        ts = datetime.fromisoformat(str(as_of))
    except (TypeError, ValueError):
        return text
    if ts.tzinfo is None:
        from core.clock import SH

        ts = ts.replace(tzinfo=SH)
    now = clock.now()
    hm = now.strftime("%H:%M")
    in_session = "09:15" <= hm <= "15:05" and not ("11:30" <= hm < "13:00")
    if not in_session or (now - ts).total_seconds() <= STALE_S:
        return text
    return f"数据截至 {ts.strftime('%H:%M:%S')}\n{text}"


def _has_snapshot_cache(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                        " AND name='snapshot_cache'").fetchone() is not None


async def _keep_original(text: str, bad) -> str:
    """对话里不回灌重写：改写要再调一次 LLM，对话预算吃不消。
    校验失败的句子由 `grounding` 自己替换成「数据校验失败，请查网页」。"""
    return text


