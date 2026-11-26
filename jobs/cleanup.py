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
    out["disk_used_pct"] = None if used is None else int(used)
    if dispatcher is not None:
        # **必须传 `conn`**（Codex 评审）：不传就绕过了去抖，每日 cleanup 会在
        # 稳态水位下**再推一条**，把 10 分钟巡检的去抖白做。
        _alert_disk(dispatcher, used, conn)
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


#: 同一档位内，水位变化**超过这个百分点**才再告警一次。
#:
#: 为什么需要：`Dispatcher.alert` 的 30 分钟去重只按 `kind + 时间`，
#: **不看内容**。生产实测 2026-11-25：磁盘一整天稳在 80~82%，
#: `disk_p2` 却推了 **17 条**，每条都是同一句"磁盘水位 82%（75% 以上，
#: 建议清理）"。这种推送比不推送更糟——它训练用户忽略告警，
#: 真正该升到 P1/P0 的那条会被当成噪音划掉。
DISK_RENOTIFY_PCT = 3.0


def _disk_state(conn) -> tuple[str | None, float | None]:
    """读出上次告警的档位与水位数（没有就 `(None, None)`）。"""
    rows = dict(conn.execute(
        "SELECT name, value FROM job_state WHERE name IN"
        " ('disk_alert.kind', 'disk_alert.pct')").fetchall())
    kind = rows.get("disk_alert.kind") or None
    try:
        pct = float(rows["disk_alert.pct"]) if rows.get("disk_alert.pct") else None
    except (TypeError, ValueError):
        pct = None
    return kind, pct


def _save_disk_state(conn, kind: str, pct: float) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES (?, ?)",
                     ("disk_alert.kind", kind))
        conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES (?, ?)",
                     ("disk_alert.pct", f"{pct:.2f}"))


def _alert_disk(dispatcher, used: float | None, conn=None) -> None:
    """按水位发告警。

    **`used is None`（读不到）必须单独告警，不能当成 0% 静默**：
    读不到的常见原因是 bind mount 丢失 / 路径不存在 / EIO，
    这时磁盘可能已经满了，而 0% 会让 75/85/95 三档全部不触发。

    **kind 必须带档位**（`disk_p2` / `disk_p1` / `disk_p0`）。
    `Dispatcher.alert` 的 30 分钟去重判据是 `kind + 时间`，**不看 level**：
    75% 先发一条 P2 之后，10 分钟内升到 85%/95% 的 P1/P0 会被
    "同 kind 刚发过"直接压掉——最该升级的那次反而发不出去。
    分档 kind 让它们各自去重，升级一定发得出去。
    """
    if used is None:
        dispatcher.alert("disk_unknown", "读不到磁盘占用（statvfs 失败），无法判断水位", "P1")
        return

    if used >= DISK_CRIT_PCT:
        kind, level, text = ("disk_p0",
                             "P0",
                             f"磁盘水位 {used:.0f}%（{DISK_CRIT_PCT:.0f}% 以上，随时可能写不进）")
    elif used >= DISK_HIGH_PCT:
        kind, level, text = ("disk_p1",
                             "P1",
                             f"磁盘水位 {used:.0f}%（{DISK_HIGH_PCT:.0f}% 以上）")
    elif used >= DISK_WARN_PCT:
        kind, level, text = ("disk_p2",
                             "P2",
                             f"磁盘水位 {used:.0f}%（{DISK_WARN_PCT:.0f}% 以上，建议清理）")
    else:
        # 回到安全线以下：**清掉状态**，这样下次再越线能立刻告警，
        # 而不是因为"上次是 70%"被自己的去抖压掉。
        if conn is not None:
            with conn:
                conn.execute("DELETE FROM job_state WHERE name IN"
                             " ('disk_alert.kind', 'disk_alert.pct')")
        return

    # **同档位且水位没明显变化就闭嘴**：30 分钟去重只看 kind + 时间，
    # 不看内容，于是稳态 82% 一天推 17 条一模一样的。
    if conn is not None:
        last_kind, last_pct = _disk_state(conn)
        if last_kind == kind and last_pct is not None \
                and abs(used - last_pct) < DISK_RENOTIFY_PCT:
            log.debug("磁盘仍处 %s 档（%.1f%%，上次 %.1f%%），不重复告警",
                      kind, used, last_pct)
            return

    # **先发再记状态**（Codex 评审）：`Dispatcher.alert()` 会在维护模式、
    # 30 分钟去重时直接返回、也可能推送失败。原来先写状态再发，
    # 于是"记成已告警、其实一条都没送达"，之后同水位一直被去抖压着——
    # 磁盘真涨上去也不会有第二条。
    # 返回 `False` 表示这次没送到：不记状态，下一轮（10 分钟后）还会再试。
    delivered = dispatcher.alert(kind, text, level)
    if conn is not None and delivered is not False:
        _save_disk_state(conn, kind, used)


