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
from pathlib import Path
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
        #
        # **按日期（3 个自然日），不是按"过期满 72 小时"**（v0.2.90 修的 L3）：
        # 用 `due_at < now - 3d` 时，一条 4 天前到期、但昨天才被作废的闹钟
        # 会**活到明天**才被删——它与 `agent/main.py::sweep_stale_alarms()` 的
        # "按日期"口径不一致，两条路径对"过期"的定义不同，排查时对不上。
        # 这里统一成"3 个自然日之前的 due_at"。
        if _has_table(conn, "agent_schedule"):
            stale_day = (now - timedelta(days=3)).date().isoformat()
            out["agent_schedule.cancelled"] = conn.execute(
                "UPDATE agent_schedule SET status='cancelled'"
                " WHERE status='pending' AND substr(due_at, 1, 10) < ?",
                (stale_day,)).rowcount
            out["agent_schedule"] = conn.execute(
                "DELETE FROM agent_schedule"
                " WHERE status IN ('fired','cancelled') AND substr(due_at, 1, 10) < ?",
                (stale_day,)).rowcount
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
        _alert_disk(dispatcher, used)
    return out


#: 磁盘水位阈值。**分三档**：2026-11-21 那次本机事故里，
#: 原来的 85/95 两档在"一天只查一次"的前提下等于没有预警——
#: Docker 虚拟磁盘从 70% 涨到 100% 只用了几个小时，等到 16:00 的 cleanup
#: 跑起来时 PostgreSQL 已经起不来了。
#:
#: 现在由 `jobs/main.py` 每 10 分钟查一次（与交易日无关，磁盘不看开市），
#: 75% 就提前喊，留出清理时间。
DISK_WARN_PCT = 75.0
DISK_HIGH_PCT = 85.0
DISK_CRIT_PCT = 95.0


def _alert_disk(dispatcher, used: float) -> None:
    """按水位发告警。同 kind 30 分钟去重由 `Dispatcher.alert` 保证。"""
    if used >= DISK_CRIT_PCT:
        dispatcher.alert("disk", f"磁盘水位 {used:.0f}%（{DISK_CRIT_PCT:.0f}% 以上，随时可能写不进）", "P0")
    elif used >= DISK_HIGH_PCT:
        dispatcher.alert("disk", f"磁盘水位 {used:.0f}%（{DISK_HIGH_PCT:.0f}% 以上）", "P1")
    elif used >= DISK_WARN_PCT:
        dispatcher.alert("disk", f"磁盘水位 {used:.0f}%（{DISK_WARN_PCT:.0f}% 以上，建议清理）", "P2")


def check_disk(conn, dispatcher=None, path: str | None = None) -> float:
    """**独立于每日 cleanup 的磁盘巡检**（每 10 分钟一次）。

    监控的是**宿主机根分区**而不是只看 `/app/data`：容器里 `df /` 看到的就是
    宿主机文件系统（Docker 的 `/var/lib/docker` 也在上面），所以 Docker 自身的
    可写层膨胀也会被算进来——2026-11-21 那次就是这么被撑爆的。

    返回占用百分比，供调用方记录。
    """
    used = disk_used_pct(path or ("/" if Path("/app/data").exists() else "."))
    if dispatcher is not None:
        _alert_disk(dispatcher, used)
    return used


def disk_used_pct(path: str) -> float:
    """`/app/data` 的占用百分比；**独立于表清理，无论如何都要算**。"""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return 0.0
    return usage.used / usage.total * 100 if usage.total else 0.0


#: 备份允许的最大陈旧时间（小时）。备份在 23:30 跑，次日 08:55 的自检若看到
#: 最新备份超过这个时长，说明**昨晚那次没跑成**。
#: 2026-11-21 实测到的真实缺口：`data/backups/` 里有 11-13/14/16/17/18/19，
#: 独独缺了 11-20——那天晚上连着做了几次部署，jobs 容器在 23:30 前后被重建，
#: 而 `misfire_grace_time=3600` 只兜一小时，过了就静默跳过。
BACKUP_STALE_H = 36


def check_backup_freshness(conn, data_dir, now=None, dispatcher=None,
                           *, keep: int = 7, stale_h: int = BACKUP_STALE_H) -> float | None:
    """返回最新备份距今的小时数；**超时则告警**。

    为什么需要它：备份只有 `misfire_grace_time=3600` 的兜底，部署窗口正好压在
    23:30 时那一晚的备份会被**静默跳过**——7 份保留位里出现一个空洞，
    谁都不会发现，直到真要从备份恢复的那天。

    只看文件时间，不碰数据库，所以不需要写锁。
    """
    from datetime import datetime, timezone
    from pathlib import Path as _P

    root = _P(data_dir) / "backups"
    if not root.is_dir():
        return None
    files = sorted(root.glob("market-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        if dispatcher is not None:
            dispatcher.alert("backup_stale", "没有任何数据库备份", "P1")
        return None
    newest = files[0]
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    age_h = (now_ts - newest.stat().st_mtime) / 3600
    if dispatcher is not None and age_h > stale_h:
        dispatcher.alert(
            "backup_stale",
            f"最新备份是 {newest.name}（{age_h:.0f} 小时前），超过 {stale_h} 小时",
            "P1")
    return age_h
