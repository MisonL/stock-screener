"""唤醒调度（计划 Task 11，spec §5.4.2 / §5.4.7）。

`next_heartbeat`：自适应心跳间隔，S-A18 统一口径。
`schedule_alarm` / `after_alarm`：模型自排的闹钟。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core.clock import SH

#: 心跳节奏的**默认值**。真实来源是 `config/agent.yaml::heartbeat`，
#: 由 `next_heartbeat(now, hb)` 的 `hb` 参数传入；这些常量只是"没传就用它们"，
#: 供测试与不读配置的调用方（如 `api/watchdog.py` 的推算）使用。
#:
#: **2026-11-20 的审计发现这里曾是死配置**：`agent.yaml` 的
#: `heartbeat.base_min/tight_min/max_min/tight/relaxed` 五个键**全仓零读取**，
#: 真值写死在这三个常量里，而两者数值恰好相等（2/5/10 min ↔ 120/300/600s）
#: ——所以改配置不生效却完全看不出来。这与当天修掉的"死配置 #2"是同一个形状。
#: 现在 `agent/main.py` 把 `cfg.agent.heartbeat` 传进来。
_DENSE = 120         # 2 min
_LOOSE = 600         # 10 min
_NORMAL = 300        # 5 min


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


#: 默认时段表（`config/agent.yaml::heartbeat.tight / relaxed` 的等价物）。
#: 只有 `hb is None` 时才用——生产走配置。
_DEFAULT_TIGHT = (("09:15", "10:00"), ("14:30", "15:00"))
_DEFAULT_RELAXED = (("11:00", "11:30"), ("13:00", "13:30"))


def _profile(hb) -> tuple[tuple, tuple, dict[str, int]]:
    """把 `cfg.agent.heartbeat` 翻成 (密集区间, 放松区间, {档位: 秒})。

    `hb is None` 时退回模块常量——保持既有调用方与测试的行为不变。
    """
    default = (_DEFAULT_TIGHT, _DEFAULT_RELAXED,
               {"tight": _DENSE, "relaxed": _LOOSE, "base": _NORMAL})
    if hb is None:
        return default
    # **配置坏了也不能抛**：`agent/main.py::_once()` 在心跳推进前裸调用本函数，
    # 而 `run()` 会吞掉异常重试——一次类型错误就会让心跳/闹钟/开盘回合全部停摆
    # （v0.2.93 修过同一个形状）。所以这里退回默认节奏，宁可慢一点也别停。
    try:
        tight = tuple((str(a), str(b)) for a, b in hb.tight)
        relaxed = tuple((str(a), str(b)) for a, b in hb.relaxed)
        iv = {"tight": int(hb.tight_min) * 60,
              "relaxed": int(hb.max_min) * 60,
              "base": int(hb.base_min) * 60}
        if not tight or not relaxed or min(iv.values()) <= 0:
            return default
        return tight, relaxed, iv
    except (AttributeError, TypeError, ValueError):
        import logging
        logging.getLogger(__name__).warning(
            "heartbeat 配置非法（%r），退回默认节奏", hb, exc_info=True)
        return default


def _step_for(hm: str, tight, relaxed, iv) -> int:
    """当前时刻属于哪一档，返回该档的间隔秒数。都不在区间内 → base。"""
    for lo, hi in tight:
        if lo <= hm < hi:
            return iv["tight"]
    for lo, hi in relaxed:
        if lo <= hm < hi:
            return iv["relaxed"]
    return iv["base"]


def next_heartbeat(now: datetime, hb=None) -> datetime:
    """下次心跳时间。跳过午休（11:30–13:00）与盘后（>15:00）。

    午休 95 分钟不排心跳，但 `next_heartbeat` 不能把间隔算成 95 分钟——
    间隔最大值就是 `relaxed` 档（默认 10 分钟），所以午休期间调用时返回 13:00。

    **档位与时段都来自 `cfg.agent.heartbeat`**（v0.2.105）：此前五个键
    （`base_min/tight_min/max_min/tight/relaxed`）全仓零读取，真值写死在这里，
    而数值恰好相等——改配置不生效却看不出来。现在 `hb` 由 `agent/main.py` 传入，
    传 `None` 时退回模块默认值。
    """
    minute = now.replace(second=0, microsecond=0)
    tight, relaxed, iv = _profile(hb)
    hm = _hm(now)

    def clamp(when):
        """排出来的时间不能落进午休（11:30–13:00）或盘后（>15:00）。

        午休没有心跳消费者：11:29 加上 10 分钟得到的 11:39 落进午休，就该直接跳到
        13:00；否则这次心跳永远不发生（`pending_retry` 无人接手、确认卡无人过期）。
        """
        h = _hm(when)
        if "11:30" <= h < "13:00":
            return when.replace(hour=13, minute=0)
        if h >= "15:00":
            tomorrow = when + timedelta(days=1)
            return tomorrow.replace(hour=9, minute=15, second=0, microsecond=0)
        return when

    if hm < "09:15":
        return minute.replace(hour=9, minute=15)
    if hm < "11:30":
        return clamp(minute + timedelta(seconds=_step_for(hm, tight, relaxed, iv)))
    if hm < "13:00":
        return minute.replace(hour=13, minute=0)      # 午休直接跳到 13:00
    if hm < "15:00":
        return clamp(minute + timedelta(seconds=_step_for(hm, tight, relaxed, iv)))
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


#: 闹钟回合失败后最多退回重试几次（第 N 次之后转 `cancelled`）。
#: 3 次 ≈ 容忍一次瞬时故障（LLM 抖动/写库失败），又不至于把整天耗在同一条闹钟上。
ALARM_MAX_ATTEMPTS = 3

#: 退回时把 `due_at` 往后推多少秒（**必须 > 轮询间隔**，否则下一轮立刻又取出）。
#: `_once()` 每 2 秒轮询，这里给 60 秒，既是退避也给上游恢复留时间。
ALARM_RETRY_BACKOFF_S = 60


def release_alarm(conn, alarm_id: int, *, now: datetime | None = None,
                  backoff_s: float | None = None) -> bool:
    """把认领**退回去**（回合失败时用）。返回是否还能再试。

    「先认领再跑」是为了防重复回合，但它让失败变成了永久丢失：
    `after_alarm` 在跑回合**之前**就把 status 改成 `fired`，若这一轮
    `_run` 因为 LLM 挂掉/预算耗尽/超时而失败，没有任何逻辑把闹钟放回
    `pending`——模型说的"我 30 分钟后回来看"就静默作废了。

    **但"退回 pending"不能是无限的**（v0.2.81 修的真实事故）：
    `_once()` 每 2 秒轮询、`due_alarms()` 返回所有 `due_at <= now` 的 pending 行，
    于是一条已过期的闹钟会陷入
    「到期 → 认领 → 失败 → 退回 → 2 秒后又到期」的死循环。
    只要失败原因当天不可能恢复（最典型的是「日预算已用完」），
    它就会一直转到收盘——生产实测一天写了 **17885 行** `agent_turn`
    （全是 `rounds=0, latency_ms=0` 的空转），把日志彻底冲垮。

    所以现在三件事一起做：
      1. `attempts += 1` 记账；
      2. `due_at` 往后推 `backoff_s`（默认 60 秒），**必须大于轮询间隔**；
      3. 超过 `ALARM_MAX_ATTEMPTS` 转 `cancelled`，不再无限退回。
    """
    now = now or datetime.now(SH)
    backoff = ALARM_RETRY_BACKOFF_S if backoff_s is None else float(backoff_s)
    # **要连 `fired_turn` 一起清掉**：`_run` 里是先 `set_fired_turn` 再判失败，
    # 带上 `fired_turn IS NULL` 这个条件会让释放永远不生效（实测就是这样）。
    # 清空后下一次成功的回合会重新写上正确的 turn_id。
    conn.execute(
        "UPDATE agent_schedule"
        "   SET status = CASE WHEN attempts + 1 >= ? THEN 'cancelled' ELSE 'pending' END,"
        "       attempts = attempts + 1,"
        "       fired_turn = NULL,"
        "       due_at = ?"
        " WHERE id = ? AND status = 'fired'",
        (ALARM_MAX_ATTEMPTS,
         (now + timedelta(seconds=backoff)).isoformat(), alarm_id))
    conn.commit()
    row = conn.execute("SELECT status, attempts FROM agent_schedule WHERE id = ?",
                       (alarm_id,)).fetchone()
    return bool(row and row[0] == "pending")


def set_fired_turn(conn, alarm_id: int, turn_id: int) -> None:
    """把「执行它的回合」写回闹钟（只补一次，不覆盖已有的）。"""
    conn.execute("UPDATE agent_schedule SET fired_turn = ?"
                 " WHERE id = ? AND fired_turn IS NULL", (turn_id, alarm_id))
    conn.commit()
