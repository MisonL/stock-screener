"""推送计数的单一出口（计划 Task 7 建 `channel_b_today`，Task 12 扩展）。

**口径只有一份**：batch1 的 `Dispatcher`、batch2 的注意力层、`/api/status` 都从这里
取数。任何一处另写一份 SQL，迟早出现「阈值被自己写出的 suppressed 行抬高」的正反馈。

三条共用规则：
- **只认 `status IN ('pending','accepted')`**：`suppressed` / `failed` 不是「已推」。
  把被压的算成已推，暴跌日写满 60 条被压行后后续全部熔断——「越压越压」。
- **第二段不是新通知**：它是同一 `seq_id` 的更新，用 `exclude_segments=(2,)` 排除。
- **确认卡/确认审计行不是通知**：交互卡与留痕行，用 `exclude_kinds` 排除。
"""
from __future__ import annotations

from dataclasses import dataclass

PUSHED_STATUS = ("pending", "accepted")
NON_PUSH_KINDS = ("confirm_card", "confirm_result")
CHANNEL_A_KINDS = ("breakout", "breakdown")


@dataclass
class PushRow:
    """`push_log` 的一行。**必须含 `event_id`**：`.agent_note` 要「对原条发第二段」时
    靠它定位第一段——拿不到 event_id 就只能新开一条，用户会看到两条重复提醒。"""

    id: int
    ts: str
    code: str | None
    kind: str
    level: str
    seq_id: str
    segment: int
    status: str
    trade_date: int
    event_id: int | None


def _rows(conn, sql: str, params) -> list[PushRow]:
    return [PushRow(id=r[0], ts=r[1], code=r[2], kind=r[3], level=r[4], seq_id=r[5],
                    segment=r[6], status=r[7], trade_date=r[8], event_id=r[9])
            for r in conn.execute(sql, params).fetchall()]


_COLS = "id, ts, code, kind, level, seq_id, segment, status, trade_date, event_id"


def recent_independent_push(conn, code: str, kind: str | None = None,
                            minutes: int = 30, now=None) -> PushRow | None:
    """该 code 最近 `minutes` 分钟内的**独立**推送（`segment=1`）。

    **`kind` 必须由调用方传**：只按 code 查会把「同一票先突破后跌破」误判成重复，
    把新事实压掉——那正是用户最需要看到的那条。
    """
    clock_now = now if now is not None else _now()
    since = clock_now.timestamp() - minutes * 60
    sql = (f"SELECT {_COLS} FROM push_log WHERE code = ? AND segment = 1"
           " AND status IN ('pending','accepted') AND ts >= ?")
    params: list = [code, _iso(since, clock_now)]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY ts DESC, id DESC LIMIT 1"
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


def pushed_today(conn, kinds=None, channel: str | None = None, exclude_segments=(),
                 exclude_kinds=(), day: int | None = None) -> int:
    """当天**新通知**条数（按 `seq_id` 去重：合并批每只一行，按行数算会提前熔断）。

    `channel`：`'A'` → 策略层信号、`'B'` → 其余；`None` → 不按通道过滤。
    `push_log` 没有 `channel` 列，通道只能由 `kind` 判定。
    """
    today = day if day is not None else _today()
    sql = "SELECT count(DISTINCT seq_id) FROM push_log WHERE trade_date = ?"
    params: list = [today]
    sql += " AND status IN ('pending','accepted')"
    if channel == "A":
        sql += f" AND kind IN ({','.join('?' * len(CHANNEL_A_KINDS))})"
        params.extend(CHANNEL_A_KINDS)
    elif channel == "B":
        sql += f" AND kind NOT IN ({','.join('?' * len(CHANNEL_A_KINDS))})"
        params.extend(CHANNEL_A_KINDS)
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params.extend(kinds)
    if exclude_kinds:
        sql += f" AND kind NOT IN ({','.join('?' * len(exclude_kinds))})"
        params.extend(exclude_kinds)
    if exclude_segments:
        sql += f" AND segment NOT IN ({','.join('?' * len(exclude_segments))})"
        params.extend(exclude_segments)
    return int(conn.execute(sql, params).fetchone()[0])


def channel_b_today(conn, day: int | None = None) -> int:
    """今天**已推出去的**通道 B 条数（决定 B9/D8）。

    只计：`kind='agent_note'`、`segment=1`、`status IN ('pending','accepted')`。

    - `segment` 列是 `NOT NULL DEFAULT 1`，不存在 NULL，「NULL 或 1」是无效条件；
    - 被压（`suppressed`）与失败（`failed`）的行**不算已推**：把它们算进来，阈值会被
      自己写出的 suppressed 行越抬越高，形成「越压越压」的正反馈；
    - 模板通道 A 与第二段（`segment=2`）不计入。
    """
    today = day if day is not None else _today()
    row = conn.execute(
        "SELECT count(*) FROM push_log WHERE kind = 'agent_note' AND segment = 1"
        " AND trade_date = ? AND status IN ('pending','accepted')", (today,)).fetchone()
    return int(row[0]) if row else 0


def agent_notes_today(conn, day: int | None = None) -> int:
    """今天写过的通道 B 条目数（**含被压的**）：`seq_id` 的 `n` 用它的下一号。

    与 `channel_b_today` 口径不同是有意的：序号必须单调，否则被压条目会和后来者
    撞同一个 `seq_id`；而「今天推了几条」只看真的推出去的。
    """
    today = day if day is not None else _today()
    row = conn.execute(
        "SELECT count(*) FROM push_log WHERE kind = 'agent_note' AND trade_date = ?",
        (today,)).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------- 内部


def _now():
    """**只为测试方便**的默认值。生产路径一律显式传 `now` / `day`。

    这里默认走 `SystemClock` 是唯一可接受的写法（policy 没有注入时钟的口子），
    代价是：凡是拿 `FixedClock` 的调用方，**必须显式传 day**，否则「今天」会
    与业务时钟差一天，日熔断与通道 B 计数全部错位。
    """
    from core.clock import SystemClock

    return SystemClock().now()


def _today() -> int:
    from core.clock import SystemClock

    return SystemClock().today()


def _iso(epoch: float, now) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch, tz=now.tzinfo).isoformat()
