"""Task 19：分时量能曲线（插值）与 C2 时段阈值。

曲线只有 9 个节点，盘中 5 分钟粒度有 47 个时刻（09:45–11:30 的 22 个 + 13:00–15:00 的 25 个），
其中 38 个必须走插值——`dict.get(hm, 常数)` 兜底会系统性高估午后 C2（spec §4.6.8 第 4 条）。
"""
from datetime import time

import pytest

from core.clock import traded_minutes
from core.intraday.curve import VolumeFreezer, c2_threshold, curve_at, projected_full_day
from tests.helpers import minutes

# 47 个盘中时刻，显式按已交易分钟生成，不含午休时刻（spec §4.6.8 第 4 条，2026-11-02 勘误为 47）。
EVERY_5MIN_TRADING = (
    [f"09:{m:02d}" for m in range(45, 60, 5)]                  # 09:45, 09:50, 09:55
    + [f"10:{m:02d}" for m in range(0, 60, 5)]                  # 10:00 … 10:55
    + [f"11:{m:02d}" for m in range(0, 35, 5)]                  # 11:00 … 11:30
    + [f"13:{m:02d}" for m in range(0, 60, 5)]                  # 13:00 … 13:55
    + [f"14:{m:02d}" for m in range(0, 60, 5)]                  # 14:00 … 14:55
    + ["15:00"]
)  # 共 47 个盘中时刻（47 − 9 节点 = 38 个插值点）

NODES = ["09:45", "10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]


def neighbors(hm: str) -> tuple[str, str]:
    """返回 hm 之前与之后的两个节点；hm 恰为节点时返回 (该节点, 下一节点)。

    跨午休的 11:30→13:30 按 traded_minutes 连续处理，不出现「隔 90 分钟」的插值分母。
    """
    t = minutes(hm)
    for a, b in zip(NODES, NODES[1:]):
        if minutes(a) <= t <= minutes(b):
            return a, b
    return NODES[-2], NODES[-1]


def test_grid_has_47_traded_times_and_38_interpolated():
    assert len(EVERY_5MIN_TRADING) == 47
    assert len(set(EVERY_5MIN_TRADING)) == 47
    assert len([hm for hm in EVERY_5MIN_TRADING if hm in NODES]) == 9
    assert all(hm not in EVERY_5MIN_TRADING for hm in ("11:35", "11:40", "12:00", "12:30"))


@pytest.mark.parametrize("hm,v", [("09:45", 0.19), ("10:30", 0.43), ("13:30", 0.71), ("15:00", 1.00)])
def test_nodes(hm, v, CURVE):
    assert curve_at(hm, CURVE) == pytest.approx(v)


def test_cross_boundaries(CURVE):
    assert curve_at("11:29", CURVE) == pytest.approx(0.52 + (0.59 - 0.52) * 29 / 30)
    assert curve_at("13:01", CURVE) == pytest.approx(0.59 + (0.71 - 0.59) * 1 / 30)
    assert curve_at("12:15", CURVE) == pytest.approx(0.59)        # 午休不前进


def test_no_constant_fallback(CURVE):
    """47 个时刻里 9 个精确命中节点、其余 38 个必须落在相邻节点的直线上。"""
    nodes = dict(CURVE)
    for hm in EVERY_5MIN_TRADING:
        v = curve_at(hm, CURVE)
        if hm in nodes:
            assert v == pytest.approx(nodes[hm])          # 节点精确匹配
        else:
            # 落在相邻两节点确定的直线方程上——早盘与午后都要校验（13:00–14:55 的 21 个插值点
            # 正是「午后 C2 高估 3.26×」那段；只判 hm < "11:30" 会漏掉午后）
            prev_n, next_n = neighbors(hm)
            t_traded = traded_minutes(time.fromisoformat(hm))
            t_prev = traded_minutes(time.fromisoformat(prev_n))
            t_next = traded_minutes(time.fromisoformat(next_n))
            expected = nodes[prev_n] + (nodes[next_n] - nodes[prev_n]) * (t_traded - t_prev) / (t_next - t_prev)
            assert v == pytest.approx(expected, abs=1e-6)
    # 午休时刻全部 == 0.59
    for hm in [f"11:{m:02d}" for m in range(35, 60, 5)] + [f"12:{m:02d}" for m in range(0, 60, 5)]:
        assert curve_at(hm, CURVE) == pytest.approx(0.59)
    # 13:05 的值 ≠ 0.30（一段被常数兜底过的旧坑）
    assert curve_at("13:05", CURVE) != pytest.approx(0.30)


def test_before_0945_is_none(CURVE, S):
    assert curve_at("09:44", CURVE) is None and c2_threshold("09:44", S) is None
    assert curve_at("09:30", CURVE) is None


def test_strict_window(S):
    assert c2_threshold("09:50", S) == 2.5 and c2_threshold("10:00", S) == 1.5


def test_lunch_is_none(S):
    """午休优先判：11:30–13:00 一律 None，不能被写成 [10:00, 14:57) 的 c2。"""
    for hm in ("11:30", "11:35", "12:00", "12:59"):
        assert c2_threshold(hm, S) is None


def test_c2_threshold_after_1457(S):
    assert c2_threshold("14:57", S) == 1.5 and c2_threshold("14:59", S) == 1.5 and c2_threshold("15:00", S) == 1.5


def test_freeze_after_1457():
    f = VolumeFreezer()
    f.ratio("x", "14:57", 1.8)
    assert f.ratio("x", "14:59", 1.2) == 1.8


def test_freeze_falls_back_to_last_valid_ratio():
    """14:57 前无值时取最近一次有效 ratio（不是当次 ratio_now）。"""
    f = VolumeFreezer()
    assert f.ratio("y", "10:00", 1.2) == 1.2
    assert f.ratio("y", "14:58", 9.9) == 1.2
    assert f.ratio("y", "15:00", 9.9) == 1.2


def test_freezer_is_per_code():
    f = VolumeFreezer()
    f.ratio("a", "14:57", 1.8)
    f.ratio("b", "14:57", 2.4)
    assert f.ratio("a", "15:00", 0.1) == 1.8
    assert f.ratio("b", "15:00", 0.1) == 2.4


def test_curve_at_1457_freeze(CURVE):
    # 14:59 的 curve_at 仍插值（冻结的是 VolumeFreezer 的 ratio，不是 curve_at）
    assert curve_at("14:59", CURVE) == pytest.approx(0.86 + (1.0 - 0.86) * 29 / 30)


def test_curve_at_1500_plus(CURVE):
    assert curve_at("15:01", CURVE) == 1.0


def test_projected_full_day(CURVE):
    assert projected_full_day(1000.0, "10:30", CURVE) == pytest.approx(1000.0 / 0.43)
    assert projected_full_day(1000.0, "14:59", CURVE) == pytest.approx(
        1000.0 / (0.86 + (1.0 - 0.86) * 29 / 30)
    )
    assert projected_full_day(1000.0, "09:44", CURVE) is None
