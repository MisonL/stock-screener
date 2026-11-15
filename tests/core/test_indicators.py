"""指标原语（口径照 spec §4.6.1.1）。"""
import numpy as np
import pytest

from core.indicators import atr_wilder, cv, golden_cross, macd, rank_pct, slope_pct, sma

close_200 = 10.0 + np.cumsum(np.full(220, 0.4))


def test_rank_pct_average_rank():
    assert rank_pct(np.array([1, 2, 2, 3]), 2) == 50.0


def test_slope_pct_ols():
    assert slope_pct(np.array([10, 11, 12, 13, 14.0])) == pytest.approx(1 / 12)


def test_cv_ddof1():
    assert cv(np.array([10, 10.1, 9.9])) == pytest.approx(0.01)


def test_macd_hist_is_double():
    d, e, h = macd(close_200)
    assert np.allclose(h[60:], 2 * (d[60:] - e[60:]))


def test_macd_hist_doubles_nonzero_spread():
    # close_200 单调线性会让 DIF 与 DEA 收敛到相等，上面的恒等式恒为 0；
    # 这条用加速序列保证 hist 真的非零，1 倍的实现会失败。
    accel = 10.0 + 0.001 * np.arange(220) ** 2
    d, e, h = macd(accel)
    assert np.max(np.abs(h[60:] - (d[60:] - e[60:]))) > 1e-6
    assert np.allclose(h[60:], 2 * (d[60:] - e[60:]))


def test_atr_seed_is_mean_of_first_14_tr():
    c = 10.0 + 0.1 * np.arange(20)
    c[5:] += 1.0                       # 第 5 根整体跳空高开，只有这一根产生缺口
    h = c + 0.2
    l = c - 0.2
    h[5], l[5] = c[5] + 0.05, c[5] - 0.05   # 跳空 bar 自身振幅只有 0.1

    atr = atr_wilder(h, l, c, 14)
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])

    assert np.isnan(atr[13])
    assert atr[14] == pytest.approx(tr[:14].mean())
    # 初值必须含跳空缺口，而不是只用 H−L
    assert atr[14] > np.mean(h[1:15] - l[1:15])


def test_ma60_is_simple_not_ema():
    jumpy = np.concatenate([np.full(160, 10.0), np.full(60, 20.0)])
    ma60 = sma(jumpy, 60)
    assert ma60[-1] == pytest.approx(20.0)   # 简单平均：最后 60 根全是 20
    # EMA60 在同样的台阶上会明显低于 20（约 18.6），SMA 不会
    assert ma60[-1] > 19.99


def test_golden_cross_range():
    dif = np.array([0.0, -1.0, 0.5, 1.0, 2.0])
    dea = np.zeros(5)
    assert golden_cross(dif, dea, 2, 2) is True
    assert golden_cross(dif, dea, 0, 1) is False
    assert golden_cross(dif, dea, 3, 4) is False
