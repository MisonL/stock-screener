"""bar 标注与共用下跌期窗口（spec §4.6.6 末尾红框与 §4.6.5）。

`rows_double_top / rows_high_in_box / rows_long_decline` 是三个只造形状的 `list[DayRow]`
常量（价格取整、四价相同），下面 5 个用例直接吃它们。
"""
import pytest

from core.models import Bar, DayRow
from core.strategy.window import annotate, decline_window

BASE = 20250101
CODE = "sh600519"


def _day(i: int, close: float, vol: float = 1_000.0, resume: bool = False,
         since_resume: int = 99, oneword: str | None = None) -> DayRow:
    return DayRow(
        date=BASE + i, o=close, h=close, l=close, c=close, v=vol,
        raw_h=close, raw_l=close, raw_c=close, raw_v=vol, amount=close * vol,
        oneword=oneword, resume=resume, since_resume=since_resume,
    )


def _series(highs: list[float]) -> list[DayRow]:
    return [_day(i, h) for i, h in enumerate(highs)]


# 双顶：250 根里第 100、180 根同为最高（其余 20.0）。
rows_double_top = _series([25.0 if i in (100, 180) else 20.0 for i in range(250)])
# 最高点落在 box_start(230) 之后：全量复测里 27 只的形态。
rows_high_in_box = _series([30.0 if i == 235 else 20.0 for i in range(250)])
# 高点在第 80 根（30.0），之后单边下跌 170 根。
rows_long_decline = _series([20.0] * 80 + [30.0 - 0.1 * k for k in range(170)])


def _bar(date: int, high: float, vol: float = 1_000.0, close: float | None = None,
         low: float | None = None) -> Bar:
    c = high if close is None else close
    return Bar(code=CODE, date=date, open=c, high=high, low=c if low is None else low,
               close=c, volume=vol, amount=c * vol, source="tencent")


def test_tie_takes_last_high():
    # 双顶：第 100 根与第 180 根同为 250 日最高 → 起点取第二个顶
    assert decline_window(rows_double_top, box_start=220).start == 180


def test_high_inside_box_is_none():
    # 250 日高点就在箱体内 → L ≤ 0 → 没有「跌下来」这一段
    assert decline_window(rows_high_in_box, box_start=230) is None


def test_tail_truncated_to_60():
    d = decline_window(rows_long_decline, box_start=200)      # 高点在 80，L=120
    assert (d.length, d.tail_start) == (120, 140)
    assert d.mean_vol == pytest.approx(1_000.0)
    assert d.min_low == pytest.approx(rows_long_decline[199].l)


def test_resume_days_excluded_from_mean():
    vols = [1_000.0] * 25
    vols[5] = 1_000_000.0                     # 复牌首日巨量：一次性异常，不进分母
    for i in range(6, 11):
        vols[i] = 5_000.0                     # 复牌后 5 个交易日换手偏高，同样不进分母
    highs = [10.0 + 0.01 * i for i in range(25)]
    highs[2] = 30.0                           # 250 日高点：第 2 根
    raw = [_bar(BASE + i, highs[i], vols[i]) for i in range(25)]
    adj = [_bar(BASE + i, highs[i], vols[i]) for i in range(25)]

    rows = annotate(raw, adj, "cn_main", lambda _d: "normal", {}, {BASE + 5})
    assert [r.since_resume for r in rows[5:7]] == [0, 1]
    assert rows[5].resume is True and rows[12].resume is False
    assert (rows[5].since_resume, rows[12].since_resume) == (0, 7)

    d = decline_window(rows, box_start=20)
    # tail = rows[2:20]（18 根）；剔掉复牌首日 5 与其后 5 日（6..10）→ 12 根 × 1000
    assert (d.length, d.tail_start) == (18, 2)
    assert d.mean_vol == pytest.approx(1_000.0)


def test_exdate_limit_uses_exref():
    # 10 送 10：除权日参考价 = 10.00/2 = 5.00，当日一字跌停收 4.50
    raw = [_bar(BASE, 10.00), _bar(BASE + 1, 4.50, close=4.50)]
    adj = [_bar(BASE, 10.00), _bar(BASE + 1, 9.00, close=9.00)]    # 复权价 4.50 × 2

    rows = annotate(raw, adj, "cn_main", lambda _d: "normal", {BASE + 1: 5.00}, set())
    assert (rows[1].raw_c, rows[1].c) == (4.50, 9.00)              # 复权值给因子，raw 给触板
    assert (rows[1].hit, rows[1].oneword) == ("down", "down")

    # 不给参考价时按上一根 raw 收盘 10.00 算，跌停价 9.00 → 4.50 不算触板
    rows = annotate(raw, adj, "cn_main", lambda _d: "normal", {}, set())
    assert rows[1].hit is None
