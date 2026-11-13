"""交易时段、交易日历与时钟。

交易日历的权威来源是 `daily_bars` 里 `sh000300` 的日线序列（spec §6.4）：
沪深300 每个交易日必有一根 bar，比人工维护节假日表可靠。

**当日 bar 要到 15:05 `run_eod` 才 append**，所以盘中判断「今天开不开市」
必须用 `TradingCalendar.is_trading_day()`，不能用 `contains()`——后者只回答
「历史里有没有这根 bar」，盘中恒 False，会把今天判成休市。
"""
from __future__ import annotations

import functools
from datetime import date, datetime, time
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from core.config import load_config

SH = ZoneInfo("Asia/Shanghai")

_OPEN = time(9, 30)
_AUCTION_START = time(9, 15)
_MATCH = time(9, 25)
_MORNING_END = time(11, 30)
_AFTERNOON_START = time(13, 0)
_CLOSE_AUCTION = time(14, 57)
_CLOSE = time(15, 0)
_WATCH_END = time(15, 5)


class Clock(Protocol):
    """时间来源。所有落库的非事件类 `trade_date` 都走 `today()`。"""

    def now(self) -> datetime:
        """带 `Asia/Shanghai` 时区的当前时刻。"""

    def today(self) -> int:
        """`YYYYMMDD` 形式的上海日期。"""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(SH)

    def today(self) -> int:
        return _ymd(self.now())


class FixedClock:
    """测试用固定时钟。naive 时间按上海本地时间解释。"""

    def __init__(self, now: datetime):
        self._now = now.replace(tzinfo=SH) if now.tzinfo is None else now.astimezone(SH)

    def now(self) -> datetime:
        return self._now

    def today(self) -> int:
        return _ymd(self._now)


def _ymd(dt: datetime) -> int:
    return int(dt.astimezone(SH).strftime("%Y%m%d"))


def traded_minutes(t: time) -> int:
    """开盘后的交易分钟数：09:30 前 0，午休恒 120，15:00 后 240。"""
    if t < _OPEN:
        return 0
    if t < _MORNING_END:
        return _minutes_between(_OPEN, t)
    if t < _AFTERNOON_START:
        return 120
    if t < _CLOSE:
        return 120 + _minutes_between(_AFTERNOON_START, t)
    return 240


def phase(t: time) -> str:
    """交易时段名：pre / auction / pre_open / morning / lunch / afternoon / closing_auction / closed。"""
    if t < _AUCTION_START:
        return "pre"
    if t < _MATCH:
        return "auction"
    if t < _OPEN:
        return "pre_open"
    if t < _MORNING_END:
        return "morning"
    if t < _AFTERNOON_START:
        return "lunch"
    if t < _CLOSE_AUCTION:
        return "afternoon"
    if t < _CLOSE:
        return "closing_auction"
    return "closed"


def in_watch_window(t: time) -> bool:
    """盯盘窗口：09:15–11:30、13:00–15:05（§7.6.4）。两端都含。"""
    return _AUCTION_START <= t <= _MORNING_END or _AFTERNOON_START <= t <= _WATCH_END


def snapshot_is_today(quote_ts: datetime, today: date) -> bool:
    """上游快照是不是当日的（`run_eod` 与 `capture_auction` 的交易日闸用它）。

    无时区的时间戳按上海本地时间理解——上游返回的就是本地时刻，走 `astimezone`
    会被宿主机时区改写（容器里是 UTC，早晨 8 点的快照会被判成前一天）。
    """
    if quote_ts.tzinfo is None:
        return quote_ts.date() == today
    return quote_ts.astimezone(SH).date() == today


