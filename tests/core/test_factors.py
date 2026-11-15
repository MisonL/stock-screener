"""因子原值与 B4/B8/A 组硬门槛（spec §4.2、§4.6.1.1、§4.6.3、§4.6.4、§4.6.5）。

本文件的 `rows_*` 是只造形状的 `list[DayRow]` 常量。基准形态（`_base(...)` 的默认值）：

    0..19   下跌前段 13.0 → 10.0（20 根，量 1.0）
    20..59  下跌末段平在 10.0（40 根，量 1.0）   → 下跌期 L=60、mean_vol=1.0、min_low=10.0
    60..79  横盘 20 根，收 10.0 / 高 10.1 / 低 10.0，量 0.40 → b4 = 0.40，B8 通过
    80      今天 10.0，量 0.40                     → a1 = (13 − 10)/13 = 23%、a2 = a3 = 0

箱体与下跌期分别用 `_window(rows, n)` 从窗口极值 / `decline_window` 现算，不手写 `Box`：
`Box.start`、`Decline.length` 与 `rows` 永远自洽，测试挂了就是实现挂了。

`index_rows` 用 `core.models.Bar`（生产里指数就是从 `daily_bars` 读出来的 Bar 序列），顺带锁住
`compute_factors` 对指数序列只要求 `.date` / `.close` 两个字段。
"""
import pytest

from core.models import Bar, Box, DayRow, Reject
from core.strategy.factors import FactorValues, compute_factors
from core.strategy.window import decline_window

BASE = 20250101
CODE = "sh600519"
INDEX = "sh000300"


# ---------------------------------------------------------------- 造数 helper

def _row(i: int, close: float, *, high: float | None = None, low: float | None = None,
         vol: float = 1.0, open_: float | None = None) -> DayRow:
    high = close if high is None else high
    low = close if low is None else low
    o = close if open_ is None else open_
    return DayRow(
        date=BASE + i, o=o, h=high, l=low, c=close, v=vol,
        raw_h=high, raw_l=low, raw_c=close, raw_v=vol, amount=close * vol,
    )


def _decline(i0: int, *, n_drop: int = 20, n_flat: int = 40, high: float = 13.0,
             low: float = 10.0, vol: float = 1.0, panic_last: int = 0,
             panic_vol: float = 3.0) -> list[DayRow]:
    """下跌期：前 `n_drop` 根从 `high` 线性跌到 `low`，再平走 `n_flat` 根。

    `panic_last` 把**末尾**若干根的量换成 `panic_vol`（§4.6.5 的「恐慌段在末尾」构造：
    只有放在末 60 日截断窗口里，L=60 与 L=120 的 `mean_vol` 才会相同）。
    """
    if n_drop > 1:
        closes = [high - (high - low) * k / (n_drop - 1) for k in range(n_drop)]
    else:
        closes = [high]
    closes += [low] * n_flat
    vols = [vol] * len(closes)
    for k in range(panic_last):
        vols[-1 - k] = panic_vol
    return [_row(i0 + k, c, vol=vols[k]) for k, c in enumerate(closes)]


def _box_rows(i0: int, *, n: int = 20, close: float = 10.0, high: float = 10.1,
              low: float = 10.0, vols: list[float] | None = None,
              vol: float = 0.40) -> list[DayRow]:
    vs = [vol] * n if vols is None else list(vols)
    return [_row(i0 + k, close, high=high, low=low, vol=vs[k]) for k in range(n)]


def _base(*, n_box: int = 20, box_close: float = 10.0, box_high: float = 10.1,
          box_low: float = 10.0, box_vols: list[float] | None = None,
          today_close: float = 10.0, today_high: float = 10.1,
          today_low: float = 10.0, today_vol: float = 0.40,
          n_drop: int = 20, n_flat: int = 40, high: float = 13.0,
          history: int = 0, history_close: float = 10.0,
          panic_last: int = 0) -> list[DayRow]:
    """基准形态（见模块 docstring）；`history` 在下跌期之前垫若干根平走的前史。"""
    rows: list[DayRow] = [_row(k, history_close) for k in range(history)]
    rows += _decline(len(rows), n_drop=n_drop, n_flat=n_flat, high=high,
                     panic_last=panic_last)
    rows += _box_rows(len(rows), n=n_box, close=box_close, high=box_high,
                      low=box_low, vols=box_vols)
    rows.append(_row(len(rows), today_close, high=today_high, low=today_low,
                     vol=today_vol))
    return rows


