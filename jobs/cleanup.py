"""每日 16:00 的清理任务（D19）。

三条容易踩的坑：

1. **`chat_session` 没有 `ts` 列**，按 `ts` 删会 `no such column: ts`，整条清理链第一步就炸。
2. **批2 才建的表（`agent_schedule` / `chat_session` / `chat_message`）在批1 单跑时不存在**，
   每条 DELETE 前先查 `sqlite_master`，缺表跳过并记日志——不能因为「以后才有的表」让
   磁盘水位检查也一起跳过。
3. **`status='pending'` 且过期的 agent 计划要改 `cancelled`**：agent 停摆几天后一次性取出
   一串「回访 3 天前」的回合，烧预算还推无意义消息。
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import timedelta

log = logging.getLogger(__name__)

# (表名, 时间列, 保留天数, 额外条件)；**顺序即删除顺序**
PRUNE_RULES = (
    ("push_log", "ts", 30, None),
    ("event_log", "ts", 30, None),
    ("pool", "trade_date", 90, None),
    # chat_message 必须排在 chat_session 前面：它有 `REFERENCES chat_session(id)`，
    # 先删父表会让整条清理链以 FOREIGN KEY constraint failed 中断
    ("chat_message", "ts", 90, None),
    ("chat_session", "created_at", 90, None),          # 不是 ts！
)


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def cleanup(conn, clock, dispatcher=None, data_dir: str = "/app/data") -> dict[str, int]:
    """跑一轮清理，返回 `{表/指标: 处理行数}`。"""
    now = clock.now()
    out: dict[str, int] = {}

    with conn:
        for table, column, days, extra in PRUNE_RULES:
            cutoff = now - timedelta(days=days)
            value = cutoff.strftime("%Y%m%d") if column == "trade_date" else cutoff.isoformat()
            if not _has_table(conn, table):
                out[table] = -1                     # -1 = 跳过（表不存在）
                continue
            if table == "chat_session" and _has_table(conn, "chat_message"):
                # 会话过期时，它名下的消息也要一起删：只删父表会撞 FOREIGN KEY
                # （90 天前的会话仍可能挂着新消息——用户翻旧会话继续聊就是这种情况）。
                # **必须再查一次 chat_message**：批1 单跑时它不存在（002 才建），
                # 少了这个判断整条清理链会以 no such table 中断。
                conn.execute(
                    "DELETE FROM chat_message WHERE session_id IN"
                    " (SELECT id FROM chat_session WHERE created_at < ?)", (value,))
            sql = f"DELETE FROM {table} WHERE {column} < ?"
            if extra:
                sql += f" AND {extra}"
            out[table] = conn.execute(sql, (value,)).rowcount

        # **僵尸 pending 回收到 failed**：推送认领后、状态回写前进程被杀，那一行会永远
        # 停在 pending——它既占着当日熔断额度（`_capped` 数 pending/accepted），
        # 又让同 seq_id 再也认领不了。超过 10 分钟仍在 pending 的必然是僵尸。
        if _has_table(conn, "push_log"):
            stale = (now - timedelta(minutes=10)).isoformat()
            out["push_log.zombie"] = conn.execute(
                "UPDATE push_log SET status='failed', error=COALESCE(error,'zombie pending')"
                " WHERE status='pending' AND ts < ?", (stale,)).rowcount

        # 过期的 pending 计划改 cancelled（不是删除：留痕才能回答「为什么没跑」）
        if _has_table(conn, "agent_schedule"):
            stale = (now - timedelta(days=3)).isoformat()
            out["agent_schedule.cancelled"] = conn.execute(
                "UPDATE agent_schedule SET status='cancelled' WHERE status='pending' AND due_at < ?",
                (stale,)).rowcount
            out["agent_schedule"] = conn.execute(
                "DELETE FROM agent_schedule WHERE status IN ('fired','cancelled') AND due_at < ?",
                (stale,)).rowcount
        else:
            out["agent_schedule.cancelled"] = -1
            out["agent_schedule"] = -1

    # upstream_calls 只留近 2 小时：写入侧用的是 clock.now().isoformat()，
    # 这里也必须用同一格式与同一时钟（datetime('now','-2 hours') 是 UTC 且格式不同，字符串比较会错）
    if _has_table(conn, "upstream_calls"):
        cutoff = (now - timedelta(hours=2)).isoformat()
        with conn:
            out["upstream_calls"] = conn.execute(
                "DELETE FROM upstream_calls WHERE ts < ?", (cutoff,)).rowcount
    else:
        out["upstream_calls"] = -1

    # **刷新优化器统计**：`daily_bars` 816 万行、`WITHOUT ROWID`，若从没跑过统计，
    # 优化器对选择性一无所知，选池那条 `WHERE date <= ? GROUP BY code` 会走全表
    # SCAN（实测 3570ms）；有统计后走主键跳扫，1009ms（3.5 倍）。
    # 用 `PRAGMA optimize` 而不是全库 `ANALYZE`：前者只在统计过期时才干活，
    # 后者每次都扫一遍全库（1.2GB，成本高得多）。
    # 放在清理尾巴上：此刻没有盘中写压力。
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.Error as e:                       # noqa: BLE001 —— 统计失败不该中断清理
        out["optimize"] = -1
        log.warning("PRAGMA optimize 失败：%s", e)

    used = disk_used_pct(data_dir)
    out["disk_used_pct"] = int(used)
    if dispatcher is not None:
        if used > 95:
            dispatcher.alert("disk", f"磁盘水位 {used:.0f}%", "P0")
        elif used > 85:
            dispatcher.alert("disk", f"磁盘水位 {used:.0f}%", "P1")
    return out


def disk_used_pct(path: str) -> float:
    """`/app/data` 的占用百分比；**独立于表清理，无论如何都要算**。"""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return 0.0
    return usage.used / usage.total * 100 if usage.total else 0.0
