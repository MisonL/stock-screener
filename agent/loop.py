"""回合循环（计划 Task 10，spec §5.4.4）：一次唤醒 = 一个有硬截止的多轮工具对话。

四个「不写就会出事」的点：

1. **整回合硬截止 25/60 秒**：`asyncio.wait_for` 取消后 `run_turn` 的局部变量全丢，
   所以 `rendered` 必须由外层先算好传入、`state` 必须是外层建的可变容器——否则超时
   回合写不进日志（`agent_turn.context_digest` 是 NOT NULL）。
2. **工具调用放到线程里**：`ToolRegistry.invoke` 是同步的（SQLite、numpy、15 秒的
   westock 子进程），在事件循环里同步调用时 `wait_for` 无法抢占，硬截止形同虚设。
3. **每次 chat 都在 `finally` 里记账**：被取消的在途调用中转站可能已经计费。
4. **沉默回合不跑 grounding**：`body` 为空，`extract` 会抛 `TypeError`，而心跳沉默是
   最常见的回合。
"""
from __future__ import annotations

import asyncio
import dataclasses
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from agent.context import Rendered, render_event, render_heartbeat
from agent.decision import EMIT_DECISION_TOOL, TurnDecision, parse_decision
from llm.grounding import GroundedText, enforce

MAX_TOOL_CALLS_PER_ROUND = 6
MAX_TOKENS = 2000
REWRITE_MAX_TOKENS = 1000


@dataclass
class Trigger:
    """一次唤醒。`group`/`events`/`channel` 必须有默认值：六种 wake_kind 里只有
    `event` 一定有组，其余四种没有事件，没默认值就要各写一套占位实参。"""

    wake_kind: Literal["event", "heartbeat", "alarm", "auction", "open", "close"]
    group: Any | None = None
    events: list = field(default_factory=list)
    channel: Literal["A", "B"] | None = None
    market: str | None = None
    rounds_budget: int = 6
    deadline_s: float = 60
    alarm_id: int | None = None
    # **闹钟的上下文**（`agent/main.py` 的 `_trigger` 一直往实例上挂，但渲染器没读过）。
    # 不声明成正式字段，模型就永远拿不到"要回访哪只票、为什么回访"——
    # 不同闹钟会渲染出完全相同的请求。
    target: str | None = None
    note: str = ""


@dataclass
class TurnResult:
    decision: TurnDecision
    rounds: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    rendered: Rendered | None = None
    usage: Any | None = None
    latency_ms: int = 0
    fallback: int = 0
    why: str | None = None
    push_seq: str | None = None
    level: str | None = None
    grounded: GroundedText | None = None


@dataclass
class Deps:
    conn: Any
    cfg: Any
    clock: Any
    llm: Any
    budget: Any
    tools: Any
    render: Callable[[Trigger], Rendered]
    # 字符串注解 + None 默认：`agent/memory.py` 在 Task 12 组装点之前不应被 import
    pusher: "AgentPusher | None" = None
    memory: "Any | None" = None
    westock: Any = None


def render_trigger(trigger: Trigger, conn, clock, cfg) -> Rendered:
    """默认的分派渲染器：事件回合走 `render_event`，其余走 `render_heartbeat`。

    `window_start` 取「当日 09:25」与「上一条 `agent_turn.ts`」中较晚者——比它更早的
    事件在上一次回合里已经看过了。
    """
    if trigger.wake_kind == "event" and trigger.group is not None:
        return render_event(trigger.group, conn, clock, cfg)
    now = clock.now()
    start = now.replace(hour=9, minute=25, second=0, microsecond=0)
    row = conn.execute("SELECT max(ts) FROM agent_turn").fetchone()
    if row and row[0]:
        from datetime import datetime

        try:
            last = datetime.fromisoformat(row[0])
        except (TypeError, ValueError):
            last = None
        if last is not None and last > start:
            start = last
    return render_heartbeat(start, conn, clock, cfg)


