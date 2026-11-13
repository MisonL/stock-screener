"""因子原值与硬门槛（spec §4.2、§4.6.1.1、§4.6.3、§4.6.4、§4.6.5）。

`compute_factors` 是纯函数：吃已复权的 `DayRow` 序列、已选定的 `Box`、共用的 `Decline`
与沪深300 的 bar 序列，吐 `FactorValues`（打分用）或 `Reject`（淘汰用）。

**as-of 口径（§4.6.1.1）**：箱体类（B2/B3/B4/B6/B9/D2/D3）只用 T−1 及之前；区间类
（A1/A2/A3/B5/B7/D1）含今天。D1 是区间类——「近 20 个交易日相对表现」是「以现在的位置
看历史」，不含今天等于用昨天的位置给今天的票打分。

**命名（数字 F16/F20，三者不可混用）**：
- `b4` 是**比值**（横盘期剔除放量日后的均量 ÷ `decl.mean_vol`），与展示名「缩量」对应；
- `box_vol_mean_adj` 是**复权均量（股/日）**，就是 C2 的分母（§4.6.4「同源必须是同一个数」），
  写库时除以 `vol_factor_T` 才是 `pool.box_vol_mean`；
- 事件 payload 的 `box_vol_multiple` 是倍数（`projected_full_day / box_vol_mean`），不在这里算。

**硬门槛顺序（先命中先 `Reject`）**：`decline → B8 → B4 → A`。decline 必须排最前——
`decl is None` 时先算 B8 会 `AttributeError`；`decl.mean_vol <= 0` 也在这里挡掉，否则 B4 除零。
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

import numpy as np

from core.indicators import atr_wilder, cv, golden_cross, macd, rank_pct, sma, slope_pct
from core.models import Box, DayRow, Decline, Reject

# 均线周期（§4.6.1.1：全部 SMA）。这几个不是「可调阈值」，是因子定义的一部分，不进 YAML。
_MA_FAST = 5
_MA_MID = 10
_MA_SLOW = 20
_MA60 = 60
_A3_DAYS = 20        # A3 = MA60 的 20 日斜率
_B9_DAYS = 3         # B9 = 最近 3 根收盘的变异系数


@dataclass(frozen=True)
class FactorValues:
    """一只票在某一天的全部因子原值（不含达成度，打分在 `core.strategy.scoring`）。

    `b4 is None` 只在 `b4_degenerate`（横盘期全是放量日）时出现：B4 达成度记 0，但该票仍可入池
    （§4.2 B 组 2026-10-29 补）。`d1 is None` 表示指数或个股的窗口内有效 bar 不足
    `mapping.d1_min_bars`，打分按 0 分处理。
    """

    a1: float
    a2: float
    a3: float
    b2: float
    b3: float
    b4: float | None
    b5: float
    b6: float
    b7: float
    b9: float
    d1: float | None
    d2: bool
    d3: bool
    box_vol_mean_adj: float
    a_group_discounted: bool
    b4_degenerate: bool


def compute_factors(
    rows: list[DayRow],
    box: Box,
    decl: Decline | None,
    index_rows,
    p,
    s,
) -> FactorValues | Reject:
    """因子原值 + B4/B8/A 组硬门槛（spec §4.2、§4.6.4、§4.6.5）。

    `p` 是 `Config.market(m)`（`b8_buffer` / `a1` / `a2_T`），`s` 是 `Config.strategy`
    （`decline.*` / `mapping.*`）。`index_rows` 是沪深300 的 bar 序列（`.date` + `.close`，
    接受 `Bar` 或 `DayRow`）。
    """
    # 1. 下跌期门槛：必须最先判（B8 要读 decl.min_low；B4 要除 decl.mean_vol）
    if decl is None:
        return Reject("decline", "no_decline")
    if decl.length < int(s.decline.discount_min_len):
        return Reject("decline", "too_short")
    if decl.oneword_down >= int(s.decline.oneword_down_reject_at):
        return Reject("decline", "oneword_down")
    if decl.mean_vol <= 0:
        return Reject("decline", "zero_vol")

    # 2. 5 <= L < 10：A 组达成度折半，但不淘汰（§4.6.5）
    discounted = decl.length < int(s.decline.min_len)

    # 3. B8 不创前期新低：横盘期最低 > 下跌期最低 × b8_buffer
    window = rows[box.start : box.start + box.n]
    if min(r.l for r in window) <= decl.min_low * float(p.b8_buffer):
        return Reject("B8", "new_low")

    # 4. B4 缩量：剔除放量日后的横盘均量 ÷ 下跌期均量
    surge_at = decl.mean_vol * float(s.mapping.surge_ratio)
    quiet = [r.v for r in window if r.v <= surge_at]
    if quiet:
        box_vol_mean_adj = sum(quiet) / len(quiet)
        b4: float | None = box_vol_mean_adj / decl.mean_vol
        degenerate = False
    else:
        # 横盘期全是放量日：B4 记 0 分但仍可入池；C2 的分母回落到整窗均量（不能是 0）
        box_vol_mean_adj = sum(r.v for r in window) / len(window)
        b4 = None
        degenerate = True
    if b4 is not None and b4 > float(s.mapping.b4[-1]):
        return Reject("B4", "vol_not_dry")

    # 5. A 组门槛（§4.2 红框：三项全过才入池）
    if len(rows) < _MA60 + _A3_DAYS - 1:
        return Reject("A", "warmup")
    a_group = _a_group(rows, s)
    if a_group is None:
        return Reject("A", "bad_price")          # 非正的极值/均线：除零守卫，不兜底
    a1, a2, a3 = a_group
    if a1 < float(p.a1[0]):
        return Reject("A", "drawdown")
    if a2 > float(p.a2_T):
        return Reject("A", "below_ma")
    if a3 > 0:
        return Reject("A", "ma_trend")

    return FactorValues(
        a1=a1,
        a2=a2,
        a3=a3,
        b2=(box.high - box.low) / box.low,
        b3=_small_body(rows, box),
        b4=b4,
        b5=_atr_percentile(rows, s),
        b6=_ma_converge(rows),
        b7=_price_position(rows, s),
        b9=_three_flat(rows),
        d1=_rel_strength(rows, index_rows, s),
        d2=_first_surge(window, surge_at, int(s.mapping.d2_last_days)),
        d3=_macd_cross(rows, box, s),
        box_vol_mean_adj=box_vol_mean_adj,
        a_group_discounted=discounted,
        b4_degenerate=degenerate,
    )


# ---------------------------------------------------------------- A 组（含今天）

def _a_group(rows: list[DayRow], s) -> tuple[float, float, float] | None:
    """`(a1, a2, a3)`：回撤 / 距 MA60 / MA60 的 20 日斜率（%/日）。

    极值或均线非正时返回 `None`（调用方 `Reject("A", "bad_price")`）：脏数据不该让
    整池的因子计算抛 `ZeroDivisionError`，也不该兜成满分。
    """
    closes = np.array([r.c for r in rows], dtype=np.float64)
    ma60 = sma(closes, _MA60)
    today = float(closes[-1])
    m60 = float(ma60[-1])
    peak = max(r.h for r in rows[-int(s.mapping.pct_window) :])
    if peak <= 0 or today <= 0 or m60 <= 0:
        return None

    # A1：从近 250 日最高价回撤（用 (peak − close)/peak 而不是 1 − close/peak，
    # 后者在「正好 20%」时算成 0.19999999999999998，压线票会被门槛误杀）
    a1 = (peak - today) / peak

    # A2：到 MA60 的距离，不带方向（「横久了刚站上 MA60」正是最理想的一类）
    a2 = abs(today - m60) / m60

    # A3：MA60 的 20 日 OLS 斜率，分母是 mean(MA60) 而不是 mean(close)（§4.6.1.1）
    valid = ma60[~np.isnan(ma60)][-_A3_DAYS:]
    a3 = slope_pct(valid)
    return a1, a2, a3


# ---------------------------------------------------------------- B 组

def _small_body(rows: list[DayRow], box: Box) -> float:
    """B3 = mean(|c_t − c_{t−1}| / c_{t−1})，前收作分母（跳空不算实体幅度）。

    窗口首根的前收取窗口外那一根（§4.6.1「窗口 = rows[start:-1]」）；序列从首根开始时
    没有前收，跳过该根。
    """
    moves = []
    for i in range(box.start, box.start + box.n):
        if i == 0:
            continue
        prev = rows[i - 1].c
        if prev > 0:
            moves.append(abs(rows[i].c - prev) / prev)
    return sum(moves) / len(moves) if moves else 0.0


def _atr_percentile(rows: list[DayRow], s) -> float:
    """B5 = ATR(14)/close 在近 250 日（含今天）的平均秩百分位。"""
    closes = np.array([r.c for r in rows], dtype=np.float64)
    highs = np.array([r.h for r in rows], dtype=np.float64)
    lows = np.array([r.l for r in rows], dtype=np.float64)
    atr = atr_wilder(highs, lows, closes, int(s.mapping.atr_n))
    ratio = atr / closes
    valid = ratio[~np.isnan(ratio)][-int(s.mapping.pct_window) :]
    if valid.size == 0:
        return 0.0
    return rank_pct(valid, valid[-1])


def _ma_converge(rows: list[DayRow]) -> float:
    """B6 = (max − min) / mean of MA5/MA10/MA20，取 T−1（箱体类口径）。"""
    closes = np.array([r.c for r in rows], dtype=np.float64)
    i1 = len(rows) - 2
    mas = [
        float(sma(closes, period)[i1])
        for period in (_MA_FAST, _MA_MID, _MA_SLOW)
    ]
    return (max(mas) - min(mas)) / mean(mas)


def _price_position(rows: list[DayRow], s) -> float:
    """B7 = 今天收盘在近 250 日 [最低, 最高] 区间里的位置（百分数，含今天）。"""
    window = rows[-int(s.mapping.pct_window) :]
    low = min(r.l for r in window)
    high = max(r.h for r in window)
    if high <= low:
        return 0.0
    return (rows[-1].c - low) / (high - low) * 100.0


def _three_flat(rows: list[DayRow]) -> float:
    """B9 = 最近 3 根收盘的变异系数，取 T−1（样本标准差 ddof=1，n=3 时与 ddof=0 差 22.5%）。"""
    closes = [r.c for r in rows[-(_B9_DAYS + 1) : -1]]
    return cv(closes)


# ---------------------------------------------------------------- D 组

def _rel_strength(rows: list[DayRow], index_rows, s) -> float | None:
    """D1 = 个股 − 沪深300 的近 `d1_window` 个交易日涨幅（按日期 join，含今天）。

    交易日集合以指数为准；个股停牌日跳过、不补 0。窗口内个股有效 bar 少于
    `d1_min_bars` → `None`（打分按 0 分，并标注缺失）。
    """
    window_n = int(s.mapping.d1_window)
    min_bars = int(s.mapping.d1_min_bars)
    index = [(_date(r), _close(r)) for r in index_rows][-window_n:]
    if len(index) < min_bars or index[0][1] <= 0:
        return None

    dates = {date for date, _ in index}
    stock = [(r.date, r.c) for r in rows if r.date in dates]
    if len(stock) < min_bars or stock[0][1] <= 0:
        return None

    # 两条腿必须用**同一对起止日期**：个股首根晚于指数首根时（停牌/上市），
    # 若各用各的首根，比较的就是两段不同的行情，差值不再代表相对强弱。
    stock_map = dict(stock)
    common = [d for d, _ in index if d in stock_map]
    if len(common) < min_bars:
        return None
    first, last = common[0], common[-1]
    index_map = dict(index)
    stock_ret = stock_map[last] / stock_map[first] - 1.0
    index_ret = index_map[last] / index_map[first] - 1.0
    return stock_ret - index_ret


def _first_surge(window: list[DayRow], surge_at: float, last_days: int) -> bool:
    """D2 = 放量日（v > 下跌均量 × surge_ratio）恰好 1 天，且在窗口最后 `last_days` 根内。"""
    hits = [i for i, r in enumerate(window) if r.v > surge_at]
    return len(hits) == 1 and hits[0] >= len(window) - last_days


def _macd_cross(rows: list[DayRow], box: Box, s) -> bool:
    """D3 = MACD 在箱体窗口 [T−N*, T−1] 内金叉（DIF 上穿 DEA），预热 `macd_warmup` 根。"""
    closes = np.array([r.c for r in rows], dtype=np.float64)
    fast, slow, signal = (int(x) for x in s.mapping.macd)
    dif, dea, _ = macd(closes, fast, slow, signal)
    i0 = max(box.start, int(s.mapping.macd_warmup))
    i1 = len(rows) - 2
    if i0 >= i1:
        return False
    return golden_cross(dif, dea, i0, i1)


# ---------------------------------------------------------------- 序列适配

def _date(row) -> int:
    return int(row.date)


def _close(row) -> float:
    """`Bar` 用 `.close`，`DayRow` 用 `.c`——指数序列两种都可能传进来。"""
    close = getattr(row, "c", None)
    return float(row.close if close is None else close)
