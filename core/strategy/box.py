"""最长有效箱体搜索（spec §4.6.1；一字板与复牌规则见 §4.6.6）。

从 `max_lookback` 到 `min_n` **递减**试 N，第一个同时满足 (a)(b)(c)(d) 的就是 N\\*：
窗口 = `rows[-(N+1):-1]`，**右端是昨天、不含今天**（§4.6.1 2026-10-28 修正：含今天会让
箱体吸收突破日自身的最高价，`pos_in_box` 与 (d) 双双失真）。今天只用来做 (d) 的有效性判定。

(a) `(max(high) − min(low)) / min(low) <= box_range`
(b) `median((high − low) / close) <= daily_amp`
(c) `|slope_ols(close) / mean(close)| <= slope`
(d) `low × today_low <= 今日复权收盘 <= high × (1 + c3)`

(d) 的上下界用**当前 N** 的极值，必须在循环里随 N 重算——它不是「选定最长 N 之后的独立一步」。
注意窗口嵌套的一个数学事实：N 越大窗口越宽、`low` 只降 `high` 只升，所以 (d) 对更长的 N
**单调更宽**；「长 N 因 (d) 不过而退短 N」在嵌套窗口下不会发生，能退短 N 的是 (a)(b)(c)
与触板/复牌约束。把 (d) 放进循环仍然必要：它保证「今天已跌破/追高」的票不会因为更长的
窗口极值更宽而蒙混过关。

窗口内的三种特殊 bar（§4.6.6）：
- **一字跌停**：硬作废该 N（连续跌停被误判成「横盘极窄」正是要拦的），递减重试；
- **普通触板**（有振幅的涨停/跌停）：窗口左端必须晚于最后一个触板日——等价于把窗口从触板
  次日重启，**不是整窗作废**；
- **一字涨停**：封板，不作废也不重启（它走 `sealed=true` 独立突破通道）；把它当普通触板会让
  连板股的窗口每天往前推一天，横盘窗口永远起不来。

零振幅窗口（`high == low`）直接作废该 N：`pos_in_box` 与 (a) 都会在这里除零，而「零振幅的箱体」
不是箱体，不给兜底值。
"""
from __future__ import annotations

from statistics import median

from core.indicators import slope_pct
from core.models import Box, DayRow, Reject


def find_box(rows: list[DayRow], p, box_cfg) -> Box | Reject:
    """返回最长有效箱体，或 `Reject("box", reason)`。

    `p` 是 `Config.market(m)`——(a)(b)(c)(d) 的 `box_range / daily_amp / slope / c3 /
    today_low` 都在 `by_market:` 段；`box_cfg` 是 `CFG.strategy.box`（`min_n` / `max_lookback`）。
    `reason` 取值固定为 `no_window / oneword_down / resume_cooldown / oneword_up_today / zero_range`。
    """
    min_n = int(box_cfg.min_n)
    n_rows = len(rows)
    if n_rows < min_n + 1:
        return Reject("box", "no_window")

    today = rows[-1]
    # 复牌冷却：复牌首日 + 其后不足 10 根，价格与量能都还不是常态（§4.6.6）。
    if today.since_resume < int(p.universe.resume_cooldown):
        return Reject("box", "resume_cooldown")
    # 今天一字涨停：走 sealed 独立通道，不进池。
    if today.oneword == "up":
        return Reject("box", "oneword_up_today")

    # 无复牌历史的序列里 annotate 从首根起算 since_resume，下标会落到负值——正好表示「没有复牌日」。
    resume_index = n_rows - 1 - today.since_resume

    saw_oneword_down = False
    saw_zero_range = False
    for n in range(min(int(box_cfg.max_lookback), n_rows - 1), min_n - 1, -1):
        left = n_rows - n - 1
        window = rows[left : n_rows - 1]

        if any(r.oneword == "down" for r in window):
            saw_oneword_down = True
            continue

        last_hit = _last_plain_hit(window, left)
        if last_hit is not None and left <= last_hit:
            continue
        if left <= resume_index:
            continue

        high = max(r.h for r in window)
        low = min(r.l for r in window)
        if high == low:
            saw_zero_range = True
            continue

        if (high - low) / low > p.box_range:
            continue
        if median((r.h - r.l) / r.c for r in window) > p.daily_amp:
            continue
        if abs(slope_pct([r.c for r in window])) > p.slope:
            continue

        close = today.c
        if close < low * p.today_low or close > high * (1 + p.c3):
            continue

        return Box(
            n=n,
            start=left,
            high=high,
            low=low,
            pos_in_box=(close - low) / (high - low),
        )

    if saw_oneword_down:
        return Reject("box", "oneword_down")
    if saw_zero_range:
        return Reject("box", "zero_range")
    return Reject("box", "no_window")


def _last_plain_hit(window: list[DayRow], left: int) -> int | None:
    """窗口内最后一个**普通**触板日的绝对下标；一字涨停不算触板（§4.6.6 2026-11-02）。

    一字跌停同样带 `hit`，但含它的窗口已在上一步整窗作废，走不到这里。
    """
    last = None
    for offset, row in enumerate(window):
        if row.hit is not None and row.oneword != "up":
            last = left + offset
    return last