def _messages(trigger: Trigger, rendered: Rendered) -> list[dict]:
    from llm.prompts import SYSTEM_PROMPT

    what = f"唤醒类型：{trigger.wake_kind}"
    if trigger.wake_kind == "event":
        what += f"（{len(trigger.events or [])} 条事件，通道 {trigger.channel}）"
    # **闹钟的标的与备注必须进请求**：它们是这次唤醒的唯一理由，
    # 只挂在 Trigger 上等于没说——模型会按普通心跳处理，不知道要回访谁。
    if trigger.wake_kind == "alarm":
        if trigger.target:
            what += f"\n回访标的：{trigger.target}"
        if trigger.note:
            what += f"\n回访原因：{trigger.note}"
        if trigger.alarm_id is not None:
            what += f"\n闹钟记录：#{trigger.alarm_id}"
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": rendered.text + "\n\n" + what}]


def make_rewriter(deps: Deps, state: dict) -> Callable[[str, list], Awaitable[str]]:
    """grounding 的「回灌重写」：把标错的句子改写一遍。

    **重写单独记在 `state["rewrites"]`，不占 `state["rounds"]`**：
    F-34 的「一轮收场」量的是「模型是否一次看完就下结论」，重写只是把数字改对，
    算进轮数会把合格回合误判成多轮（2 轮）。用量照样记账、预算照样查。
    """
    async def rewrite(text: str, bad: list) -> str:
        wrong = "\n".join(f"- {m.claim.value}（{m.claim.tool}.{m.claim.path or m.claim.expr}）"
                          f" 实际 {m.actual}" for m in bad)
        msgs = [{"role": "system", "content": "只输出改写后的正文，不要解释。"},
                {"role": "user", "content": f"{text}\n\n以下数字与工具返回不一致：\n{wrong}\n"
                                            "请改正这些数字后只输出正文。"}]
        # 重写也是一次真实调用：额度不够就不发（否则「预算已耗尽仍能靠重写超支」）
        if not deps.budget.allow(est_in=_est_in(msgs), est_out=REWRITE_MAX_TOKENS):
            return text
        try:
            result = await deps.llm.chat(msgs, max_tokens=REWRITE_MAX_TOKENS, timeout_s=10,
                                         deduct=_deduct(deps, state), est_in=_est_in(msgs),
                                         on_attempt=_make_auditor(deps, state))
        except (asyncio.CancelledError, GeneratorExit):
            raise                                  # 额度已扣不退，无需再记账
        except Exception:                           # noqa: BLE001 —— 重写失败就保留原文
            return text
        _bump_rounds(state)
        return (result.content or "").strip() or text

    return rewrite


def _bump_rounds(state: dict) -> None:
    """重写单独计数（不占 `rounds`）。"""
    state["rewrites"] = int(state.get("rewrites") or 0) + 1


#: 预算耗尽的统一措辞。`agent/main.py` 用它判断"今天不可能再跑了"，
#: 从而跳过闹钟——避免"每 2 秒重跑一次必败回合"的死循环（生产实测一天 17885 行）。
BUDGET_EXHAUSTED_WHY = "agent 日预算已用完"

#: 兜底硬上限（秒）。只在"上游彻底不回包"时才会撞上，不是正常的时长约束。
#: 取 10 分钟，**刻意小于 `api/watchdog.py` 的 `AGENT_TURN_STALE_S`（15 分钟）**：
#: 让回合自己收尾并记一行 `timeout`，比"被外部判定为卡死"留下更好的证据。
#: 正常回合几十秒到几分钟，撞不到这里。
SAFETY_DEADLINE_S = 600.0


def _call_timeout(cap: float, deadline_s: float | None, started: float) -> float | None:
    """本轮单次调用的超时。**返回 `None` 表示不设上限**。

    三层约束，取最小的那层：
      · `cap` —— 配置的单次调用上限（`<= 0` 表示不限，这是默认值）
      · `deadline_s` —— 回合的总时长上限（`<= 0` 表示不限，默认）
    都放开时返回 `None`，让 SDK 一直等到上游返回。

    **绝不能把 0 或负数传下去**：那会被 SDK 当成"立刻超时"，
    于是每个 attempt 都瞬间失败——正是这个坑导致过整个交易日报废。
    """
    cap = float(cap or 0)
    deadline_s = float(deadline_s or 0)
    limits: list[float] = []
    if cap > 0:
        limits.append(cap)
    if deadline_s > 0:
        # 不能是负数或 0：到点了但还没发出的调用至少要有 1 秒
        limits.append(max(deadline_s - (time.monotonic() - started), 1.0))
    if not limits:
        # **绝不返回 None**：`None` 会一路传到 SDK 变成"永远等"。
        # 上游不回包时那就是永久卡死。兜底给一个足够大的值。
        return SAFETY_DEADLINE_S
    return min(limits)


