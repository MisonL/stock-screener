"""指标原语（口径照 spec §4.6.1.1）。

全部 `numpy.ndarray` 进出，预热期返回 NaN，与 TA-Lib 的 lookback 对齐。
均线用 SMA、ATR 用 Wilder、MACD 柱是 2×(DIF−DEA)、分位用平均秩、斜率用 OLS。
"""
from __future__ import annotations

import numpy as np
import talib


def sma(x, n: int) -> np.ndarray:
    """简单算术平均（TA-Lib `MA` matype=0），前 n−1 根为 NaN。"""
    return talib.SMA(np.asarray(x, dtype=np.float64), timeperiod=n)


def atr_wilder(h, l, c, n: int = 14) -> np.ndarray:
    """Wilder ATR；TR 含前收跳空，初值 = 前 n 个 TR 的算术平均。"""
    return talib.ATR(
        np.asarray(h, dtype=np.float64),
        np.asarray(l, dtype=np.float64),
        np.asarray(c, dtype=np.float64),
        timeperiod=n,
    )


def macd(c, fast: int = 12, slow: int = 26, sig: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`(dif, dea, hist)`；TA-Lib 的 hist 只有 1 倍，这里按 spec 返回 2×(DIF−DEA)。"""
    dif, dea, hist = talib.MACD(
        np.asarray(c, dtype=np.float64),
        fastperiod=fast,
        slowperiod=slow,
        signalperiod=sig,
    )
    return dif, dea, 2.0 * hist


def golden_cross(dif, dea, i0: int, i1: int) -> bool:
    """`[i0, i1]` 内是否存在 DIF 上穿 DEA（前一日 DIF ≤ DEA，当日 DIF > DEA）。"""
    for i in range(max(i0, 1), i1 + 1):
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            return True
    return False


def rank_pct(window, x) -> float:
    """平均秩百分位：`(#<x + 0.5·#==x) / n × 100`，窗口含当日。"""
    window = np.asarray(window, dtype=np.float64)
    less = int(np.count_nonzero(window < x))
    equal = int(np.count_nonzero(window == x))
    return (less + 0.5 * equal) / window.size * 100.0


def slope_pct(y) -> float:
    """对 `t = 0…N−1` 做 OLS，斜率 ÷ `mean(y)`（单位为 %/日）。"""
    y = np.asarray(y, dtype=np.float64)
    t = np.arange(y.size, dtype=np.float64)
    slope = np.sum((t - t.mean()) * (y - y.mean())) / np.sum((t - t.mean()) ** 2)
    return float(slope / y.mean())


def cv(y) -> float:
    """样本变异系数：`std(ddof=1) / mean`。"""
    y = np.asarray(y, dtype=np.float64)
    return float(np.std(y, ddof=1) / np.mean(y))
