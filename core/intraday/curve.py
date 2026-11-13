"""分时量能曲线、全天量折算与 C2 时段阈值（spec §4.6.8）。

A 股成交量是 U 型分布：10:30 只过了 60 分钟，累计量通常已占全天的 30–35%，不是线性的 25%。
所以「当前累计量 × 240 / 已交易分钟数」的线性折算会**高估**早盘量能、制造假突破信号。
正确口径是 `折算全天量 = 当前累计量 / 该时刻的累计占比`，占比查本模块的曲线。

三条写死的实现口径（§4.6.8）：

1. **曲线只有 9 个节点，盘中 47 个 5 分钟时刻里 38 个必须按「已交易分钟数」插值**，
   绝不可以用 `dict.get(hm, 常数)` 兜底——常数值接近早盘真值、远离午后真值，
   实测会把午后 C2 高估最多 3.26×。
2. **11:30–13:00 午休不推进曲线**：`traded_minutes` 在午休恒为 120，插值在数学上自然
   给出 11:30 的值（0.59），不前进也不后退，无需为午休写特判。
3. **14:57 起冻结量能比**：14:57 之后的累计量不含收盘集合竞价那三分钟，而曲线在 15:00
   处是 100%，继续折算会系统性低估。冻结由 `VolumeFreezer` 承担，`c2_threshold` 的阈值
   **不变**（仍是常规的 c2，不要切回早盘那档 c2_strict）。
"""
from __future__ import annotations

import functools
from datetime import time
from pathlib import Path

from core.clock import phase, traded_minutes
from core.config import load_config


def _as_time(hm: str) -> time:
    return time.fromisoformat(hm)


def _nodes(curve) -> list[tuple[int, float]]:
    """曲线节点 → `[(已交易分钟数, 占比)]`，按分钟升序。

    节点之间按 `traded_minutes` 单调递增（09:45=15 … 11:30=120 … 13:30=150 … 15:00=240），
    午休 90 分钟被扣除，所以 11:30→13:30 的插值分母是 30 分钟而不是 120 分钟。
    """
    return sorted((traded_minutes(_as_time(hm)), float(v)) for hm, v in curve.items())


def curve_at(hm: str, curve: dict[str, float]) -> float | None:
    """`hm` 时刻的累计量占比；首个节点（09:45）之前返回 `None`，15:00 之后封顶为末节点值（1.0）。

    `curve` 是 `Config.strategy.intraday.curve`（9 个节点）。边界由曲线自身的首末节点决定，
    不在代码里重复写死 09:45 / 15:00——首节点之前不判 C/C′ 的规则与 `judge_start` 同源。
    """
    t = traded_minutes(_as_time(hm))
    nodes = _nodes(curve)
    if t < nodes[0][0]:
        return None
    for (ta, va), (tb, vb) in zip(nodes, nodes[1:]):
        if ta <= t <= tb:
            if t == ta or tb == ta:
                return va
            return va + (vb - va) * (t - ta) / (tb - ta)
    return nodes[-1][1]


def projected_full_day(cum_vol: float, hm: str, curve: dict[str, float]) -> float | None:
    """按 `hm` 的累计占比把当前累计量折算成全天量；09:45 前无占比，返回 `None`（不判 C2）。"""
    ratio = curve_at(hm, curve)
    if ratio is None or ratio <= 0:
        return None
    return cum_vol / ratio


def c2_threshold(hm: str, s) -> float | None:
    """`hm` 时刻的 C2 量能比阈值（`s` = `Config.strategy`；`None` = 该时刻不判 C/C′）。

    **午休优先判**：`11:30 ≤ hm < 13:00` 直接 `None`，不看后面的区间。区间必须拆成
    `[10:00, 11:30)` 与 `[13:00, 14:57)` 两段——写成 `[10:00, 14:57)` 会把午休判成正常
    交易时段并返回 1.5，与「午休不判定」自相矛盾。

    - `< judge_start`（09:45）→ `None`（开盘集合竞价让量能失真）
    - `[judge_start, strict_until)`（09:45–10:00）→ `c2_strict`
    - `[strict_until, freeze_at)` 与 `≥ freeze_at`（含 14:57–15:00）→ `c2`

    14:57 起冻结的是**量能比**（`VolumeFreezer`），不是阈值，所以这里仍返回常规 c2。
    """
    p = s.intraday
    t = _as_time(hm)
    if phase(t) == "lunch":
        return None
    if t < _as_time(p.judge_start):
        return None
    if t < _as_time(p.strict_until):
        return p.c2_strict
    return p.c2


@functools.lru_cache(maxsize=1)
def _default_freeze_at() -> str:
    """`config/strategy.yaml` 的 `intraday.freeze_at`（14:57）。

    `VolumeFreezer()` 的签名是计划定死的（测试零参构造），冻结时刻在进程内读一次配置，
    测试要用别的时点就把 `freeze_at=` 传进去。
    """
    return load_config(Path("config")).strategy.intraday.freeze_at


class VolumeFreezer:
    """按票冻结量能比（§4.6.8）：收盘竞价起不再重算，只用价格确认。

    14:57 那一刻的折算值就是当天的最终量能比。14:57 前没有任何值时取最近一次有效 ratio
    （同一只票 14:57 之后才第一次被调用时用得上），都没有才用当次传入的 `ratio_now`。
    """

    def __init__(self, freeze_at: str | None = None):
        self._freeze = _as_time(freeze_at or _default_freeze_at())
        self._frozen: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def ratio(self, code: str, hm: str, ratio_now: float) -> float:
        t = _as_time(hm)
        if t < self._freeze:
            self._last[code] = ratio_now
            return ratio_now
        frozen = self._frozen.get(code)
        if frozen is not None:
            return frozen
        # 冻结时刻当次就用传入值；晚于冻结时刻且此前无值，才回落到最近一次有效 ratio。
        value = ratio_now if t == self._freeze else self._last.get(code, ratio_now)
        self._frozen[code] = value
        return value