def _est_in(msgs: list[dict], tools: list[dict] | None = None) -> int:
    """输入 token 粗估：**消息正文 + 工具 schema + 历史 tool_calls 参数**。

    只算 `content` 会低估一大截：工具 schema 有一两千 token，历史工具调用的
    `arguments` 也是正文级别的量，漏掉它们会让临近日预算的请求被放行。
    中文按 1 字 ≈ 1 token，再乘 1.2 留余量。
    """
    chars = 0
    for m in msgs:
        chars += len(str(m.get("content") or ""))
        for c in m.get("tool_calls") or []:
            chars += len(str(c))
    if tools is not None:
        chars += len(str(tools))
    return int(chars * 1.2)


def _now_iso(deps: Deps) -> str:
    try:
        return deps.clock.now().isoformat()
    except Exception:                                      # noqa: BLE001
        from datetime import datetime, timezone, timedelta

        return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _deduct(deps, state=None):
    """给 `LlmClient` 的**非退款扣减**回调：每次网络请求发出前原子扣掉保守额度。

    `state` 用来打「请求真的开始发了」的标记：扣减通过 = `LlmClient` 紧接着就会发。
    轮数统计需要它——一个请求已发出但随后被 `BudgetExceeded` 拒绝（翻倍重试）
    或被硬截止取消的回合，**应该算一轮**，而不是 0 轮。

    **兼容只判老接口，不吞异常**：`hasattr` 在调用**前**探测，`deduct` 内部抛的
    `AttributeError` 是真实编程错误，必须上抛——旧写法 `except AttributeError`
    包住整个调用，会让实现里的 bug 变成「放行」，硬上限直接失效。
    """
    budget = deps.budget
    has_deduct = hasattr(budget, "deduct")

    def deduct(est_in, est_out):
        if has_deduct:
            ok = bool(budget.deduct(est_in, est_out))
        else:
            ok = bool(budget.allow(est_in, est_out))
        if ok and state is not None:
            state["sent"] = True
        return ok
    return deduct


def _make_auditor(deps, state):
    """给 `LlmClient` 的审计回调：每次 attempt 结束后写 token_audit 并累计到 state。"""
    def on_attempt(seq, usage, unknown, est_in, est_out):
        try:
            deps.budget.audit(usage, unknown=unknown, est_in=est_in, est_out=est_out,
                              attempt_seq=seq)
        except Exception:                                  # noqa: BLE001
            pass
        if usage is not None:
            _accumulate(state, usage)
    return on_attempt


def _accumulate(state: dict, usage) -> None:
    """把一次 attempt 的真实用量累加进 `state["usage"]`（日志/展示用，不参与预算）。"""
    prev = state.get("usage")
    if prev is None:
        state["usage"] = usage
        return
    try:
        prev.prompt_tokens = int(getattr(prev, "prompt_tokens", 0)) + int(
            getattr(usage, "prompt_tokens", 0))
        prev.completion_tokens = int(getattr(prev, "completion_tokens", 0)) + int(
            getattr(usage, "completion_tokens", 0))
        prev.total_tokens = prev.prompt_tokens + prev.completion_tokens
    except Exception:                                      # noqa: BLE001
        state["usage"] = usage


def _budget_write_failed(conn) -> bool:
    """本进程的 agent 池记账故障标志（按池隔离，跨进程可见）。"""
    try:
        row = conn.execute(
            "SELECT value FROM job_state WHERE name = 'budget.write_failed:agent'").fetchone()
    except Exception:                                      # noqa: BLE001
        return True                                        # 读不到就 fail-closed
    return bool(row and row[0])