def check_disk(conn, dispatcher=None, path: str | None = None) -> float | None:
    """**独立于每日 cleanup 的磁盘巡检**（每 10 分钟一次）。

    监控的是**宿主机根分区**而不是只看 `/app/data`：容器里 `df /` 看到的就是
    宿主机文件系统（Docker 的 `/var/lib/docker` 也在上面），所以 Docker 自身的
    可写层膨胀也会被算进来——2026-11-21 那次就是这么被撑爆的。

    `/app/data` 不存在时（bind mount 丢失）**明确记一条警告**再退到 `/`：
    悄悄换测量对象会让人以为监控一直正常。
    """
    if path is None:
        if Path("/app/data").exists():
            path = "/"
        else:
            log.warning("/app/data 不存在（bind mount 丢了？），改测 /")
            path = "."
    used = disk_used_pct(path)
    if dispatcher is not None:
        _alert_disk(dispatcher, used, conn)
    return used


def disk_used_pct(path: str) -> float | None:
    """`/app/data` 的占用百分比；**独立于表清理，无论如何都要算**。

    **读不到时返回 `None`，绝不返回 `0.0`**：0 是"磁盘很空"，
    它同时意味着所有阈值（75/85/95）都不触发——**告警彻底静默**，
    而 `push_log` 里只会留下一条"磁盘 0%"的假记录。这和本函数
    刚修掉的"口径偏低导致 P0 永不触发"是同一个后果，只是触发条件
    从"算错"换成了"读不到"（bind mount 没挂上、路径不存在、EIO）。

    **必须用 `used/(used+free)`，不能用 `used/total`**：ext4 默认给 root
    保留 5% 空间（本机实测 5.02 GB / 98 GB）。`total` 把这块保留区算进分母，
    于是同一个磁盘本函数报 80.3%、`df` 报 84.6%——**差 4.3 个百分点**。

    后果不是"数字难看"，而是**告警永远不会按时触发**：
    P1（85%）要等 `df` 到 89.5%，P0（95%）要等 `df` 到 100.2%——
    磁盘写满那一刻 P0 还没到线。2026-11-21 那次撑爆正是这个形态
    （那时是"一天只查一次"，这次是"口径不对"，同一个结果）。

    RUNBOOK §571 的约定也是「与 df 一致（±2%）」，本函数此前违反了它。
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    # `free` 是给非 root 的可用量（f_bavail），保留块不在其中——
    # 用它做分母才和 `df` 的 Use% 同口径。
    usable = usage.used + usage.free
    if not usable:
        return None
    # 夹到 [0,100]：部分网络/配额文件系统 `f_bavail` 可能为负，
    # 不夹会写出 `-3%` 这种日志，让人以为读数坏了。
    return min(100.0, max(0.0, usage.used / usable * 100))


#: 备份允许的最大陈旧时间（小时）。备份在 23:30 跑，次日 08:55 的自检若看到
#: 最新备份超过这个时长，说明**昨晚那次没跑成**。
#: **必须 < 33.4 小时**（次日 08:55 距昨晚 23:30 的实际间隔）：
#: 我原来设 36，评审实测发现**单晚中断恰好是 33.4h，永远摸不到阈值**——
#: 连漏两晚（57.4h）才报警，等于对最常发生的单晚故障完全失明。
#: 29h 留 4.4h 余量：既覆盖 23:30→次日 08:55 的正常间隔，又能抓住单晚断档。
BACKUP_STALE_H = 29


def check_backup_freshness(conn, data_dir, now=None, dispatcher=None,
                           *, stale_h: int = BACKUP_STALE_H) -> float | None:
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
