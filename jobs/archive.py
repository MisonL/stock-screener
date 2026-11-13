"""agent_turn 归档（计划 Task 13，spec §7.6.5）：90 天前的回合移到 archive 表，**不删**。

顺序固定、三条都不能省：

1. **`INSERT OR IGNORE` 进归档表**：`agent_turn_archive.id` 沿用原 id 做主键。任务重跑、
   或上一次在「插完、删前」崩溃时，重复插入会 `UNIQUE constraint failed` 让整个归档事务
   失败——此后每天 15:30 都静默失败。
2. **置空 `agent_memory.source_turn_id` 引用**：批1 `connect()` 开了 `foreign_keys=ON`，
   长期记忆（`expires_at NULL`）一旦引用 90 天前的回合，直接 DELETE 会
   `FOREIGN KEY constraint failed`。新库测试没有这类数据，测不出来。
3. 最后删原表。

其余 TTL 由批1 `jobs/cleanup.py` 每日 16:00 统一执行，这里**不重复删**——两个进程对同一
批行反复 DELETE 只会互相拖慢。
"""
from __future__ import annotations

from datetime import timedelta

KEEP_DAYS = 90

_ARCHIVE_COLS = ("id, ts, wake_kind, trigger_id, context_digest, context_chars,"
                 " context_truncated, tool_calls, rounds, decision, why, level, push_seq,"
                 " schedule_next, latency_ms, tokens_in, tokens_out, fallback, market,"
                 " pending_action, pending_status")


def run(conn, clock) -> dict:
    """把 90 天前的 `agent_turn` 移进归档表。

    三个「都会静默丢数据」的坑：

    1. **`INSERT OR IGNORE ... ; DELETE` 一把梭**：`agent_turn.id` 是普通
       `INTEGER PRIMARY KEY`（可重用）。停用一段时间后 id 被新回合重新占用，
       IGNORE 把**新行**丢掉，随后 DELETE 把它删了——日志直接没了。
       → 先只归档「归档表里没有这个 id」的行，再只删真正归档成功的（剩下的
       `leftover` 报告出来，不静默丢）。
    2. **重跑**：同一行若已归档又因崩溃留在原表，重复插入会 `UNIQUE` 冲突让整个
       事务失败，此后每天 15:30 都失败。→ `INSERT OR IGNORE` 在这里是必要的，
       但必须配合上面的「先查」才不会吞新行。
    3. **归档表主键**：`agent_turn_archive.id` 沿用原 id 做 PRIMARY KEY——两个同 id
       不同内容的回合在归档表里**存不下**。真有重用（代码或人工插入了小 id），
       这一行不归档也不删除，由 `leftover` 报告出来，交给人处理；静默丢才是真事故。
    """
    cutoff = (clock.now() - timedelta(days=KEEP_DAYS)).isoformat()
    with conn:
        cur = conn.execute(
            # **`INSERT OR IGNORE` 不可省**：上次在「插完、删前」崩溃时，重跑会撞
            # `agent_turn_archive.id` 的 UNIQUE——整体失败后每天 15:30 都会失败。
            # 但要配合下面的「内容相等才删」：IGNORE 掉的可能是**同 id 的另一行**
            # （主键可重用），那行不能删。
            f"INSERT OR IGNORE INTO agent_turn_archive({_ARCHIVE_COLS})"
            f" SELECT {_ARCHIVE_COLS} FROM agent_turn t WHERE t.ts < ?", (cutoff,))
        archived = max(int(cur.rowcount or 0), 0)
        # **只清空「确实会被删掉」的回合引用**：id 冲突被保留在原表的回合还在，
        # 它的 memory 引用不该被清成 NULL（否则 memory 断了溯源，行却没走）。
        conn.execute(
            f"UPDATE agent_memory SET source_turn_id = NULL WHERE source_turn_id IN"
            f" (SELECT t.id FROM agent_turn t WHERE t.ts < ? AND EXISTS"
            f"  (SELECT 1 FROM agent_turn_archive a WHERE "
            + " AND ".join(f"a.{c} IS t.{c}" for c in _ARCHIVE_COLS.split(","))
            + "))", (cutoff,))
        # 删除用**内容完全相等**做判据：只看 id IN 会把「id 被重用、内容不同的另一行」
        # 也删掉（那是真丢数据）；按 (id, ts, decision) 相等则只删确实搬走的那一行。
        # 判据是**所有归档列逐一相等**：只比 (id, ts, decision, context_digest) 时，
        # 一个 why/tokens 不同、因 OR IGNORE 没真正搬走的行会被误删。
        same = " AND ".join(f"a.{c} IS agent_turn.{c}" for c in _ARCHIVE_COLS.split(","))
        cur = conn.execute(
            f"DELETE FROM agent_turn WHERE ts < ? AND EXISTS (SELECT 1 FROM agent_turn_archive a"
            f" WHERE {same})", (cutoff,))
        deleted = max(int(cur.rowcount or 0), 0)

        leftover = conn.execute("SELECT count(*) FROM agent_turn WHERE ts < ?",
                                (cutoff,)).fetchone()[0]
    return {"archived": archived, "deleted": deleted, "leftover": int(leftover),
            "cutoff": cutoff}