async def run_turn(trigger: Trigger, deps: Deps, rendered: Rendered,
                   state: dict | None = None) -> TurnResult:
    state = state if state is not None else {"rounds": 0, "usage": None}
    started = time.monotonic()
    msgs = _messages(trigger, rendered)
    tools = list(deps.tools.schema(trigger.wake_kind)) + [EMIT_DECISION_TOOL]
    tool_calls: list[dict] = []
    # **回合级**工具返回：`ToolRegistry._returns` 是跨回合累计的，上一回合查过报价、
    # 本回合没查却引用同一个旧数字时，用累计值校验会「恰好通过」——出处校验要防的
    # 正是这件事。
    tool_returns: dict[str, list[dict]] = {}
    rounds = 0
    usage = None

    while rounds < trigger.rounds_budget:
        timeout_s = _call_timeout(deps.cfg.llm.timeouts.agent_call_s,
                                  trigger.deadline_s, started)
        # **每一轮都查**：只在回合开头查的话，首轮把额度用光后，后续工具轮与
        # grounding 重写仍然继续调用中转站——日预算是硬上限，不是软提示
        if not deps.budget.allow(est_in=_est_in(msgs, tools), est_out=MAX_TOKENS):
            # **这里不加轮数**：一次 LLM 都没发出去，记成 1 轮会把 F-34 的
            # 「一轮收场」占比虚高到 1.0。
            # **必须记 `error` 而不是 `timeout`**：预算耗尽不是"慢"，
            # 它是配置/额度问题。记成 timeout 会让「回合超时率」这个告警口径
            # 失真——实测某天上午 11 条 timeout 里有 10 条其实是预算耗尽
            # （耗时 0–5 秒，根本没等），真实超时率被虚报成 51%（实际约 5%）。
            # 入口层（`run_with_deadline` 前面的预检）一直记的是 `error`，
            # 这里跟它统一。
            return _result(TurnDecision.error(BUDGET_EXHAUSTED_WHY), rounds, tool_calls,
                           rendered, state, started, deps)
        # **`rounds` 在 `chat` 真正发出后才加**（见下）：`deduct` 扣不动时
        # `LlmClient` 会在发请求前抛 `BudgetExceeded`，那次不算一轮。
        # **三个出口各记一次账，且都记在构造结果之前**。
        # 为什么不用 `finally`：except 分支里先 `_result(...)` 再靠 finally 补记，
        # 构造结果时 `state["usage"]` 还是旧值 → `agent_turn.tokens_*` 恒为 NULL，
        # 预算有数、日志空白（第五轮实测）。
        # 为什么取消要单独接：`CancelledError` 是 BaseException，`except Exception`
        # 接不住；而被 `wait_for` 取消时之前几次尝试已经计费，`LlmClient` 会把累计
        # usage 挂在异常上带出来。
        result = None
        try:
            result = await deps.llm.chat(msgs, tools, max_tokens=MAX_TOKENS,
                                         timeout_s=timeout_s, deduct=_deduct(deps, state),
                                         est_in=_est_in(msgs, tools),
                                         on_attempt=_make_auditor(deps, state))
        except (asyncio.CancelledError, GeneratorExit):
            # 有请求在途时被硬截止取消：那一轮**已经开始**，`run_with_deadline`
            # 的超时分支要读到它（否则 rounds=0，F-34 漏样本）
            if state.get("sent") and not state.get("rounds"):
                state["rounds"] = max(int(state.get("rounds") or 0), 1)
            raise                                       # 额度已扣不退，无需再记账
        except Exception as exc:                        # noqa: BLE001 —— 分类见下
            # 延迟导入：`llm.client` 会拉起 openai 栈，而 `agent.main` 的模块级
            # import 不该付这个代价（`test_agent_main_does_not_pull_openai`）。
            from llm.client import BudgetExceeded as _BudgetExceeded

            if not isinstance(exc, _BudgetExceeded):
                # 请求真的发出去了（且额度已扣），这才算一轮
                rounds += 1
                state["rounds"] = rounds
            elif state.get("sent") and rounds == 0:
                # 前一个请求已经发出、被翻倍重试的扣减拒绝：这是一个**开始过的
                # 逻辑轮**，不能记 0（F-34 的「一轮收场」占比会因此漏样本）
                rounds = 1
                state["rounds"] = rounds
            decision = _map_llm_error(exc)
            return _result(decision, rounds, tool_calls, rendered, state, started, deps)
        rounds += 1
        state["rounds"] = rounds
        usage = state.get("usage") or usage

        calls = list(getattr(result, "tool_calls", None) or [])
        if not calls:
            return _result(TurnDecision.invalid("本轮没有调用任何工具（应收场于 emit_decision）"),
                           rounds, tool_calls, rendered, state, started, deps)

        emit = next((c for c in calls if _name(c) == "emit_decision"), None)
        if emit is not None:
            decision = parse_decision(_args(call=emit), channel=trigger.channel or "B",
                                      event_kind=_event_kind(trigger))
            return await _finalize(decision, rounds, tool_calls, rendered, state, started, deps,
                                   trigger, tool_returns)

        # 一轮里最多执行 6 个：超出**不执行**，只记 "capped"（防模型一轮发起 20 个打满预算）
        for c in calls[:MAX_TOOL_CALLS_PER_ROUND]:
            name = _name(c)
            args = _json_args(c)
            try:
                env = await asyncio.to_thread(deps.tools.invoke, name, args, trigger.wake_kind)
            except Exception as exc:                        # noqa: BLE001
                env = {"ok": False, "reason": f"tool_error:{type(exc).__name__}"}
            tool_calls.append({"name": name, "args": args})
            call_id = _call_id(c) or name
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": call_id, "type": "function",
                                         "function": {"name": name,
                                                      "arguments": _raw_args(c)}}]})
            tool_returns.setdefault(name, []).append(env)
            msgs.append({"role": "tool", "tool_call_id": call_id, "content": _dump(env)})
        for _ in calls[MAX_TOOL_CALLS_PER_ROUND:]:
            tool_calls.append({"name": _name(_), "args": _json_args(_), "capped": True})

    return _result(TurnDecision.timeout("轮数用尽"), rounds, tool_calls, rendered, state,
                   started, deps)


