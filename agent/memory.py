"""记忆检索与写入（计划 Task 8，spec §5.3 `agent_memory`）。

**只增不删**：被推翻的记忆靠 `superseded_by` 标失效，`recall` 过滤掉它——agent 之后
要能回答「我为什么改主意」，删掉旧判断就永远说不清。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MAX_RECALL = 3


@dataclass
class MemoryRow:
    id: int
    created_at: str
    expires_at: str | None
    scope: str
    target: str | None
    kind: str
    content: str
    confidence: float | None
    superseded_by: int | None
    source_turn_id: int | None


def _row(r) -> MemoryRow:
    return MemoryRow(id=r[0], created_at=r[1], expires_at=r[2], scope=r[3], target=r[4],
                     kind=r[5], content=r[6], confidence=r[7], superseded_by=r[8],
                     source_turn_id=r[9])


_COLS = ("id, created_at, expires_at, scope, target, kind, content, confidence,"
         " superseded_by, source_turn_id")


def recall(conn, target: str | None, now: datetime, *, limit: int = MAX_RECALL,
           scope: str | None = None) -> list[MemoryRow]:
    """未过期、未被推翻的记忆，`created_at` 降序，最多 3 条。

    `target` 为空时只按 scope 取（例如「大盘」这类没有具体标的的记忆）。
    """
    if not target and scope is None:
        return []
    iso = now.isoformat()
    sql = (f"SELECT {_COLS} FROM agent_memory WHERE superseded_by IS NULL"
           " AND (expires_at IS NULL OR expires_at > ?)")
    params: list = [iso]
    if target:
        sql += " AND target = ?"
        params.append(target)
    if scope is not None:
        sql += " AND scope = ?"
        params.append(scope)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return [_row(r) for r in conn.execute(sql, params).fetchall()]


def write(conn, kind: str, scope: str, target: str | None, content: str, turn_id: int | None,
          expires_at: str | None = None, *, created_at: str, confidence: float | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO agent_memory(created_at, expires_at, scope, target, kind, content,"
        " confidence, source_turn_id) VALUES (?,?,?,?,?,?,?,?)",
        (created_at, expires_at, scope, target, kind, content, confidence, turn_id))
    conn.commit()
    return int(cur.lastrowid)


def supersede(conn, old_id: int, new_id: int) -> None:
    """把旧记忆标成「被 new_id 推翻」。**不删**。"""
    conn.execute("UPDATE agent_memory SET superseded_by = ? WHERE id = ?", (new_id, old_id))
    conn.commit()
