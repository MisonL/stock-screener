"""分段线性映射与打分（spec §4.3、§4.6.3、§4.6.3.1、§4.6.5、§4.6.7）。

`score` 把 `FactorValues`（因子原值）按 §4.6.3 的权威映射表转成 13 个分项得分，
**总分 = 分项得分之和**（§4.3：权重合计 100，故 score ∈ [0,100]）。

**阈值来源**：因子阈值（`T` / `S` / `L` / 钟形边界）一律由调用方传入——
市场档位来自 `p`（`Config.market(m)`），全局映射参数来自 `s`（`Config.strategy`）。
本模块只持有**映射形状**（阈值处 0.6、饱和 1.0、A 组折半 0.5），这些是 §4.6.3 的
定义而不是可调阈值。`b4_map` 的 `points` 默认值同样读 YAML，不写常量。

**已知退化**：`b4_degenerate`（横盘期全是放量日）时 B4 达成度 0（不是 0.6）；
`d1 is None`（窗口内有效 bar 不足）时 D1 记 0（§4.6.3「按 0 分处理并标注」）。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from core.strategy.factors import FactorValues

# §4.6.3 的统一映射形状：阈值是「及格线」，不是「满分线」。
PASS = 0.6              # 值 == 阈值时的达成度
A1_FLOOR = 0.3          # 回撤 ≥ 原始上界之后降权到的常数（不淘汰）
# §4.6.5：下跌期 5 ≤ L < 10 时 A 组三项达成度折半（仍可入池）。
A_GROUP_DISCOUNT = 0.5

_FACTORS = ("A1", "A2", "A3", "B2", "B3", "B4", "B5", "B6", "B7", "B9", "D1", "D2", "D3")
_A_GROUP = ("A1", "A2", "A3")


# ---------------------------------------------------------------- 通用映射

def smaller_better(v: float, T: float) -> float:
    """「越小越好」：`v ≤ T/2` → 1.0；`v == T` → 0.6；`v ≥ 1.5T` → 0.0，中间线性。"""
    if T <= 0:
        raise ValueError("smaller_better 的阈值 T 必须为正；T=0 的退化因子用专用映射")
    if v <= T / 2:
        return 1.0
    if v >= T * 1.5:
        return 0.0
    if v <= T:
        return 1.0 - (1.0 - PASS) * (v - T / 2) / (T / 2)
    return PASS * (T * 1.5 - v) / (T * 1.5 - T)


def larger_better(v: float, S: float, L: float = 0.0) -> float:
    """「越大越好」：`v ≥ S` → 1.0；`v == L` → 0.6；`v ≤ 0.6L` → 0.0，中间线性。

    `L == 0` 时退化（0 既算 0.6 又算 0.0），下界改取 `−S`：`v ≤ −S` → 0.0、
    `v == 0` → 0.6、`v ≥ S` → 1.0（§4.6.3 的 D1 规则）。
    """
    if S <= 0:
        raise ValueError("larger_better 的饱和点 S 必须为正")
    if v >= S:
        return 1.0
    if L == 0:
        if v <= -S:
            return 0.0
        if v >= 0:
            return PASS + (1.0 - PASS) * v / S
        return PASS * (v + S) / S
    if v <= L * 0.6:
        return 0.0
    if v <= L:
        return PASS * (v - L * 0.6) / (L * 0.4)
    return PASS + (1.0 - PASS) * (v - L) / (S - L)


# ---------------------------------------------------------------- 专用映射

def a1_bell(dd: float, bounds: Sequence[float]) -> float:
    """A1 钟形（§4.6.3）：边界 `[L, S_lo, S_hi, H]`（默认 20/35/45/60%）。

    `[0, L]` 线性 0 → 0.6、`[L, S_lo]` 0.6 → 1.0、`[S_lo, S_hi]` 平台 1.0、
    `[S_hi, H]` 1.0 → 0.3、`≥ H` 常数 0.3（基本面崩坏嫌疑，不淘汰只降权）。
    """
    low, full_lo, full_hi, high = (float(x) for x in bounds)
    if not 0 < low < full_lo < full_hi < high:
        raise ValueError(f"a1 边界必须递增且为正: {tuple(bounds)!r}")
    if dd <= 0:
        return 0.0
    if dd <= low:
        return PASS * dd / low
    if dd <= full_lo:
        return PASS + (1.0 - PASS) * (dd - low) / (full_lo - low)
    if dd <= full_hi:
        return 1.0
    if dd <= high:
        return 1.0 - (1.0 - A1_FLOOR) * (dd - full_hi) / (high - full_hi)
    return A1_FLOOR


def a3_map(slope: float, zero_at: float) -> float:
    """A3（MA60 斜率，T=0 的退化）：`≤ 0` → 1.0、`≥ zero_at` → 0.0、中间线性。"""
    if zero_at <= 0:
        raise ValueError("a3 的归零点必须为正")
    if slope <= 0:
        return 1.0
    if slope >= zero_at:
        return 0.0
    return 1.0 - slope / zero_at


def b4_map(v: float, points: Sequence[float] | None = None) -> float:
    """B4（§4.6.3 的三点映射）：`points = (饱和, 及格, 归零)`，默认取 YAML 的 `mapping.b4`。

    不套用通用 `1.5T` 段——`0.75` 恰好是硬淘汰点，与归零点对齐才没有断崖。
    """
    sat, mid, zero = (float(x) for x in (_default_b4_points() if points is None else points))
    if not 0 < sat < mid < zero:
        raise ValueError(f"b4 三点必须递增且为正: {(sat, mid, zero)!r}")
    if v <= sat:
        return 1.0
    if v >= zero:
        return 0.0
    if v <= mid:
        return 1.0 - (1.0 - PASS) * (v - sat) / (mid - sat)
    return PASS * (zero - v) / (zero - mid)


@lru_cache(maxsize=1)
def _default_b4_points() -> tuple[float, ...]:
    """`b4_map` 的独立调用（测试 / 脚本）用 YAML 里的三点，不在代码里写数字。

    `score` 永远显式传 `s.mapping.b4`，此默认路径不参与生产打分。
    """
    from core.config import load_config

    return tuple(load_config(Path("config")).strategy.mapping.b4)


# ---------------------------------------------------------------- 打分

@dataclass(frozen=True)
class ScoreResult:
    """打分结果。`items` 的键是因子编号（大写），值是 `(原值, 达成度, 得分)`。

    总分 = `items` 里各分项得分之和（展示层逐项取整时不会与总分对不上）。
    `raw` 在 `b4 is None` / `d1 is None` 时是 `None`。
    """

    score: float
    items: dict[str, tuple[float | bool | None, float, float]]


def score(fv: FactorValues, p, s) -> ScoreResult:
    """按 §4.6.3 打总分。

    `p` 是 `Config.market(m)`（`b2_T` / `b3_T` / `a1` / `a2_T` / `a3_zero` / `b6_T` / `b9_T`），
    `s` 是 `Config.strategy`（`weights` / `mapping`）。`a_group_discounted` 时 A 组三项达成度
    ×0.5（§4.6.5）；`b4_degenerate` 时 B4 记 0；`d1 is None` 时 D1 记 0。
    """
    m = s.mapping
    ach = {
        "A1": a1_bell(fv.a1, p.a1),
        "A2": smaller_better(fv.a2, float(p.a2_T)),
        "A3": a3_map(fv.a3, float(p.a3_zero)),
        "B2": smaller_better(fv.b2, float(p.b2_T)),
        "B3": smaller_better(fv.b3, float(p.b3_T)),
        "B4": 0.0 if fv.b4_degenerate or fv.b4 is None else b4_map(fv.b4, m.b4),
        "B5": smaller_better(fv.b5, float(m.b5_T)),
        "B6": smaller_better(fv.b6, float(p.b6_T)),
        "B7": smaller_better(fv.b7, float(m.b7_T)),
        "B9": smaller_better(fv.b9, float(p.b9_T)),
        "D1": 0.0 if fv.d1 is None else larger_better(fv.d1, float(m.d1_sat)),
        "D2": 1.0 if fv.d2 else 0.0,
        "D3": 1.0 if fv.d3 else 0.0,
    }
    raw: dict[str, float | bool | None] = {
        "A1": fv.a1,
        "A2": fv.a2,
        "A3": fv.a3,
        "B2": fv.b2,
        "B3": fv.b3,
        "B4": fv.b4,
        "B5": fv.b5,
        "B6": fv.b6,
        "B7": fv.b7,
        "B9": fv.b9,
        "D1": fv.d1,
        "D2": fv.d2,
        "D3": fv.d3,
    }

    if fv.a_group_discounted:
        for key in _A_GROUP:
            ach[key] *= A_GROUP_DISCOUNT

    items = {key: (raw[key], ach[key], float(s.weights[key]) * ach[key]) for key in _FACTORS}
    return ScoreResult(score=sum(item[2] for item in items.values()), items=items)