def _window(rows: list[DayRow], n_box: int = 20) -> tuple[Box, object]:
    """按 `rows` 现算箱体（窗口 = 末 `n_box` 根，不含今天）与共用的下跌期。"""
    start = len(rows) - 1 - n_box
    window = rows[start:-1]
    box = Box(n=n_box, start=start, high=max(r.h for r in window),
              low=min(r.l for r in window), pos_in_box=0.5)
    return box, decline_window(rows, start)


def _index_rows(rows: list[DayRow], *, n: int = 20, ret: float = 0.02) -> list[Bar]:
    """沪深300 序列：日期与个股末 `n` 根对齐，整段涨幅 `ret`（默认 +2%）。"""
    dates = [r.date for r in rows[-n:]]
    out = []
    for k, date in enumerate(dates):
        close = 100.0 * (1.0 + ret) ** (k / max(n - 1, 1))
        out.append(Bar(code=INDEX, date=date, open=close, high=close, low=close,
                       close=close, volume=0.0, amount=0.0, source="tencent"))
    return out


def _run(rows: list[DayRow], p, s, *, n_box: int = 20, index: list[Bar] | None = None):
    box, decl = _window(rows, n_box)
    return compute_factors(rows, box, decl, _index_rows(rows) if index is None else index, p, s)


# ---------------------------------------------------------------- 模块级数据

# 下跌期末段最低 10.00，横盘箱体最低 9.79 → 9.79 <= 10.00 × 0.98 = 9.80 → B8 淘汰。
rows_box_low_below_decline_low = _base(box_low=9.79)

# 横盘期每根都放量（2.0 > 下跌均量 1.0 × 1.2）→ B4 剔除后为空。
rows_all_surge = _base(box_vols=[2.0] * 20)

# 恐慌量固定在下跌期最后 15 天（×3.0），高点日分别距横盘起点 60 / 120 天。
# 两种长度都只取末尾 60 根 → mean_vol == 1.5 → b4 = 0.40 / 1.5 = 0.2667。
rows_high_60d = _base(panic_last=15)
rows_high_120d = _base(n_drop=20, n_flat=100, panic_last=15)


# ---------------------------------------------------------------- B8 / B4

def test_b8_uses_shared_decline(MAIN, S):
    fv = _run(rows_box_low_below_decline_low, MAIN, S)
    assert isinstance(fv, Reject)
    assert fv.stage == "B8"


def test_b4_excludes_surge_days(MAIN, S):
    # 横盘 20 天里 1 天放量（2.0），剔除后均量 0.40；下跌均量 1.0 → b4 == 0.40（数字 F21：比值）
    rows = _base(box_vols=[2.0] + [0.40] * 19)
    fv = _run(rows, MAIN, S)
    assert isinstance(fv, FactorValues)
    assert fv.b4 == pytest.approx(0.40 / 1.0)
    assert fv.box_vol_mean_adj == pytest.approx(0.40)      # 复权均量（股/日），不是比值
    assert fv.b4_degenerate is False


def test_b4_all_surge_is_degenerate_not_crash(MAIN, S):
    fv = _run(rows_all_surge, MAIN, S)
    assert isinstance(fv, FactorValues)
    assert fv.b4_degenerate is True
    assert fv.b4 is None
    # C2 的分母不许是 0：退化时回落到整窗均量，写库前不会被 zero_box_vol 误杀
    assert fv.box_vol_mean_adj == pytest.approx(2.0)


def test_b4_tail_truncation_equivalence(MAIN, S):
    fv_60 = _run(rows_high_60d, MAIN, S)
    fv_120 = _run(rows_high_120d, MAIN, S)
    assert isinstance(fv_60, FactorValues) and isinstance(fv_120, FactorValues)
    assert fv_60.b4 == pytest.approx(0.267, abs=0.002)      # spec §4.6.5 表格 L=60
    assert fv_120.b4 == pytest.approx(0.267, abs=0.002)     # 截断后 L=120 也得 0.267（未截断是 0.320）


def test_d2_first_surge_is_last_three_days(MAIN, S):
    # 放量日恰好 1 天且在窗口最后 3 天内 → D2 给分；放到第一天或出现两次都不给
    last = _run(_base(box_vols=[0.40] * 19 + [2.0]), MAIN, S)
    assert isinstance(last, FactorValues) and last.d2 is True

    first = _run(_base(box_vols=[2.0] + [0.40] * 19), MAIN, S)
    assert isinstance(first, FactorValues) and first.d2 is False

    twice = _run(_base(box_vols=[2.0, 2.0] + [0.40] * 18), MAIN, S)
    assert isinstance(twice, FactorValues) and twice.d2 is False


