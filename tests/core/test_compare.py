"""compare_pattern（z-score + 皮尔逊 + 后 20 日事实）。

用直接造库的方式喂序列：这里测的是算法与窗口口径，不是选股链路。
"""
import math
from datetime import datetime

import pytest

from core.clock import FixedClock, SH
from core.config import load_config
from core.strategy.compare import compare_pattern
from pathlib import Path

CFG = load_config(Path("config"))
TODAY = 20261030
CLOCK = FixedClock(datetime(2026, 10, 30, 15, 0, tzinfo=SH))


def seed(conn, closes, *, start_date=20260801, box_days=None, market="cn_main"):
    """喂一段复权收盘 + 原始收盘；`daily_bars` 与 `daily_bars_adj` 日期一致。"""
    dates = [start_date + i for i in range(len(closes))]
    for d, c in zip(dates, closes):
        # 交易日历（sh000300）必须同步喂：停牌判据与「已收盘」判定都靠它，
        # 日历为空时 compare 会 fail-closed 成 ref_window_insufficient
        conn.execute(
            "INSERT OR IGNORE INTO daily_bars VALUES ('sh000300', ?, 3000, 3000, 3000, 3000, 0, 0, 'x')",
            (d,))
        conn.execute(
            "INSERT INTO daily_bars VALUES ('sh600519', ?, ?, ?, ?, ?, 1e6, 1e7, 'backfill')",
            (d, c, c * 1.01, c * 0.99, c))
        conn.execute(
            "INSERT INTO daily_bars_adj VALUES ('sh600519', ?, ?, ?, ?, ?, 1e6, 1.0, 1.0)",
            (d, c, c * 1.01, c * 0.99, c))
    conn.execute("INSERT OR REPLACE INTO stock_meta(code,name,market,updated_at)"
                 " VALUES ('sh600519','贵州茅台',?,'x')", (market,))
    if box_days:
        conn.execute(
            "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
            " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean, pos_in_box)"
            " VALUES (?,?,'sh600519',1,80,'{}',?,0,0,0,0,0,0,0.5)", (dates[-1], market, box_days))
    conn.commit()
    return dates


def wave(n, base=10.0, amp=1.0):
    return [base + amp * math.sin(i / 2.0) for i in range(n)]


def test_self_comparison_is_perfect(conn):
    closes = wave(60)
    dates = seed(conn, closes, box_days=20)
    # 当前窗口右端是昨天 → 第 40..59 根；参考窗口取同一段
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[39]), str(dates[58]), cfg=CFG)
    assert reason is None
    assert data["similarity"] == pytest.approx(1.0, abs=1e-6)
    assert data["rmse"] == pytest.approx(0.0, abs=1e-6)


def test_mirrored_series_is_negatively_correlated(conn):
    """参考窗口 = 当前窗口关于均值的镜像：仿射反射不改变 z-score 的绝对值，
    z-score 逐点取负 → 皮尔逊系数恒为 −1。"""
    closes = wave(60)
    date_s = seed(conn, closes, box_days=20)
    cur = closes[39:59]                                # 当前窗口（右端是昨天）
    for i, v in enumerate([20.0 - c for c in cur]):    # 前 20 根写成当前窗口的镜像
        for table, cols in (("daily_bars", "('sh600519', ?,?,?,?,?,1e6,1e7,'b')"),
                            ("daily_bars_adj", "('sh600519', ?,?,?,?,?,1e6,1.0,1.0)")):
            conn.execute(f"UPDATE {table} SET open=?, high=?, low=?, close=? WHERE code='sh600519' AND date=?",
                         (v, v * 1.01, v * 0.99, v, date_s[i]))
    conn.commit()
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(date_s[0]), cfg=CFG,
                                   ref_end=str(date_s[19]))
    assert reason is None
    assert data["similarity"] == pytest.approx(-1.0, abs=1e-6)


