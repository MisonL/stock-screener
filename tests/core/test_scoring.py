"""分段线性映射与打分（spec §4.3、§4.6.3、§4.6.3.1、§4.6.5、§4.6.7）。

`score` 的权威输入是 §4.6.3.1 的原型 600519：横盘 23 天、箱体振幅 8.85%、回撤 38.4%、
量能比 0.26、跑赢沪深300 9.5%，逐项复算总分 **92.0**。这个数把「阈值处 0.6」「一分项
一分」两件事同时锁住——任何一处映射写反，总分都对不上。

文件顶部另有 `fv_all_full_discounted`：各项 raw 取满分、A 组折半的常量（§4.6.5 的
「5 ≤ L < 10」场景），用来验证折半后上限正好 85.0。
"""
import pytest

from core.strategy.factors import FactorValues
from core.strategy.scoring import (
    a1_bell,
    a3_map,
    b4_map,
    larger_better,
    score,
    smaller_better,
)

# 全部因子都在满分点的原值（达成度 1.0）：A1 落在 35%–45% 平台、B4 ≤ 0.275 …
FULL_MARKS = dict(
    a1=0.40,
    a2=0.02,
    a3=-0.001,
    b2=0.05,
    b3=0.01,
    b4=0.20,
    b5=10.0,
    b6=0.02,
    b7=10.0,
    b9=0.001,
    d1=0.20,
    d2=True,
    d3=True,
    box_vol_mean_adj=1.0,
    a_group_discounted=False,
    b4_degenerate=False,
)


def _fv(**kw) -> FactorValues:
    return FactorValues(**{**FULL_MARKS, **kw})


fv_all_full_discounted = _fv(a_group_discounted=True)


# ---------------------------------------------------------------- §4.6.3.1 原型

def test_600519_prototype_scores_92(MAIN, S):
    fv = FactorValues(a1=0.384, a2=0.026, a3=-0.0002, b2=0.0885, b3=0.011, b4=0.26, b5=16, b6=0.028,
                      b7=12.2, b9=0.001, d1=0.095, d2=False, d3=False,
                      box_vol_mean_adj=0.40, a_group_discounted=False, b4_degenerate=False)
    r = score(fv, MAIN, S)
    assert r.items["B2"][1:] == (pytest.approx(0.81, abs=0.005), pytest.approx(8.1, abs=0.05))
    assert r.items["D1"][1] == pytest.approx(0.98, abs=0.005)
    assert r.score == pytest.approx(92.0, abs=0.05)
    assert r.score == sum(p for _, _, p in r.items.values())


@pytest.mark.parametrize("dd,ach", [(0.18, 0.54), (0.20, 0.60), (0.35, 1.0), (0.50, 0.767), (0.70, 0.3)])
def test_a1_bell(dd, ach, MAIN):
    assert a1_bell(dd, MAIN.a1) == pytest.approx(ach, abs=0.001)   # §4.3 的四个示例


def test_a1_bell_edges(MAIN):
    assert a1_bell(0.0, MAIN.a1) == pytest.approx(0.0)       # 没跌：0 分
    assert a1_bell(-0.05, MAIN.a1) == pytest.approx(0.0)     # 脏数据不越过下界
    assert a1_bell(0.45, MAIN.a1) == pytest.approx(1.0)      # 平台右端仍是 1.0
    assert a1_bell(0.60, MAIN.a1) == pytest.approx(0.3)      # ≥ 60% 常数 0.3


# ---------------------------------------------------------------- 通用映射

@pytest.mark.parametrize("v,ach", [(0.0, 1.0), (0.06, 1.0), (0.09, 0.8), (0.12, 0.6), (0.18, 0.0), (0.30, 0.0)])
def test_smaller_better(v, ach):
    """T=0.12：T/2 饱和、T 及格 0.6、1.5T 归零。"""
    assert smaller_better(v, 0.12) == pytest.approx(ach)


@pytest.mark.parametrize("v,ach", [(0.10, 1.0), (0.20, 1.0), (0.05, 0.8), (0.0, 0.6), (-0.05, 0.3), (-0.10, 0.0), (-0.20, 0.0)])
def test_larger_better_zero_lower_bound(v, ach):
    """L == 0 退化：下界取 −S（spec §4.6.3 的 D1 规则）。"""
    assert larger_better(v, 0.10, 0.0) == pytest.approx(ach)


