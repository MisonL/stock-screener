"""工作日志：一个事务里写 `agent_turn` 并更新 `event_log`。

三件事同一事务，否则日志写了但事件状态没更新 → 同一个 `new` 事件在下次 poll 时又出现。
"""
from __future__ import annotations

import json
from datetime import datetime

from agent.loop import TurnResult, Trigger


def record(conn, trigger: Trigger, result: TurnResult, clock) -> int:
    """写一条 `agent_turn` 并更新本次事件的状态。

    返回 turn_id。三件事同一 `conn` 事务，不同步则日志有行但事件回滚，下次 poll 又来。
    """
    now = clock.now().isoformat()
    decision = result.decision
    why = result.why or decision.why_silent or decision.title or ""
    pending_action = decision.pending_action
    pending_status = "pending" if pending_action else None
    wake_kind = str(trigger.wake_kind)

    trigger_ids = [str(e.id) for e in (trigger.events or [])]
    if not trigger_ids and trigger.group is not None and trigger.group.events:
        trigger_ids = [str(e.id) for e in trigger.group.events]
    trigger_id = int(trigger_ids[0]) if trigger_ids else None

    rendered = result.rendered
    group_events = trigger.group.events if trigger.group is not None else []
    n_events = len(trigger.events or group_events)
    digest = (f"唤醒={wake_kind} 事件={n_events} "
              f"上下文={(rendered.chars if rendered else 0)}字符")

    is_stale = decision.kind in ("shadow", "deduped", "gave_up")

    with conn:
        cur = conn.execute(
            "INSERT INTO agent_turn(ts, wake_kind, trigger_id, context_digest, context_chars,"
            " context_truncated, tool_calls, rounds, decision, why, level, push_seq, latency_ms,"
            " schedule_next, tokens_in, tokens_out, fallback, market, pending_action,"
            " pending_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, wake_kind, trigger_id, digest,
             rendered.chars if rendered else None,
             1 if rendered and rendered.truncated else 0,
             json.dumps(result.tool_calls, ensure_ascii=False) if result.tool_calls else None,
             result.rounds, decision.kind, why, result.level, result.push_seq,
             result.latency_ms,
             # **`schedule_next` 必须落库**：不写的话日志页永远显示「排了闹钟：无」，
             # 而实际闹钟已经排上了——排查时两边对不上
             getattr(decision, "schedule_next", None),
             getattr(result.usage, "prompt_tokens", None),
             getattr(result.usage, "completion_tokens", None),
             result.fallback, str(trigger.market or "cn_main"),
             json.dumps(pending_action, ensure_ascii=False) if pending_action else None,
             pending_status))
        turn_id = cur.lastrowid

        # 更新事件状态
        if not is_stale:
            for e in (trigger.events or []):
                if decision.kind in ("timeout", "error", "invalid"):
                    # **第一次失败 → pending_retry；已经在 pending_retry 里再失败 → gave_up**。
                    # 不设上限的话，同一条事件会被心跳整天反复重试，直到收盘清扫
                    # 才放弃——白烧额度。`agent_status` 未升级过的事件本来就不会进
                    # 这个分支（`agent_status='new'` 时上面的 UPDATE 直接写 pending_retry）。
                    already = getattr(e, "agent_status", "escalated") == "pending_retry"
                    conn.execute("UPDATE event_log SET agent_status = ? WHERE id = ?",
                                 ("gave_up" if already else "pending_retry", e.id))
                else:
                    conn.execute("UPDATE event_log SET agent_status = 'done', turn_id = ?"
                                 " WHERE id = ?", (turn_id, e.id))
            if trigger.group is not None:
                for e in trigger.group.events:
                    if e.agent_status != "escalated":
                        continue                        # 没升级过的不动
                    if decision.kind in ("timeout", "error", "invalid"):
                        conn.execute("UPDATE event_log SET agent_status = 'pending_retry'"
                                     " WHERE id = ? AND agent_status = 'escalated'", (e.id,))
                    else:
                        conn.execute("UPDATE event_log SET agent_status = 'done', turn_id = ?"
                                     " WHERE id = ?", (turn_id, e.id))

    return turn_id
