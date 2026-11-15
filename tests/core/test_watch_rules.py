"""第一部分：自选股 / 指数 / 板块 / 竞价规则（`core/intraday/watch_rules.py`）。"""
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from core.config import load_config
from core.intraday.watch_rules import MoveRing, eval_auction, eval_index, eval_sector, eval_watch
from core.models import Bar, Quote, SectorRow

CFG = load_config(Path("config"))
INTRA = CFG.strategy.intraday
T = datetime(2026, 10, 30, 10, 30)


def mkq(**kw):
    base = dict(code="sh600519", name="贵州茅台", price=10.0, prev_close=10.0, open=10.0,
                high=10.1, low=9.9, volume=1000.0, amount=1.0e7, volume_ratio=1.0,
                limit_up=None, limit_down=None, ts=T, raw=[])
    base.update(kw)
    return Quote(**base)


def hist(closes, *, start=20260701, vols=None):
    out = []
    for i, c in enumerate(closes):
        d = datetime(2026, 7, 1) + timedelta(days=i)
        out.append(Bar("sh600519", int(d.strftime("%Y%m%d")), c, c, c, c,
                       (vols[i] if vols else 1.0e6), 1.0e7, "backfill"))
    return out


# ---- eval_watch ----------------------------------------------------------

def test_watch_pct_threshold_fires():
    ds = eval_watch(mkq(price=10.6, prev_close=10.0), hist([10.0] * 60), 10.0, INTRA, "10:30")
    kinds = [d.kind for d in ds]
    assert "watch_rule" in kinds
    assert all(d.watch for d in ds)          # D6：自选股产出一律 watch=True


def test_watch_threshold_override_per_stock():
    # 用户在网页上给这只票单独设了 10% → 涨 5.45% 默认该触发、10% 不该触发
    bars = hist([11.0] * 60)
    q_rise = mkq(price=11.6, prev_close=11.0, high=11.6, low=11.0)
    assert "watch_rule" in [d.kind for d in eval_watch(q_rise, bars, 11.0, INTRA, "10:30")]
    ds = eval_watch(q_rise, bars, 11.0, INTRA, "10:30", pct_threshold=0.10)
    assert "watch_rule" not in [d.kind for d in ds]


def test_watch_paused_on_exright():        # §5.3：除权日整只暂停
    ds = eval_watch(mkq(price=10.6, prev_close=9.0), hist([10.0] * 60), 10.0, INTRA, "10:30")
    assert ds == []


def test_watch_extreme_high_with_volume():
    bars = hist([10.0] * 59 + [9.0])
    ds = eval_watch(mkq(price=9.0, prev_close=9.0, volume_ratio=2.5), bars, 9.0, INTRA, "10:30")
    assert "watch_rule" in [d.kind for d in ds]


def test_watch_ma20_cross():
    # 前一日收盘（9.9）在 MA20（10.0）之下、现价（10.2）在其上 → 穿越
    bars = hist([10.0] * 79 + [9.9])
    ds = eval_watch(mkq(price=10.2, prev_close=9.9), bars, 10.0, INTRA, "10:30")
    assert any("ma20" in str(d.payload.get("reason")) for d in ds)


def test_watch_silent_before_0945():
    ds = eval_watch(mkq(price=10.6, prev_close=10.0), hist([10.0] * 60), 10.0, INTRA, "09:40")
    assert "watch_rule" not in [d.kind for d in ds]


# ---- MoveRing / index / sector ------------------------------------------

def test_index_move_needs_300s():
    ring = MoveRing(300)
    ring.push("sh000001", T - timedelta(seconds=299), 100.0)      # 299 秒前：不够老
    assert ring.change("sh000001", T) is None
    older = MoveRing(300)
    older.push("sh000001", T - timedelta(seconds=301), 100.0)     # ≥300 秒前：可用
    assert older.change("sh000001", T) == pytest.approx(0.0)


def test_index_move_fires_on_one_percent():
    ring = MoveRing(300)
    ring.push("sh000001", T - timedelta(seconds=310), 100.0)
    ds = eval_index(ring, {"sh000001": 101.2}, T, INTRA)
    assert [d.kind for d in ds] == ["index_move"]


def test_index_move_below_threshold_silent():
    ring = MoveRing(300)
    ring.push("sh000001", T - timedelta(seconds=310), 100.0)
    assert eval_index(ring, {"sh000001": 100.5}, T, INTRA) == []


def test_sector_small_skipped():
    ring = MoveRing(300)
    row = SectorRow("new_001", "小板块", 103.0, 100.0, 3.0, 1.0, T, members=8)
    rows = [row] + [SectorRow(f"new_00{i}", f"x{i}", 100.0, 100.0, 0.0, 1.0, T, members=8)
                    for i in range(7)]
    for r in rows:
        ring.push(r.code, T - timedelta(seconds=310), r.prev_close)
    assert eval_sector(ring, rows, T, INTRA) == []          # 8 只 < min_members 10

    rows = [SectorRow(r.code, r.name, r.price, r.prev_close, r.chg, r.volume, T, members=12) for r in rows]
    rows.append(SectorRow("new_009", "x9", 100.0, 100.0, 0.0, 1.0, T, members=12))
    rows.append(SectorRow("new_010", "x10", 100.0, 100.0, 0.0, 1.0, T, members=12))
    for r in rows[8:]:
        ring.push(r.code, T - timedelta(seconds=310), r.prev_close)
    assert [d.kind for d in eval_sector(ring, rows, T, INTRA)] == ["sector_move"]


# ---- auction -------------------------------------------------------------

def test_auction_baseline_not_ready():
    # 基线不足 20 天 → 只判高低开、无量能异常
    ds = eval_auction(mkq(price=10.0, prev_close=10.0), baseline_20d=[1.0] * 7, p=INTRA)
    assert ds == []
    ds = eval_auction(mkq(price=10.5, prev_close=10.0), baseline_20d=[1.0] * 7, p=INTRA)
    assert [d.kind for d in ds] == ["auction_anomaly"]
    assert ds[0].payload["baseline_days"] == 7


def test_auction_gap_and_volume():
    q = mkq(price=10.3, prev_close=10.0, volume=4.0e6)     # 高开 3%，竞价量 4 倍
    ds = eval_auction(q, [1.0e6] * 20, INTRA)
    kinds = [d.kind for d in ds]
    assert kinds == ["auction_anomaly"]
    assert ds[0].payload["baseline_days"] == 20


def test_auction_normal_open_silent():
    assert eval_auction(mkq(price=10.05, prev_close=10.0), [1.0e6] * 20, INTRA) == []


def test_watch_exright_threshold_is_price_eps():
    # 前收与库内收盘差 0.005：小于 price_eps 0.011 → 是浮点噪声不是除权，正常判定
    bars = hist([10.0] * 60)
    ds = eval_watch(mkq(price=10.6, prev_close=10.005, volume_ratio=3.0), bars, 10.0, INTRA, "10:30")
    assert ds and "pct" in ds[0].payload["reason"]
    # 差 0.05 → 超过 price_eps → 整只暂停
    ds = eval_watch(mkq(price=10.6, prev_close=10.05), bars, 10.0, INTRA, "10:30")
    assert ds == []


def test_sector_bad_price_row_skipped():
    ring = MoveRing(300)
    ring.push("new_001", T - timedelta(seconds=310), 100.0)
    rows = [SectorRow("new_001", "x", None, None, 1.0, 1.0, T, members=12)]   # price 缺失
    assert eval_sector(ring, rows, T, INTRA) == []                            # 不抛、不误判
