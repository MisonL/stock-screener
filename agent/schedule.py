"""唤醒调度（计划 Task 11，spec §5.4.2 / §5.4.7）。

`next_heartbeat`：自适应心跳间隔，S-A18 统一口径。
`schedule_alarm` / `after_alarm`：模型自排的闹钟。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core.clock import SH  # noqa: F401 —— `Deps` 组装点需要 `SH`

# 09:15–10:00 与 14:30–15:00：2 分钟密集
# 11:00–11:30 与 13:00–13:30：10 分钟放松
# 其余 5 分钟；任何情况下 ≤ 10 分钟；午休与 15:05 之后不排
_DENSE = 120         # 2 min
_LOOSE = 600         # 10 min
_NORMAL = 300        # 5 min


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def next_heartbeat(now: datetime) -> datetime:
    """下次心跳时间。跳过午休（11:30–13:00）与盘后（>15:00）。

    午休 95 分钟不排心跳，但 `next_heartbeat` 不能把间隔算成 95 分钟——间隔
    最大值就是 10 分钟（`_LOOSE`），所以午休期间调用时返回 13:00。
    """
    minute = now.replace(second=0, microsecond=0)

    def clamp(when):
        """排出来的时间不能落进午休（11:30–13:00）或盘后（>15:00）。

        午休没有心跳消费者：11:29 加上 10 分钟得到的 11:39 落进午休，就该直接跳到
        13:00；否则这次心跳永远不发生（`pending_retry` 无人接手、确认卡无人过期）。
        """
        hm = _hm(when)
        if "11:30" <= hm < "13:00":
            return when.replace(hour=13, minute=0)
        if hm >= "15:00":
            tomorrow = when + timedelta(days=1)
            return tomorrow.replace(hour=9, minute=15, second=0, microsecond=0)
        return when

    if _hm(now) < "09:15":
        return minute.replace(hour=9, minute=15)
    if _hm(now) < "10:00":
        return clamp(minute + timedelta(seconds=_DENSE))
    if _hm(now) < "11:00":
        return clamp(minute + timedelta(seconds=_NORMAL))
    if _hm(now) < "11:30":
        return clamp(minute + timedelta(seconds=_LOOSE))
    if _hm(now) < "13:00":
        return minute.replace(hour=13, minute=0)      # 午休直接跳到 13:00
    if _hm(now) < "13:30":
        return clamp(minute + timedelta(seconds=_LOOSE))
    if _hm(now) < "14:30":
        return clamp(minute + timedelta(seconds=_NORMAL))
    if _hm(now) < "15:00":
        return clamp(minute + timedelta(seconds=_DENSE))
    # > 15:00：盘后不排；次日 09:15
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=9, minute=15, second=0, microsecond=0)


def schedule_alarm(conn, turn_id: int, due_at: str, market: str,
                   target: str | None = None, note: str = "",
                   now: datetime | None = None) -> int:
    """插一条闹钟；`due_at` 是 Asia/Shanghai ISO 串。

    `now` **必须能从外部传**：原来 `created_at` 直接取 `datetime.now(SH)`，
    而 `due_at` 来自注入的 `clock.now()`。生产用 `SystemClock` 无碍，
    但测试/回放用 `FixedClock` 时两个字段会差出真实日期——
    `get_own_history` 按 `created_at` 筛当日，模型就查不到自己刚排的闹钟了。
    """
    created = (now or datetime.now(SH)).isoformat()
    cur = conn.execute(
        "INSERT INTO agent_schedule(created_at, due_at, market, target, note, status)"
        " VALUES (?,?,?,?,?,'pending')",
        (created, due_at, market, target, note))
    conn.commit()
    return cur.lastrowid


def due_alarms(conn, now: datetime) -> list[dict]:
    """到期且未认领的闹钟。"""
    rows = conn.execute(
        "SELECT id, created_at, due_at, market, target, note, status, fired_turn"
        " FROM agent_schedule WHERE status = 'pending' AND due_at <= ?"
        " ORDER BY due_at LIMIT 10", (now.isoformat(),)).fetchall()
    return [{"id": r[0], "created_at": r[1], "due_at": r[2], "market": r[3],
             "target": r[4], "note": r[5], "status": r[6], "fired_turn": r[7]}
            for r in rows]


def after_alarm(conn, alarm_id: int) -> bool:
    """**认领**闹钟（条件更新，只成功一次）。

    认领与「记下执行它的回合」是两件事：认领发生在跑回合之前（那时还没有 turn_id），
    写 `fired_turn` 发生在 `journal.record` 之后。合成一步会让第二次更新必然失败
    （status 已不是 'pending'），`fired_turn` 就永远是空。
    """
    cur = conn.execute(
        "UPDATE agent_schedule SET status = 'fired' WHERE id = ? AND status = 'pending'",
        (alarm_id,))
    conn.commit()
    return cur.rowcount == 1


def release_alarm(conn, alarm_id: int) -> None:
    """把认领**退回去**（回合失败时用）。

    「先认领再跑」是为了防重复回合，但它让失败变成了永久丢失：
    `after_alarm` 在跑回合**之前**就把 status 改成 `fired`，若这一轮
    `_run` 因为 LLM 挂掉/预算耗尽/超时而失败，没有任何逻辑把闹钟放回
    `pending`——模型说的"我 30 分钟后回来看"就静默作废了。
    清理任务只处理 3 天前的 pending，不会补跑。

    退回成 `pending` 后下一轮（2 秒后）会重新取出它，与心跳的
    `pending_retry` 是同一个思路。
    """
    # **要连 `fired_turn` 一起清掉**：`_run` 里是先 `set_fired_turn` 再判失败，
    # 带上 `fired_turn IS NULL` 这个条件会让释放永远不生效（实测就是这样）。
    # 清空后下一次成功的回合会重新写上正确的 turn_id。
    conn.execute("UPDATE agent_schedule SET status = 'pending', fired_turn = NULL"
                 " WHERE id = ? AND status = 'fired'", (alarm_id,))
    conn.commit()


def set_fired_turn(conn, alarm_id: int, turn_id: int) -> None:
    """把「执行它的回合」写回闹钟（只补一次，不覆盖已有的）。"""
    conn.execute("UPDATE agent_schedule SET fired_turn = ?"
                 " WHERE id = ? AND fired_turn IS NULL", (turn_id, alarm_id))
    conn.commit()