# ---------------------------------------------------------------- 下跌期长度 / A 组折半

def test_short_decline_discounts_a_group(MAIN, S):
    # L=7：5 <= 7 < 10 → A 组折半标记，但不 Reject（§4.6.5 / §10 B4 (c)）
    fv = _run(_base(history=80, history_close=10.2, n_drop=3, n_flat=4), MAIN, S)
    assert isinstance(fv, FactorValues)
    assert fv.a_group_discounted is True

    # L=4 < discount_min_len(5) → 淘汰
    rej = _run(_base(history=80, history_close=10.2, n_drop=2, n_flat=2), MAIN, S)
    assert isinstance(rej, Reject)
    assert rej.stage == "decline"


def test_zero_decline_volume_rejected(MAIN, S):
    # 下跌期全是零成交 → B4 分母为 0，必须在 decline 阶段挡掉，不能让 b4 除零
    rows = _decline(0, vol=0.0) + _box_rows(60) + [_row(80, 10.0, high=10.1, low=10.0, vol=0.4)]
    assert _run(rows, MAIN, S).stage == "decline"


# ---------------------------------------------------------------- A 组硬门槛

def _flat_ma60_rows(price: float = 25.0, today_close: float | None = None) -> list[DayRow]:
    """MA60 恒为 `price` 的序列，用来压 A2 / A3 的阈值线。

    末 61 根为 `…, price, 今天, 2×price − 今天, price×58, 今天`：60 日窗每滑动一天都恰好
    移出一个「今天」/「2×price − 今天」/平静根，60 个收盘之和恒为 `60 × price` →
    MA60 恒等于 `price`（a3 恰为 0）。`today_close = price × 1.08` 时 a2 = 2/25 = 0.08 压线。
    首根 high = price / 0.75 → a1 = 0.25；横盘期量 0.40 → b4 = 0.40。
    """
    today = price * 1.08 if today_close is None else today_close
    closes = [price] * 100
    closes[39] = today                         # = closes[-61]
    closes[40] = 2.0 * price - today           # = closes[-60]
    closes[99] = today                         # 今天
    rows = [_row(i, c, vol=1.0 if i < 79 else 0.40) for i, c in enumerate(closes)]
    rows[0] = _row(0, price, high=price / 0.75, vol=1.0)   # 近 250 日高点
    return rows


def _uptrend_rows() -> list[DayRow]:
    """MA60 缓慢上行（≈ +0.015%/日 > 0）的序列，用来撞 a3 的「斜率 ≤ 0」门槛。

    100 根 9.85 → 10.00 线性上行（前 80 根量 1.0、横盘 20 根量 0.40），首根 high 13.0。
    """
    n = 100
    closes = [9.85 + 0.15 * k / (n - 1) for k in range(n)]
    rows = [_row(i, c, vol=1.0 if i < 80 else 0.40) for i, c in enumerate(closes)]
    rows[0] = _row(0, closes[0], high=13.0)
    return rows


def _drawdown_rows(a1: float) -> list[DayRow]:
    """只把 a1 摆到 `a1`（其余因子取基准形态的过线值）。"""
    today = 10.0
    return _base(high=today / (1.0 - a1), today_close=today)


def _a_rows(a1: float, a2: float, a3: float, p) -> list[DayRow]:
    """按「哪一项越界」造数据：返回的序列里只有那一项（刻意）不过线，另两项是过线的值。"""
    if a2 > float(p.a2_T):                      # a2 越界：MA60 恒 25，今天 25×(1+a2)
        return _flat_ma60_rows(25.0, 25.0 * (1.0 + a2))
    if a3 > 0:                                  # a3 越界：MA60 缓慢上行
        return _uptrend_rows()
    if a3 == 0:                                 # a3 压线：MA60 恒 10.0 → 斜率恰为 0
        return _flat_ma60_rows(10.0, 10.0)
    return _drawdown_rows(a1)


@pytest.mark.parametrize("a1,a2,a3", [(0.1999, 0.05, -0.001),
                                      (0.25, 0.0801, -0.001),
                                      (0.25, 0.05, +0.0001)])
