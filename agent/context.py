"""预渲染上下文（计划 Task 8，spec §7.5.1）：把「模型该看到什么」算好再喂，不让它自己翻库。

四段顺序固定，裁的时候**从后往前**裁（越靠后越可丢）：

1. 事件本身（不裁）——这是这次唤醒的全部理由。
2. 该票最近 3 根日线 + 箱体上下沿（raw 口径）+ 今日已推送（不裁）。
3. 同板块/同时段其它事件（最多 8 条，按 salience 取最高）+ 该票/该板块的记忆（最多 3 条）。
4. 自己今天说过什么（最近 10 条 `why`）。

仍超预算 → **只留前两段**，`truncated=True`。宁可少说，也不能把第一段裁掉——
模型会拿着半截上下文一本正经地讲另一件事。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent.memory import recall

DEFAULT_MAX_CHARS = 6000
SECTOR_EVENTS = 8
OWN_HISTORY = 10
OWN_HISTORY_TAIL = 5
MEMORY_LIMIT = 3
BOX_BARS = 3


@dataclass
class Rendered:
    text: str
    chars: int
    truncated: bool
    ctx: dict = field(default_factory=dict)


def _budget(cfg) -> int:
    try:
        return int(cfg.agent.context.max_chars)
    except AttributeError:
        return DEFAULT_MAX_CHARS


def _rel(ts: str, now: datetime) -> str:
    try:
        delta = (now - datetime.fromisoformat(ts)).total_seconds()
    except (TypeError, ValueError):
        return "?"
    return f"{int(delta)}秒前" if delta < 60 else f"{int(delta // 60)}分钟前"


def _event_block(group) -> tuple[str, list[dict]]:
    lines = []
    rows = []
    for e in group.events:
        price = e.payload.get("price")
        shown = f"{float(price):.2f}" if isinstance(price, (int, float)) else "-"
        lines.append(f"- {e.target or e.payload.get('code') or '-'} {e.kind} {shown}"
                     f"（{e.ts}）")
        rows.append({"code": e.target or e.payload.get("code"), "kind": e.kind,
                     "price": price, "ts": e.ts})
    return "\n".join(lines), rows


def _bars_block(conn, group, anchor: int) -> tuple[str, list[dict]]:
    code = group.events[0].target or group.events[0].payload.get("code")
    bars = []
    if code:
        bars = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
                for r in conn.execute(
                    "SELECT date, open, high, low, close FROM daily_bars WHERE code = ?"
                    " AND date < ? ORDER BY date DESC LIMIT ?", (code, anchor, BOX_BARS))]
        bars.reverse()
    box = conn.execute(
        "SELECT box_high_raw, box_low_raw, close_raw FROM pool WHERE code = ?"
        " ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
    day = conn.execute("SELECT trade_date FROM pool WHERE market = ?"
                       " ORDER BY trade_date DESC LIMIT 1", (group.market,)).fetchone()
    pushed = conn.execute(
        "SELECT count(*) FROM push_log WHERE trade_date = ? AND status IN ('pending','accepted')",
        (anchor,)).fetchone()[0] if _has_push_log(conn) else 0
    lines = ["【个股】" + (code or "-")]
    for b in bars:
        lines.append(f"- {b['date']} 开{b['open']} 高{b['high']} 低{b['low']} 收{b['close']}")
    if box is not None:
        lines.append(f"- 箱体上沿 {box[0]} 下沿 {box[1]}（{day[0] if day else '-'} 池子）")
    lines.append(f"- 今日已推送 {pushed} 条")
    return "\n".join(lines), {"bars": bars, "box_high": box[0] if box else None,
                              "box_low": box[1] if box else None, "pushed": pushed}


def _has_push_log(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='push_log'"
                        ).fetchone() is not None


def _sector_block(conn, group, anchor: int) -> tuple[str, list[dict]]:
    """同板块/同时段其它事件：按 salience 取最高 8 条（不是按时间）。"""
    industries = {e.industry for e in group.events if getattr(e, "industry", None)}
    seen = {e.id for e in group.events}
    rows = []
    for ind in industries:
        for r in conn.execute(
                "SELECT id, ts, target, kind, payload, salience FROM event_log"
                " WHERE trade_date = ? AND kind = ? ORDER BY salience DESC LIMIT ?",
                (anchor, group.events[0].kind, SECTOR_EVENTS * 2)):
            if r[0] in seen:
                continue
            try:
                payload = json.loads(r[4]) if r[4] else {}
            except (TypeError, ValueError):
                payload = {}
            if payload.get("industry") != ind:
                continue
            rows.append({"id": r[0], "ts": r[1], "code": r[2], "kind": r[3],
                         "industry": ind, "salience": float(r[5])})
    rows.sort(key=lambda d: d["salience"], reverse=True)
    rows = rows[:SECTOR_EVENTS]
    if not rows:
        return "", []
    lines = ["【同板块事件】"]
    for d in rows:
        lines.append(f"- {d['code']} {d['kind']} 显著度{d['salience']:.2f}")
    return "\n".join(lines), rows


def _memory_block(conn, group, now: datetime) -> tuple[str, list[dict]]:
    out = []
    seen = set()
    for target, scope in ((group.events[0].target, "stock"),
                          (next(iter([e.industry for e in group.events
                                      if getattr(e, "industry", None)]), None), "sector")):
        if not target or (scope, target) in seen:
            continue
        seen.add((scope, target))
        for m in recall(conn, target, now, limit=MEMORY_LIMIT, scope=scope):
            out.append({"id": m.id, "scope": m.scope, "target": m.target,
                        "content": m.content, "created_at": m.created_at})
    if not out:
        return "", []
    lines = ["【相关记忆】"]
    for m in out[:MEMORY_LIMIT]:
        lines.append(f"- [{m['created_at']}] {m['content']}")
    return "\n".join(lines), out


def _history_block(conn, now: datetime, limit: int) -> tuple[str, list[dict]]:
    since = (now - timedelta(days=1)).date().isoformat()
    rows = conn.execute(
        "SELECT id, ts, why FROM agent_turn WHERE ts >= ? AND why IS NOT NULL AND why != ''"
        " ORDER BY id DESC LIMIT ?", (since, int(limit))).fetchall()
    if not rows:
        return "", []
    lines = ["【我说过】"]
    for r in reversed(rows):
        lines.append(f"- {_rel(r[1], now)}：{r[2]}")
    return "\n".join(lines), [{"id": r[0], "ts": r[1], "why": r[2]} for r in rows]


def _confirm_block(conn, now: datetime) -> tuple[str, list[dict]]:
    """我提过的写操作里，**已被用户确认执行**的那几条。

    原来确认之后就断了：`api/routes.py::hook_agent_confirm` 把 `pending_status`
    置 `confirmed`、写一条 `confirm_result` 审计行就结束，agent 侧没有任何读取方
    （`agent/loop.py`、`agent/journal.py`、`agent/context.py` 全都不看这个状态），
    也不再产生可唤醒的事件。

    后果是三件事：用户点了「确认执行」手机端只有一个 toast、模型下一轮仍以为
    "还没办"，很可能**再提一次同样的 `pending_action`**（又是一张新卡），
    而且无法在自己的叙述里收尾（"加进自选后我盯着它"）。

    这里补上"状态回流"这一半：把当日已确认的操作渲染进上下文，模型就能
    **不再重复提议、并能接着把话说完**。不新增 LLM 调用、不改回合模型。
    """
    day = now.date().isoformat()
    rows = conn.execute(
        "SELECT id, ts, pending_action FROM agent_turn"
        " WHERE pending_status = 'confirmed' AND ts >= ? ORDER BY id DESC LIMIT 5",
        (day,)).fetchall()
    if not rows:
        return "", []
    lines = ["【已确认执行】（用户已点过确认，不要重复提议）"]
    out = []
    for r in rows:
        tool, args = "?", {}
        try:
            action = json.loads(r[2] or "")
            tool = str(action.get("tool") or "?")
            args = action.get("args") or {}
        except (TypeError, ValueError):
            pass
        code = args.get("code") if isinstance(args, dict) else None
        lines.append(f"- {_rel(r[1], now)}：{tool}" + (f"（{code}）" if code else ""))
        out.append({"id": r[0], "ts": r[1], "tool": tool, "code": code})
    return "\n".join(lines), out


def render_event(group, conn, clock, cfg) -> Rendered:
    now = clock.now()
    anchor = clock.today()
    e_block, e_rows = _event_block(group)
    head = f"【事件】{group.market} {group.kind}（{len(group.events)} 条）"
    bars_block, bars_ctx = _bars_block(conn, group, anchor)

    first_two = head + "\n" + e_block + "\n\n" + bars_block
    sec_block, sec_rows = _sector_block(conn, group, anchor)
    mem_block, mem_rows = _memory_block(conn, group, now)
    hist_block, hist_rows = _history_block(conn, now, OWN_HISTORY)

    conf_block, conf_rows = _confirm_block(conn, now)
    rest = "\n\n".join(b for b in (conf_block, sec_block, mem_block, hist_block) if b)
    text = first_two + ("\n\n" + rest if rest else "")
    truncated = False
    if len(text) > _budget(cfg):
        # 超预算：只留前两段（丢板块事件/记忆/历史），**绝不裁第一段**
        text = first_two
        truncated = True
        sec_rows, mem_rows, hist_rows, conf_rows = [], [], [], []
    ctx = {"event": e_rows, "stock": bars_ctx, "sector_events": sec_rows,
           "memory": mem_rows, "own_history": hist_rows, "confirmed": conf_rows}
    return Rendered(text=text, chars=len(text), truncated=truncated, ctx=ctx)


def render_heartbeat(window_start, conn, clock, cfg) -> Rendered:
    """自上次心跳以来的事件摘要 + 池内贴顶尖的票 + `pending_retry` + 自己说过什么。

    事件摘要与 `pending_retry` **一律加 `trade_date = today`**（D29）：跨日残留不能被
    当成「刚发生」——那正是半夜推送的来源。
    """
    now = clock.now()
    anchor = clock.today()
    since = window_start.isoformat() if hasattr(window_start, "isoformat") else str(window_start)
    rows = [{"id": r[0], "ts": r[1], "code": r[2], "kind": r[3], "salience": float(r[4])}
            for r in conn.execute(
                "SELECT id, ts, target, kind, salience FROM event_log WHERE trade_date = ?"
                " AND ts >= ? ORDER BY id", (anchor, since))]
    retry = [{"id": r[0], "code": r[1], "kind": r[2]}
             for r in conn.execute(
                 "SELECT id, target, kind FROM event_log WHERE trade_date = ?"
                 " AND agent_status = 'pending_retry' ORDER BY id", (anchor,))]
    near = [{"code": r[0], "pos_in_box": r[1]} for r in conn.execute(
        "SELECT code, pos_in_box FROM pool WHERE trade_date = (SELECT max(trade_date) FROM pool)"
        " AND pos_in_box >= 0.99 ORDER BY pos_in_box DESC LIMIT 10")]

    parts = [f"【自上次心跳以来】{len(rows)} 条事件"]
    for r in rows[:20]:
        parts.append(f"- {r['code']} {r['kind']} 显著度{r['salience']:.2f}（{r['ts']}）")
    if near:
        parts.append("【贴顶尖】" + "、".join(f"{n['code']}({n['pos_in_box']:.2f})" for n in near))
    if retry:
        parts.append("【待重试】" + "、".join(f"{r['code']} {r['kind']}" for r in retry))
    conf_block, conf_rows = _confirm_block(conn, now)
    if conf_block:
        parts.append(conf_block)
    hist_block, hist_rows = _history_block(conn, now, OWN_HISTORY)
    if hist_block:
        parts.append(hist_block)

    text = "\n".join(parts)
    truncated = False
    if len(text) > _budget(cfg):
        text = "\n".join(parts[:1])           # 只留摘要行
        truncated = True
        hist_rows, conf_rows = [], []
    ctx = {"events": rows, "near_breakout": near, "pending_retry": retry,
           "own_history": hist_rows, "confirmed": conf_rows}
    return Rendered(text=text, chars=len(text), truncated=truncated, ctx=ctx)