async def run_with_deadline(trigger: Trigger, deps: Deps) -> TurnResult:
    """整回合硬截止。`rendered` 与 `state` 必须在这里建并传进去（见模块文档）。"""
    rendered = deps.render(trigger)
    allowed = deps.budget.allow(est_in=math.ceil(rendered.chars / 2) + 800,
                                est_out=MAX_TOKENS)
    if not allowed:
        return TurnResult(decision=TurnDecision(kind="error", speak=False,
                                                why=BUDGET_EXHAUSTED_WHY,
                                                why_silent=BUDGET_EXHAUSTED_WHY),
                          rounds=0, rendered=rendered, why=BUDGET_EXHAUSTED_WHY,
                          grounded=GroundedText(text="", ungrounded=False, failed=False))
    state: dict = {"rounds": 0, "usage": None}
    started = time.monotonic()
    # **`deadline_s <= 0` 表示不设"正常"时长上限**，但仍然套一层**兜底硬上限**
    # `SAFETY_DEADLINE_S`。
    #
    # 为什么兜底不能去掉：轮数上限只保证"跑够轮数就退出"，管不到**卡在某一次
    # 调用里**——上游如果不回包（不是慢，是真挂），`await` 永不返回，回合永远
    # 结束不了，心跳也跟着停摆。`api/watchdog.py` 的 `agent_stalled` 会告警，
    # 但它只报警、不杀回合。所以这里留一个"只有真挂了才会撞上"的值：
    # 正常回合最多几十秒到几分钟，30 分钟是个纯安全网，不会误杀正常回合。
    limit = float(trigger.deadline_s or 0)
    if limit <= 0:
        limit = SAFETY_DEADLINE_S
    try:
        return await asyncio.wait_for(run_turn(trigger, deps, rendered, state), limit)
    except asyncio.TimeoutError:
        return TurnResult(decision=TurnDecision.timeout(), rounds=int(state.get("rounds") or 0),
                          rendered=rendered, usage=state.get("usage"),
                          latency_ms=int((time.monotonic() - started) * 1000),
                          why="回合超时",
                          grounded=GroundedText(text="", ungrounded=False, failed=False))


# ---------------------------------------------------------------- 内部


def _map_llm_error(exc: Exception) -> TurnDecision:
    """`RetriesExhausted` → timeout（502 是外部故障，不是程序错误）；`NonRetryable` → error。"""
    name = type(exc).__name__
    if name == "RetriesExhausted":
        return TurnDecision.timeout(f"LLM 重试耗尽：{exc}")
    if name == "NonRetryable":
        return TurnDecision.error(f"LLM 状态码 {getattr(exc, 'status', '?')}")
    return TurnDecision.error(f"LLM 调用异常：{name}")