def test_a_gate_requires_all_three(a1, a2, a3, MAIN, S):
    fv = _run(_a_rows(a1, a2, a3, MAIN), MAIN, S)
    assert isinstance(fv, Reject)
    assert fv.stage == "A"


@pytest.mark.parametrize("a1,a2,a3", [(0.2000, 0.05, -0.001),
                                      (0.25, 0.0800, -0.001),
                                      (0.25, 0.05, 0.0)])
def test_a_gate_boundaries_pass(a1, a2, a3, MAIN, S):
    fv = _run(_a_rows(a1, a2, a3, MAIN), MAIN, S)
    assert isinstance(fv, FactorValues), fv
    assert fv.a1 >= float(MAIN.a1[0])
    assert fv.a2 <= float(MAIN.a2_T)
    assert fv.a3 <= 0


# ---------------------------------------------------------------- as-of 口径

def test_box_factors_exclude_today(MAIN, S):
    # 今天巨量阳线（+2%、100 倍量）不得改变箱体类因子 b3/b4/b6/b9
    # 用「陡跌进箱体」的形态（下跌期不留平段），MA60 才有下行斜率吸收今天这一跳
    steep = dict(n_drop=60, n_flat=0)
    quiet = _run(_base(**steep), MAIN, S)
    loud = _run(_base(**steep, today_close=10.2, today_high=10.3, today_vol=100.0), MAIN, S)
    assert isinstance(quiet, FactorValues) and isinstance(loud, FactorValues)
    assert (loud.b3, loud.b4, loud.b6, loud.b9) == (quiet.b3, quiet.b4, quiet.b6, quiet.b9)
    # 区间类相反：今天的收盘必须进 B7（含今天）
    assert loud.b7 > quiet.b7


def test_box_factors_values(MAIN, S):
    fv = _run(_base(), MAIN, S)
    assert isinstance(fv, FactorValues)
    # 箱体 10.0–10.1 → B2 = 0.01；横盘 20 根收盘恒 10.0，近 3 根收盘也恒 10.0 → B3 = B6 = B9 = 0
    assert fv.b2 == pytest.approx(0.01)
    assert fv.b3 == pytest.approx(0.0)
    assert fv.b6 == pytest.approx(0.0)
    assert fv.b9 == pytest.approx(0.0)
    assert 0.0 <= fv.b5 <= 100.0                    # 百分位，不是比例
    assert fv.a1 == pytest.approx((13.0 - 10.0) / 13.0)
    assert fv.a2 == pytest.approx(0.0)
    assert fv.a3 < 0                                # 13.0 那一段滑出 MA60 窗口 → 均线仍在下行
    assert fv.d3 is False                           # 平走箱体里没有 MACD 金叉
    assert fv.a_group_discounted is False


# ---------------------------------------------------------------- D1 相对强度

def test_d1_is_stock_minus_index(MAIN, S):
    # 个股末 20 个交易日 +2%（今天 10.0 × 1.02），沪深300 整段 0% → d1 = +2%
    rows = _base(today_close=10.2, today_high=10.3)
    fv = _run(rows, MAIN, S, index=_index_rows(rows, ret=0.0))
    assert isinstance(fv, FactorValues)
    assert fv.d1 == pytest.approx(0.02, abs=1e-4)


def test_d1_none_when_too_few_bars(MAIN, S):
    # 指数窗口只有 10 根 < d1_min_bars(15) → D1 缺失（打分按 0 分，由 Task 17 处理）
    rows = _base()
    fv = _run(rows, MAIN, S, index=_index_rows(rows, n=10))
    assert isinstance(fv, FactorValues)
    assert fv.d1 is None


def test_d1_uses_common_start_date(MAIN, S):
    """个股首根晚于指数首根（停牌/上市）时，两条腿必须用同一对起止日期。

    指数 20 根 100→119（+19%），个股只有后 19 根且同比例（101→119，也是 +19%）：
    用共同起止日 → D1 = 0；各用各的首根 → 个股 +17.8%、指数 +19% → D1 被算成 -1.2%。
    """
    from core.strategy.factors import _rel_strength

    days = list(range(BASE, BASE + 20))
    index_rows = [Bar(INDEX, d, 100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i, 0.0, 0.0, "x")
                  for i, d in enumerate(days)]
    stock = [_row(i, 100.0 + i) for i in range(1, 20)]        # 缺 i=0 那根
    assert _rel_strength(stock, index_rows, S) == pytest.approx(0.0, abs=1e-9)