def test_larger_better_with_lower_bound():
    """L > 0：值 == L → 0.6、值 ≤ L×0.6 → 0.0、值 ≥ S → 1.0。"""
    assert larger_better(0.05, 0.20, 0.05) == pytest.approx(0.6)
    assert larger_better(0.03, 0.20, 0.05) == pytest.approx(0.0)
    assert larger_better(0.125, 0.20, 0.05) == pytest.approx(0.8)
    assert larger_better(0.20, 0.20, 0.05) == pytest.approx(1.0)


def test_a3_map():
    """MA60 斜率：≤ 0 满分、≥ zero_at 归零、中点 0.5（spec §4.6.3 的 0.075%/日 → 0.5）。"""
    assert a3_map(-0.001, 0.0015) == pytest.approx(1.0)
    assert a3_map(0.0, 0.0015) == pytest.approx(1.0)
    assert a3_map(0.00075, 0.0015) == pytest.approx(0.5)
    assert a3_map(0.0015, 0.0015) == pytest.approx(0.0)
    assert a3_map(0.003, 0.0015) == pytest.approx(0.0)


def test_b4_map_three_points(S):
    """B4 用自己的三点：0.275 / 0.55 / 0.75（不套用 1.5T 的通用规则）。"""
    assert b4_map(0.20, S.mapping.b4) == pytest.approx(1.0)
    assert b4_map(0.275, S.mapping.b4) == pytest.approx(1.0)
    assert b4_map(0.55, S.mapping.b4) == pytest.approx(0.6)
    assert b4_map(0.65, S.mapping.b4) == pytest.approx(0.3)
    assert b4_map(0.75, S.mapping.b4) == pytest.approx(0.0)


def test_b4_no_cliff_at_hard_floor():
    assert b4_map(0.7499) == pytest.approx(0.0, abs=0.001) and b4_map(0.75) == 0.0


# ---------------------------------------------------------------- 打分

def test_discount_caps_at_85(MAIN, S):
    assert score(fv_all_full_discounted, MAIN, S).score == pytest.approx(85.0)


def test_discount_halves_only_a_group(MAIN, S):
    full = score(_fv(), MAIN, S)
    disc = score(_fv(a_group_discounted=True), MAIN, S)
    for key in ("A1", "A2", "A3"):
        assert disc.items[key][1] == pytest.approx(full.items[key][1] * 0.5)
        assert disc.items[key][2] == pytest.approx(full.items[key][2] * 0.5)
    for key in ("B2", "B3", "B4", "B5", "B6", "B7", "B9", "D1", "D2", "D3"):
        assert disc.items[key] == full.items[key]


def test_full_marks_score_100(MAIN, S):
    r = score(_fv(), MAIN, S)
    assert set(r.items) == {"A1", "A2", "A3", "B2", "B3", "B4", "B5", "B6", "B7", "B9", "D1", "D2", "D3"}
    assert sum(S.weights.values()) == pytest.approx(100.0)
    assert r.score == pytest.approx(100.0)
    assert all(ach == pytest.approx(1.0) for _, ach, _ in r.items.values())


def test_b4_degenerate_scores_zero(MAIN, S):
    r = score(_fv(b4=None, b4_degenerate=True), MAIN, S)
    assert r.items["B4"] == (None, 0.0, 0.0)
    assert r.score == pytest.approx(85.0)     # 100 − 15（B4 权重）


def test_d1_missing_scores_zero(MAIN, S):
    r = score(_fv(d1=None), MAIN, S)
    assert r.items["D1"][1:] == (0.0, 0.0)
    assert r.score == pytest.approx(96.0)     # 100 − 4（D1 权重）


def test_bool_factors_are_binary(MAIN, S):
    r = score(_fv(d2=False, d3=False), MAIN, S)
    assert r.items["D2"] == (False, 0.0, 0.0)
    assert r.items["D3"] == (False, 0.0, 0.0)


def test_market_thresholds_come_from_config(CFG):
    """同一组原值在创业板（b2_T=24%、b3_T=5%）上宽松，证明阈值走 `p` 不走常量。"""
    gem = score(_fv(b2=0.10, b3=0.02), CFG.market("cn_gem"), CFG.strategy)
    main = score(_fv(b2=0.10, b3=0.02), CFG.market("cn_main"), CFG.strategy)
    assert gem.items["B2"][1] == pytest.approx(1.0)      # 0.10 ≤ 0.12
    assert main.items["B2"][1] == pytest.approx(0.6 + (0.12 - 0.10) / 0.06 * 0.4)   # 0.7333
    assert gem.items["B3"][1] == pytest.approx(1.0)      # 0.02 ≤ 0.025
    assert main.items["B3"][1] < gem.items["B3"][1]