def test_after_20d_uses_bars_after_reference_window(conn):
    closes = list(range(1, 61))          # 单调上行，便于算收益
    dates = seed(conn, closes, box_days=20)
    ref_end = dates[19]
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[0]), str(ref_end), cfg=CFG)
    assert reason is None
    # 基准是参考窗口**最后一根**（第 20 根），之后 20 根从第 21 根起
    expected = closes[39] / closes[19] - 1
    assert data["after_20d"]["return"] == pytest.approx(round(expected, 4), abs=1e-4)
    assert data["after_20d"]["max_drawdown"] <= 0


def test_suspended_reference_window_rejected(conn):
    closes = list(range(1, 61))
    dates = seed(conn, closes, box_days=20)
    # 删掉参考窗口里 80% 的 bar，模拟长期停牌
    for d in dates[:16]:
        conn.execute("DELETE FROM daily_bars_adj WHERE code='sh600519' AND date=?", (d,))
        conn.execute("DELETE FROM daily_bars WHERE code='sh600519' AND date=?", (d,))
    conn.commit()
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[0]), str(dates[19]), cfg=CFG)
    assert data is None and reason == "ref_window_insufficient"


def test_no_current_box(conn):
    conn.execute("INSERT OR REPLACE INTO stock_meta(code,name,market,updated_at)"
                 " VALUES ('sh600519','贵州茅台','cn_main','x')")
    conn.commit()                               # 有票无 K 线、无池记录：造不出箱体
    data, reason = compare_pattern(conn, CLOCK, "sh600519", "20260801", cfg=CFG)
    assert data is None and reason == "no_current_box"


def test_returns_plain_data_and_reason_not_envelope(conn):
    """约定：返回 (data, reason)；自己套 ok:false 会让调用方得到双层信封、grounding 路径错位。"""
    seed(conn, wave(40), box_days=20)
    data, reason = compare_pattern(conn, CLOCK, "sh600519", "20260801", cfg=CFG)
    assert "ok" not in data and reason is None
    assert data["adjust"] == "adj"


# ---- 复核补测：窗口右端 / 已收盘 / 对齐 / 停牌分母 / 边界 ----------------

def _seed_with_today(conn, n=40, today_spike=99.0):
    """喂 n 根平稳序列 + 一根「今天」的异常 bar（模拟盘中未完成/脏数据）。"""
    closes = [10.0 + 0.01 * i for i in range(n - 1)]
    dates = seed(conn, closes, box_days=10)
    today = dates[-1] + 1
    for table, cols in (("daily_bars",
                         "('sh600519', ?, ?, ?, ?, ?, 1e6, 1e7, 'b')"),
                        ("daily_bars_adj",
                         "('sh600519', ?, ?, ?, ?, ?, 1e6, 1.0, 1.0)")):
        conn.execute(f"INSERT INTO {table} VALUES {cols}",
                     (today, today_spike, today_spike, today_spike, today_spike))
    conn.execute("INSERT OR IGNORE INTO daily_bars VALUES"
                 " ('sh000300', ?, 3000,3000,3000,3000,0,0,'x')", (today,))
    conn.commit()
    return dates, today


def test_current_window_excludes_today(conn):
    """箱体窗口右端是昨天：今日那根 99 的异常 bar 不能进当前窗口（否则自比不再是 1.0）。"""
    dates, today = _seed_with_today(conn)
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[0]), cfg=CFG)
    assert reason is None
    assert data["similarity"] == pytest.approx(1.0, abs=1e-6)


def test_after_20d_ignores_unclosed_today(conn):
    """盘中（未到 14:57）今日 bar 一律不参与：after_20d 的 n 与数值都不能被它影响。"""
    from datetime import datetime as _dt

    dates, today = _seed_with_today(conn)
    intraday = FixedClock(_dt(2026, 10, 30, 10, 30, tzinfo=SH))
    data, reason = compare_pattern(conn, intraday, "sh600519", str(dates[0]), cfg=CFG)
    assert reason is None
    assert data["after_20d"]["n"] < 20 or data["after_20d"]["return"] != pytest.approx(
        (99.0 / 10.0 - 1), abs=1e-3)


