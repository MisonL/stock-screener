"""bar 标注与 B4 / B8 / D2 共用的下跌期窗口（spec §4.6.6 末尾红框、§4.6.5）。

`annotate` 把 `daily_bars`（raw）与 `daily_bars_adj`（复权）**按日期对齐**成 `DayRow`：
复权值给因子用，raw 值给涨跌停与盘中快照桥接用。涨跌停与一字板判定一律在 raw 上做——
复权价的「容差 1 分」在 5.63 倍的复权价上等于 0.002%，毫无意义，且涨跌停价本身按 raw
昨收算（§4.6.6）。除权日的「昨收」取 `ex_refs[date]`（`core.adjust.ex_ref()` 的参考价），
其余日取上一根 raw 收盘：10 送 10 之后按上一根 raw 收盘算跌停价会错一倍。

`decline_window` 是下跌期唯一的来源，只算一次：B8 的「下跌期最低」、B4 的分母、
§4.6.4 放量日判定的「下跌期均量」都从这里拿，否则 B4 的分子分母说的不是同一段下跌。
"""
from __future__ import annotations

from collections.abc import Callable

from core.market import limit_hit, limit_prices, oneword
from core.models import Bar, DayRow, Decline

# 「近 250 日」= 250 个**有 bar 的交易日**（§4.6.6「停牌日的处理」），与 A1 同源（§4.6.5）。
_HIGH_LOOKBACK = 250

# 复牌首日及其后 5 个交易日换手异常偏高，不进下跌期均量的分母（§4.6.6「复牌日的处理」）。
_RESUME_SKIP_DAYS = 5


def annotate(
    raw: list[Bar],
    adj: list[Bar],
    market: str,
    st_at: Callable[[int], str],
    ex_refs: dict[int, float],
    resume_dates: set[int],
) -> list[DayRow]:
    """按日期对齐 raw 与 adj，逐根标注涨跌停 / 一字板 / 复牌（§4.6.6）。

    `st_at(date)` 给**那一天**的 ST 状态（不是今天的）——摘帽股的 250 日窗口里混着
    5% 限价的日子，用今天的 10% 回推会全部漏判。`ex_refs` 是除权日 → 除权参考价。
    """
    adj_by_date = {bar.date: bar for bar in adj}
    rows: list[DayRow] = []
    prev_raw_close: float | None = None
    last_resume: int | None = None
    for i, bar in enumerate(raw):
        a = adj_by_date.get(bar.date)
        if a is None:
            raise ValueError(f"{bar.code} {bar.date} 缺 daily_bars_adj 行")

        if bar.date in resume_dates:
            last_resume = i
        # 序列里还没有复牌日时从首根起算：末根因此不会误触发 resume_cooldown。
        since_resume = i + 1 if last_resume is None else i - last_resume

        hit = None
        prev_close = ex_refs.get(bar.date, prev_raw_close)
        if prev_close is not None:
            limits = limit_prices(prev_close, market, st_at(bar.date), bar.date)
            if limits is not None:
                hit = limit_hit(bar.close, *limits)

        rows.append(
            DayRow(
                date=bar.date, o=a.open, h=a.high, l=a.low, c=a.close, v=a.volume,
                raw_h=bar.high, raw_l=bar.low, raw_c=bar.close, raw_v=bar.volume,
                amount=bar.amount, hit=hit, oneword=oneword(bar.high, bar.low, hit),
                resume=bar.date in resume_dates, since_resume=since_resume,
            )
        )
        prev_raw_close = bar.close
    return rows


def decline_window(rows: list[DayRow], box_start: int, tail_max: int = 60) -> Decline | None:
    """下跌期 = [近 250 根内复权最高价所在日, 箱体起点前一日]，再取末尾最多 `tail_max` 根。

    同价多日取**最后一次**高点（双顶的第二个顶才是这段下跌的起点）。`length <= 0`
    （高点落在箱体内或之后）返回 `None`——27 只「高位横盘」票就是靠这条挡掉的。
    """
    if not rows:
        return None

    first = max(0, len(rows) - _HIGH_LOOKBACK)
    start = first
    for i in range(first + 1, len(rows)):
        if rows[i].h >= rows[start].h:      # 同价取最后一次
            start = i

    length = box_start - start
    if length <= 0:
        return None

    tail_start = max(start, box_start - tail_max)
    tail = rows[tail_start:box_start]
    kept = [r for r in tail if not (r.resume or 0 < r.since_resume <= _RESUME_SKIP_DAYS)]
    return Decline(
        start=start,
        length=length,
        tail_start=tail_start,
        mean_vol=sum(r.v for r in kept) / len(kept) if kept else 0.0,
        min_low=min(r.l for r in tail),
        oneword_down=sum(1 for r in rows[start:box_start] if r.oneword == "down"),
    )