async def _finalize(decision: TurnDecision, rounds: int, tool_calls: list[dict],
                    rendered: Rendered, state: dict, started: float, deps: Deps,
                    trigger: Trigger, tool_returns: dict | None = None) -> TurnResult:
    base = _result(decision, rounds, tool_calls, rendered, state, started, deps)
    if deps.pusher is None:
        return base

    # grounding 只在真的说话时跑：沉默回合 body 为空，extract(None) 会抛 TypeError
    if decision.kind == "speak" and decision.body:
        returns = {**tool_returns, "ctx": [rendered.ctx]}
        base.grounded = await enforce(decision.body, returns, rewrite=make_rewriter(deps, state))
        # 重写可能带来**主调用没给的 usage**：结果里的 usage 以 state 为准，
        # 否则预算记了 500/20、`agent_turn.tokens_*` 还是 NULL
        base.usage = state.get("usage") or base.usage
        # **重写不计入 `rounds`**：F-34 的「一轮收场」量的是「模型是否一次看完就下结论」，
        # 重写只是把数字改对，算进去会把合格回合误判成多轮。重写次数单独记在
        # `state["rewrites"]`，需要时由日志读。
    else:
        base.grounded = GroundedText(text="", ungrounded=False, failed=False)

    # **预算写库失败就不推送**：额度已扣不可退，但推出去的数据可能漏了账。
    # 注意：保守扣减模型已经没有 fail-closed 标志了，这里按已有 job_state 检查。
    if _budget_write_failed(deps.conn):
        return base

    if decision.kind == "speak":
        # **按通道分流，不能只看 wake_kind**：通道 B 的事件（index_move / sector_move /
        # 半路提醒…）由 jobs 的模板通道负责第一段，agent 这边**没有第一段可追加**——
        # 一律走 second_segment 的话，B 事件的开口决定会被静默丢弃。
        ev = trigger.events[0] if trigger.events else None
        code = ev.target if ev is not None and getattr(ev, "scope", None) == "stock" else None
        target = getattr(ev, "target", None) if ev is not None else None
        push = deps.pusher
        if trigger.wake_kind == "event" and trigger.channel == "A":
            base.push_seq = await asyncio.to_thread(
                push.second_segment, trigger.group.events[0].id, decision, base.grounded)
        elif trigger.wake_kind == "event":
            base.push_seq = await asyncio.to_thread(
                push.agent_note, decision, base.grounded, code, target)
        elif trigger.wake_kind == "open":
            base.push_seq = await asyncio.to_thread(
                push.open_second_segment, deps.clock.today(), decision, base.grounded)
        elif trigger.wake_kind in ("heartbeat", "alarm"):
            base.push_seq = await asyncio.to_thread(
                push.agent_note, decision, base.grounded, code, target)
    base.level = decision.level
    return base


def _result(decision: TurnDecision, rounds: int, tool_calls: list[dict], rendered: Rendered,
            state: dict, started: float, deps: Deps) -> TurnResult:
    return TurnResult(decision=decision, rounds=rounds, tool_calls=tool_calls, rendered=rendered,
                      usage=state.get("usage"),
                      latency_ms=int((time.monotonic() - started) * 1000),
                      why=decision.why, level=decision.level,
                      grounded=GroundedText(text="", ungrounded=False, failed=False))


def _call_id(call) -> str | None:
    """工具调用 id。**SDK 对象没有 `.get()`**：真实返回是 pydantic 模型，
    写 `c.get("id")` 会在生产路径直接 AttributeError（单测的假 dict 永远暴露不了）。"""
    if isinstance(call, dict):
        return call.get("id")
    return getattr(call, "id", None)


def _name(call) -> str:
    if isinstance(call, dict):
        return str(call.get("name") or (call.get("function") or {}).get("name") or "")
    return str(getattr(getattr(call, "function", None), "name", "") or "")


def _raw_args(call) -> str:
    if isinstance(call, dict):
        fn = call.get("function") or {}
        return str(fn.get("arguments") or call.get("arguments") or "")
    return str(getattr(getattr(call, "function", None), "arguments", "") or "")


def _json_args(call) -> dict:
    import json

    raw = _raw_args(call)
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _args(call) -> str:
    return _raw_args(call)


def _dump(env: Any) -> str:
    import json

    try:
        return json.dumps(env, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


def _event_kind(trigger: Trigger) -> str | None:
    if trigger.group is not None and trigger.group.events:
        return trigger.group.events[0].kind
    return trigger.events[0].kind if trigger.events else None