def test_longer_reference_window_is_tail_aligned(conn):
    """区间比 N* 长时取**最近**的一段；取最早一段会把完全不同形态当成参考。"""
    closes = wave(60)
    dates = seed(conn, closes, box_days=20)
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[0]), str(dates[59]), cfg=CFG)
    assert reason is None
    assert data["n"] == 20
    # 尾部对齐：参考窗口最后一根 = 数据最后一根 → 与当前窗口完全一致
    assert data["similarity"] == pytest.approx(1.0, abs=1e-6)


def test_tail_suspension_is_rejected(conn):
    """尾部停牌（窗口最后一段没有 bar）必须被看见：分母终点是 ref_end 不是最后一根 bar。"""
    closes = list(range(1, 61))
    dates = seed(conn, closes, box_days=20)
    for d in dates[8:20]:                       # 参考窗口 20 天里停 12 天（在尾部）
        # **只删个股的 bar，指数日历必须留着**：日历也删掉的话「那天没有交易日」，
        # 停牌与「本来就不开市」就分不出来了
        conn.execute("DELETE FROM daily_bars_adj WHERE code='sh600519' AND date=?", (d,))
        conn.execute("DELETE FROM daily_bars WHERE code='sh600519' AND date=?", (d,))
    conn.commit()
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[0]), str(dates[19]), cfg=CFG)
    assert data is None and reason == "ref_window_insufficient"


def test_empty_calendar_fails_closed(conn):
    """交易日历为空时不能假装能比：返回残缺序列的相关系数比不答更糟。"""
    dates = seed(conn, wave(40), box_days=20)
    conn.execute("DELETE FROM daily_bars WHERE code='sh000300'")
    conn.commit()
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[0]), cfg=CFG)
    assert data is None and reason == "ref_window_insufficient"


def test_ref_start_before_list_date(conn):
    """早于上市日的窗口：前段根本不存在。"""
    dates = seed(conn, wave(40), box_days=20)
    conn.execute("UPDATE stock_meta SET list_date = ? WHERE code='sh600519'", (dates[20],))
    conn.commit()
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[0]), cfg=CFG)
    assert data is None and reason == "ref_window_insufficient"


def test_ref_end_none_length_equals_current_n(conn):
    """点名的口径：ref_end=None 时参考窗口长度 == 当前 N*。"""
    dates = seed(conn, wave(60), box_days=20)
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[0]), cfg=CFG)
    assert reason is None and data["n"] == 20
    assert data["after_20d"]["n"] == 20


def test_degenerate_series_reports_none(conn):
    """常数序列相似度无定义：给 0.0 是把「无定义」冒充成「完全不像」。"""
    dates = seed(conn, [10.0] * 40, box_days=20)
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[0]), cfg=CFG)
    assert reason is None and data["degenerate"] is True
    assert data["similarity"] is None and data["rmse"] is None


def test_bad_date_formats_are_rejected_not_crashed(conn):
    dates = seed(conn, wave(40), box_days=20)
    for bad in ("2026/08/03", "abc", "", "2026-8-3"):
        data, reason = compare_pattern(conn, CLOCK, "sh600519", bad, cfg=CFG)
        assert data is None and reason == "ref_window_insufficient", bad


def test_best_segments_do_not_overlap(conn):
    """三段不能是同一段的相邻滑动（自比时三段 0.98+ 毫无信息量）。"""
    dates = seed(conn, wave(60), box_days=40)
    data, reason = compare_pattern(conn, CLOCK, "sh600519", str(dates[0]), cfg=CFG)
    assert reason is None
    offs = [s["offset"] for s in data["best_segments"]]
    seg_len = max(5, data["n"] // 3)
    assert offs == sorted(offs)
    assert all(b - a >= seg_len for a, b in zip(offs, offs[1:]))
