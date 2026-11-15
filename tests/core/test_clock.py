"""交易时段、交易日历与时钟。"""
from datetime import date, datetime, time

import pytest

from core.clock import (
    FixedClock,
    SystemClock,
    TradingCalendar,
    in_watch_window,
    phase,
    snapshot_is_today,
    traded_minutes,
)
from tests.helpers import SH


@pytest.mark.parametrize("hm,m", [("09:29", 0), ("09:45", 15), ("11:29", 119), ("11:30", 120),
                                  ("12:59", 120), ("13:00", 120), ("13:01", 121), ("15:00", 240), ("15:30", 240)])
def test_traded_minutes(hm, m):
    assert traded_minutes(time.fromisoformat(hm)) == m


def test_watch_window():
    assert [in_watch_window(time.fromisoformat(x)) for x in ("09:14", "09:15", "11:31", "12:59", "15:05", "15:06")] \
        == [False, True, False, False, True, False]


def test_phase_boundaries():
    # 竞价 09:15、撮合 09:25、开盘 09:30、午休 11:30–13:00、收盘竞价 14:57、收市 15:00
    cases = [("09:14", "pre"), ("09:15", "auction"), ("09:24", "auction"), ("09:25", "pre_open"),
             ("09:29", "pre_open"), ("09:30", "morning"), ("11:29", "morning"), ("11:30", "lunch"),
             ("12:59", "lunch"), ("13:00", "afternoon"), ("14:56", "afternoon"),
             ("14:57", "closing_auction"), ("14:59", "closing_auction"), ("15:00", "closed")]
    assert [(hm, phase(time.fromisoformat(hm))) for hm, _ in cases] == cases


def test_calendar_prev_skips_holiday(db_with_index_days):   # 只有 20260930 与 20261009 两天 sh000300
    cal = TradingCalendar(db_with_index_days)
    assert cal.prev(20261009) == 20260930 and cal.contains(20261009) and not cal.contains(20261005)


def test_calendar_reads_session_times(db_with_index_days):
    cal = TradingCalendar(db_with_index_days)
    assert cal.open_time(20260930) == "09:30" and cal.close_time(20260930) == "14:57"   # spec §6.4 冻结时刻
    assert cal.open_time(20261005) is None                    # 非交易日 → None


def test_non_trading_day():   # 国庆期间 10-05 的快照时间戳仍是 09-30 → 不是交易日
    assert not snapshot_is_today(datetime(2026, 9, 30, 15, 0, tzinfo=SH), date(2026, 10, 5))


def test_snapshot_is_today():
    assert snapshot_is_today(datetime(2026, 10, 9, 9, 26, tzinfo=SH), date(2026, 10, 9))
    # 无时区的时间戳按上海本地时间理解（上游就是本地时刻），不能被宿主机时区改写
    assert snapshot_is_today(datetime(2026, 10, 9, 9, 26), date(2026, 10, 9))


def test_is_trading_day_without_bars(db):
    # 回填未完成时不能 `max(.days)` 炸掉；冷启动阶段一律视为休市
    assert TradingCalendar(db).is_trading_day(20261009) is False


def test_is_trading_day_falls_back_when_after_last_bar(db_with_index_days):
    cal = TradingCalendar(db_with_index_days)
    assert cal.is_trading_day(20260930) is True          # 历史里有这根 bar
    assert cal.is_trading_day(20261012) is True          # > max(days)、周一、非节假日 → 兜底为交易日
    assert cal.is_trading_day(20261017) is False         # 周六
    assert cal.is_trading_day(20270101) is False         # 元旦（周五）在内置节假日表里
    assert cal.is_trading_day(20261005) is False         # < max(days) 但历史里没有 → 不是交易日


def test_prev_raises_when_no_earlier_day(db_with_index_days):
    with pytest.raises(LookupError):
        TradingCalendar(db_with_index_days).prev(20260930)


def test_clock_today():
    fixed = FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH))
    assert fixed.today() == 20261030
    assert fixed.now() == datetime(2026, 10, 30, 10, 0, tzinfo=SH)
    assert SystemClock().today() == int(datetime.now(SH).strftime("%Y%m%d"))
