"""工作日志接口（计划 Task 13，spec §7.6.2）：`GET /api/agent-log`。

**沉默的回合也要在列表里**——这张表的灵魂是「为什么没说」，只列说话的回合等于把
最有信息量的那一半丢掉。
"""
from __future__ import annotations

import json

from fastapi import Query

# **没有模块级 router**：装配走 `register(app, ...)`（`api/routes.create_app` 调用）。
# 留一个空的 `router = APIRouter()` 只会误导后来人用 `include_router` 然后 404。


def _summary(conn, day: int, cfg) -> dict:
    detected = conn.execute("SELECT count(*) FROM event_log WHERE trade_date = ?",
                            (day,)).fetchone()[0]
    escalated = conn.execute(
        "SELECT count(*) FROM event_log WHERE trade_date = ? AND escalated = 1",
        (day,)).fetchone()[0]
    rows = conn.execute(
        "SELECT decision, count(*) FROM agent_turn WHERE substr(ts, 1, 10) = ?"
        " GROUP BY decision", (_iso_day(day),)).fetchall()
    counts = {r[0]: int(r[1]) for r in rows}
    tok = conn.execute(
        "SELECT COALESCE(sum(tokens_in), 0), COALESCE(sum(tokens_out), 0) FROM agent_turn"
        " WHERE substr(ts, 1, 10) = ?", (_iso_day(day),)).fetchone()
    pricing = getattr(cfg.llm, "pricing", None)
    cost = 0.0
    is_estimate = True
    if pricing is not None:
        cost = (int(tok[0]) / 1e6 * float(getattr(pricing, "in_cny_per_mtok", 0.0))
                + int(tok[1]) / 1e6 * float(getattr(pricing, "out_cny_per_mtok", 0.0)))
        is_estimate = bool(getattr(pricing, "is_estimate", True))
    return {"detected": int(detected), "escalated": int(escalated),
            "spoke": counts.get("speak", 0), "silent": counts.get("silent", 0),
            "cost_cny": round(cost, 4), "pricing_is_estimate": is_estimate}


def _iso_day(day: int) -> str:
    text = str(day)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 else text


def turns_for(conn, day: int) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, wake_kind, decision, why, level, tool_calls, rounds, push_seq,"
        " schedule_next, context_digest, context_chars, context_truncated FROM agent_turn"
        " WHERE substr(ts, 1, 10) = ? ORDER BY id", (_iso_day(day),)).fetchall()
    out = []
    for r in rows:
        tools = r[5]
        try:
            tools = json.loads(tools) if tools else []
        except (TypeError, ValueError):
            tools = []
        out.append({"ts": r[0], "wake_kind": r[1], "decision": r[2], "why": r[3],
                    "level": r[4], "tool_calls": tools, "rounds": r[6],
                    "push_seq": r[7], "schedule_next": r[8],
                    # 「看了什么」：日志页要显示这一回合读了多少上下文、有没有被裁
                    "context_digest": r[9], "context_chars": r[10],
                    "context_truncated": bool(r[11])})
    return out


#: 推送历史最多返回多少行（一天正常几十条，给足余量但不至于把页面拖垮）
PUSH_LOG_LIMIT = 500


def push_rows_for(conn, day: int, limit: int = PUSH_LOG_LIMIT) -> list[dict]:
    """当日 `push_log` 明细。

    **这个接口原来不存在**，于是"今天那条为什么没收到"只能 SSH 进库查：
    失败、被抑制、审计行全都不可见，用户在网页上只能看到一枚通道级徽标。
    """
    rows = conn.execute(
        "SELECT ts, code, kind, level, seq_id, segment, status, body, error, ntfy_id"
        " FROM push_log WHERE trade_date = ? ORDER BY id DESC LIMIT ?",
        (day, int(limit))).fetchall()
    return [{"ts": r[0], "code": r[1], "kind": r[2], "level": r[3], "seq_id": r[4],
             "segment": r[5], "status": r[6],
             # **正文要截断**：合并批的 body 可能上千字，整页拉下来没有意义
             "body": (r[7] or "")[:500], "error": r[8], "ntfy_id": r[9]}
            for r in rows]


def push_counts(conn, day: int) -> dict:
    """按状态汇总，页面顶部显示一行。"""
    rows = conn.execute(
        "SELECT status, count(*) FROM push_log WHERE trade_date = ? GROUP BY status",
        (day,)).fetchall()
    return {r[0]: r[1] for r in rows}


def register(app, conn_factory, clock, cfg) -> None:
    @app.get("/api/agent-log")
    def agent_log(date: str | None = Query(None)):
        day = int(date) if date and str(date).isdigit() and len(str(date)) == 8 else clock.today()
        conn = conn_factory()
        return {"summary": _summary(conn, day, cfg), "turns": turns_for(conn, day)}

    @app.get("/api/push-log")
    def push_log(date: str | None = Query(None)):
        """推送历史：**每一条的最终状态与失败原因**。

        查询串里的 `date` 用 `YYYYMMDD`（与 `/api/agent-log` 同口径）。
        """
        day = int(date) if date and str(date).isdigit() and len(str(date)) == 8 else clock.today()
        conn = conn_factory()
        return {"date": day, "counts": push_counts(conn, day),
                "rows": push_rows_for(conn, day)}
