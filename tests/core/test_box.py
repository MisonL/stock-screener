"""Task 15：最长有效箱体搜索（spec §4.6.1、§4.6.6）。

本文件的 `rows_*` 是只造形状的 `list[DayRow]` 常量（价格取整）。窗口语义按 §4.6.1：
`window = rows[-(N+1):-1]`（右端是昨天）。触板日 / 一字板的判定字段（`hit` / `oneword`）
由 `annotate` 填，这里直接构造。

窗口嵌套的一个后果（构造用例时必须知道）：`N` 越大窗口越宽，`box_low` 只降、`box_high`
只升，所以 (d) 的上下界对更长的 N **单调更宽**——不存在「长 N 的 (d) 不过、短 N 的 (d) 过」的
数据（故 `test_d_checked_inside_loop` 用 (a) 的箱体振幅卡掉长窗口：它要验证的是「最长 N 失败
后必须继续递减，不是整只淘汰」，同一断言在 (d) 循环内/循环外都成立，见 ledger 的 Ruling）。
"""
import pytest

from core.models import DayRow
from core.strategy.box import Box, Reject, find_box

BASE = 20250101


def _day(i: int, close: float, high: float | None = None, low: float | None = None,
         oneword: str | None = None, hit: str | None = None,
         resume: bool = False, since_resume: int = 99) -> DayRow:
    high = close if high is None else high
    low = close if low is None else low
    return DayRow(
        date=BASE + i, o=close, h=high, l=low, c=close, v=1_000.0,
        raw_h=high, raw_l=low, raw_c=close, raw_v=1_000.0, amount=close * 1_000.0,
        hit=hit, oneword=oneword, resume=resume, since_resume=since_resume,
    )


def _plain(i: int) -> DayRow:
    """窄箱里的一根：high 10.2 / low 10.0 / close 10.1（振幅 1.98%）。"""
    return _day(i, 10.1, high=10.2, low=10.0)


# 今天最高 10.5 高于前 23 根的 10.2；再往前是 8.0 的老平台（N≥24 的窗口振幅 27% 不过）。
rows_breakout_today = (
    [_day(i, 8.0) for i in range(36)]
    + [_plain(i) for i in range(36, 59)]
    + [_day(59, 10.4, high=10.5, low=10.0)]
)

# 0..18 是 15.0 的老高点（N≥26 的窗口振幅 50% 不过），19..43 是 25 根窄箱，今天收 10.6。
rows_d_fails_longest = (
    [_day(i, 14.9, high=15.0, low=14.8) for i in range(19)]
    + [_plain(i) for i in range(19, 44)]
    + [_day(44, 10.6, high=10.7, low=10.5)]
)

# 最近 12 根内（index 28 = L−11）有一字跌停 → 所有 N≥10 的窗口都含它，整只无箱体。
rows_oneword_down_in_last_12 = (
    [_plain(i) for i in range(28)]
    + [_day(28, 9.0, high=9.0, low=9.0, hit="down", oneword="down")]
    + [_plain(i) for i in range(29, 38)]
    + [_day(38, 10.1, high=10.2, low=10.0)]
)

# 普通触板在 index 14（15 根之前）：N≥15 的窗口含它 → 左端必须晚于它，最长只能 14。
rows_hit_at_minus_15 = (
    [_plain(i) for i in range(14)]
    + [_day(14, 10.0, high=10.6, low=9.9, hit="up")]
    + [_plain(i) for i in range(15, 29)]
    + [_day(29, 10.1, high=10.2, low=10.0)]
)

# 普通触板在 index 24（L−21），一字涨停在 index 25（L−20）：重启只由普通触板触发，
# 所以最长窗口是 19（左端 = 一字涨停那根），不是 18。
rows_oneword_up_at_minus_20 = (
    [_day(i, 9.6, high=9.7, low=9.5) for i in range(23)]
    + [_day(23, 9.09, high=9.2, low=9.0)]
    + [_day(24, 10.0, high=10.3, low=9.9, hit="up")]
    + [_day(25, 11.0, high=11.0, low=11.0, hit="up", oneword="up")]
    + [_day(i, 10.6, high=10.7, low=10.55) for i in range(26, 44)]
    + [_day(44, 10.65, high=10.7, low=10.55)]
)

# 今天一字涨停：走 sealed 独立通道，不进池。
rows_oneword_up_today = [
    _plain(i) for i in range(39)
] + [_day(39, 11.0, high=11.0, low=11.0, hit="up", oneword="up")]

# 8 根前复牌（复牌首日 index 31，今天 index 39 → since_resume = 8 < 10）。
rows_resumed_8_days_ago = (
    [_plain(i) for i in range(31)]
    + [_day(i, 10.1, high=10.2, low=10.0, resume=(i == 31), since_resume=i - 31) for i in range(31, 40)]
)

# 45 根 high == low == close == 10.0：每个 N 的振幅都是 0 → 数值守卫，不能除零。
rows_all_flat_high_eq_low = [_day(i, 10.0) for i in range(45)]


def test_window_excludes_today(MAIN, BOX):
    b = find_box(rows_breakout_today, MAIN, BOX)
    assert isinstance(b, Box)
    assert b.high == max(r.h for r in rows_breakout_today[-24:-1])
    assert b.n == 23


def test_d_checked_inside_loop(MAIN, BOX):
    b = find_box(rows_d_fails_longest, MAIN, BOX)
    assert isinstance(b, Box)
    assert b.n == 25


def test_oneword_down_invalidates_window(MAIN, BOX):
    r = find_box(rows_oneword_down_in_last_12, MAIN, BOX)
    assert isinstance(r, Reject)
    assert r.reason == "oneword_down"


def test_restart_after_limit_hit(MAIN, BOX):
    assert find_box(rows_hit_at_minus_15, MAIN, BOX).n == 14      # 左端 = 触板次日


def test_oneword_up_after_restart(MAIN, BOX):
    assert find_box(rows_oneword_up_at_minus_20, MAIN, BOX).n == 19


def test_oneword_up_today_not_in_pool(MAIN, BOX):
    assert find_box(rows_oneword_up_today, MAIN, BOX).reason == "oneword_up_today"


def test_resume_cooldown(MAIN, BOX):
    assert find_box(rows_resumed_8_days_ago, MAIN, BOX).reason == "resume_cooldown"


def test_zero_range_rejected_not_div_zero(MAIN, BOX):
    # 数值守卫：45 根 high==low 常数 → Reject 而不是 ZeroDivisionError
    assert find_box(rows_all_flat_high_eq_low, MAIN, BOX).reason == "zero_range"


def test_pos_in_box_uses_today_adj_close(MAIN, BOX):
    b = find_box(rows_breakout_today, MAIN, BOX)
    today = rows_breakout_today[-1]
    assert b.pos_in_box == pytest.approx((today.c - b.low) / (b.high - b.low))
    assert b.pos_in_box > 1.0        # 今天收在上沿之上：pos_in_box 允许 > 1（§4.6.1）