def _minutes_between(start: time, end: time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def _as_date(d: int) -> date:
    return datetime.strptime(str(d), "%Y%m%d").date()


# 法定节假日兜底表（只用于「最后一根 sh000300 bar 之后」的日期）。
#
# 权威来源仍是行情 bar：这里只填国务院办公厅已经公布的年度安排，且只填落在
# 周一至周五的休市日（周末本来就不是交易日，调休上班的周末也不开市）。
# 依据：国办发明电〔2025〕7号（2026 年）、国办发明电〔2026〕10号（2027 年）。
# 过期后要补新年度的日期，否则元旦/春节这些「日历日固定但农历浮动」的假会被
# 当成交易日——那样 jobs 会空跑一轮，抓到的还是上一个交易日的快照。
builtin_holidays: frozenset[int] = frozenset(
    {
        # 2026
        20260101, 20260102,                                          # 元旦
        20260216, 20260217, 20260218, 20260219, 20260220, 20260223,  # 春节
        20260406,                                                    # 清明
        20260501, 20260504, 20260505,                                # 劳动节
        20260619,                                                    # 端午
        20260925,                                                    # 中秋
        20261001, 20261002, 20261005, 20261006, 20261007,            # 国庆
        # 2027
        20270101,                                                    # 元旦
        20270205, 20270208, 20270209, 20270210, 20270211, 20270212,  # 春节
        20270405,                                                    # 清明
        20270503, 20270504, 20270505,                                # 劳动节
        20270609,                                                    # 端午
        20270915,                                                    # 中秋
        20271001, 20271004, 20271005, 20271006, 20271007,            # 国庆
    }
)


@functools.lru_cache(maxsize=1)
def _default_session() -> tuple[str, str]:
    """`config/markets.yaml` 的时段常量（spec §6.4 的冻结时刻，收盘 14:57 不是 15:00）。

    `TradingCalendar(conn)` 的签名是计划定死的（调用方只传连接），所以默认值在这里
    读一次配置；进程内只读一次，测试要用别的时段就把 `session=` 传进去。
    """
    session = load_config(Path("config")).markets.session
    return session.open_time, session.close_time


def default_session() -> tuple[str, str]:
    """`config/markets.yaml` 的 `(open_time, close_time)`；进程内只读一次。

    盘中判据要用这两个时刻（开盘 09:30、收盘冻结 14:57），但不能在 rules 里
    再写一套常量——两套常量迟早分叉。
    """
    return _default_session()


class TradingCalendar:
    """交易日历：`sh000300` 的日线日期集合 + 内置节假日兜底。"""

    def __init__(self, conn, session: tuple[str, str] | None = None):
        rows = conn.execute("SELECT date FROM daily_bars WHERE code = 'sh000300'").fetchall()
        self.days: set[int] = {row[0] for row in rows}
        self._max = max(self.days) if self.days else None
        self._open, self._close = session or _default_session()

    def contains(self, d: int) -> bool:
        """历史里有没有这根 bar（**不是**「今天开不开市」）。"""
        return d in self.days

    def prev(self, d: int) -> int:
        """`d` 之前最近的一个交易日。数据里没有更早的日期时抛 `LookupError`。"""
        earlier = [x for x in self.days if x < d]
        if not earlier:
            raise LookupError(f"{d} 之前没有 sh000300 的 bar，交易日历还不完整")
        return max(earlier)

    def is_trading_day(self, d: int) -> bool:
        """判断「今天开不开市」的唯一入口（jobs D10 / agent B-B24 / watchdog 共用）。"""
        if not self.days:
            # 回填未完成 / 测试没 seed 指数：冷启动阶段一律按休市处理，不能崩。
            return False
        if d in self.days:
            return True
        if self._max is None or d <= self._max:
            # 历史区间内没有这根 bar → 那天休市（节假日、停市）。
            return False
        return _as_date(d).weekday() < 5 and d not in builtin_holidays

    def open_time(self, d: int) -> str | None:
        """交易日返回开盘时刻常量，非交易日返回 `None`（视为休市）。"""
        return self._open if self.is_trading_day(d) else None

    def close_time(self, d: int) -> str | None:
        """交易日返回收盘冻结时刻常量（14:57），非交易日返回 `None`。"""
        return self._close if self.is_trading_day(d) else None
