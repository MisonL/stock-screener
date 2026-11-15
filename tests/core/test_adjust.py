"""自算乘法复权因子与 adj 表重建。"""
import pytest

from core.adjust import cum_factors, day_factor, ex_ref, factors_at, rebuild_adj
from core.datasource.westock import CorpAction
from tests.helpers import append_daily_bar, seed_daily_bars

action_20250729 = CorpAction("sh600519", 20250729, cash_per_share=3.974, bonus_ratio=0.8, tran_ratio=1.2)   # 10转12送8派39.74
action_20251030_cash = CorpAction("sh600519", 20251030, cash_per_share=2.49, bonus_ratio=0.0, tran_ratio=0.0)
PREV = {20250729: 337.00, 20251030: 1011.00}              # ex_date → 前一交易日 raw 收盘
cash_factor = day_factor(PREV[20251030], action_20251030_cash)


def test_ex_ref_rounds_to_2dp():
    assert ex_ref(11.60, 0.249, 0.0, 0.0) == 11.35       # 平安银行 2026-09-24（示例前收）
    assert ex_ref(337.00, 3.974, 0.8, 1.2) == 111.01     # 比亚迪 2025-07-29


def test_factor_direction():
    # spec §6.1.2.1 的 3.0358 用的是未取整的 111.0087；ref 取 2 位小数后实际是 3.03576
    assert abs(day_factor(337.00, action_20250729) - 3.0358) < 1e-3


def test_cum_factor_and_vol_factor():
    assert cum_factors([action_20250729], [20250728, 20250729, 20251028], PREV) == {
        20250728: (1.0, 1.0),
        20250729: pytest.approx((3.0358, 1 / 3), abs=1e-3),
        20251028: pytest.approx((3.0358, 1 / 3), abs=1e-3),
    }


def test_two_events_vol_factor_only_on_share_change():   # 测试 F-01：派息不改股本
    f, v = cum_factors([action_20250729, action_20251030_cash], [20251030], PREV)[20251030]
    assert v == pytest.approx(1 / 3, abs=1e-6)
    assert f == pytest.approx(3.0358 * cash_factor, abs=1e-3)


def test_snapshot_implied_uses_recorded_factor():
    implied = CorpAction("sh600519", 20251030, cash_per_share=0.0, bonus_ratio=0.0, tran_ratio=0.0,
                         source="snapshot_implied", implied_factor=1.25)
    # cash/bonus/tran 全 0 的公式路径恒为 1，必须走显式保存的反推因子
    assert day_factor(1011.00, implied) == 1.25
    assert cum_factors([implied], [20251030], {})[20251030][0] == pytest.approx(1.25)


def test_non_event_days_factor_is_one(db):   # 测试 F-02：可执行形态
    seed_daily_bars(db, "sh600519", 1688, last_date=20250728, last_close=337.00)
    seed_daily_bars(db, "sh600519", 5, start_date=20250729, close=340.00)
    db.execute("INSERT INTO corporate_actions VALUES ('sh600519', 20250729, 'westock_dividend', 3.974, 0.8, 1.2, NULL)")
    db.commit()
    rebuild_adj(db, "sh600519")
    dates = [r[0] for r in db.execute("SELECT date FROM daily_bars WHERE code='sh600519'")]
    assert any(d >= 20250729 for d in dates)
    for d in dates:
        if d < 20250729:
            assert factors_at(db, "sh600519", d)[0] == 1.0
        else:
            assert factors_at(db, "sh600519", d)[0] == pytest.approx(3.0358, abs=1e-3)


def test_same_day_events_equivalent_to_aggregate():
    """同日两事件先聚合 vs 两个日因子相乘，结果必须一致（差 2 倍是这个 bug 的典型症状）。"""
    cash = CorpAction("sh600519", 20251030, cash_per_share=0.249, bonus_ratio=0.0, tran_ratio=0.0)
    split = CorpAction("sh600519", 20251030, cash_per_share=0.0, bonus_ratio=0.8, tran_ratio=1.2)
    combined = CorpAction("sh600519", 20251030, cash_per_share=0.249, bonus_ratio=0.8, tran_ratio=1.2)
    prev = {20251030: 100.0}
    one = cum_factors([combined], [20251030], prev)[20251030]
    two = cum_factors([cash, split], [20251030], prev)[20251030]
    # 参考价量化到分带来的固有偏差 ~1e-4（§4.6.9 自己记录过 3.0358 vs 3.03576）
    assert two[0] == pytest.approx(one[0], rel=1e-3) and two[1] == pytest.approx(one[1])


def test_rebuild_skips_only_when_nothing_changed(db):   # D11
    seed_daily_bars(db, "sh600519", 10)
    assert rebuild_adj(db, "sh600519") == 10
    assert rebuild_adj(db, "sh600519") == 0
    append_daily_bar(db, "sh600519", date=20261030)
    assert rebuild_adj(db, "sh600519") == 1


def test_rebuild_rewrites_when_actions_change(db):
    seed_daily_bars(db, "sh600519", 10, last_date=20250728, last_close=337.00)
    rebuild_adj(db, "sh600519")
    db.execute("INSERT INTO corporate_actions VALUES ('sh600519', 20250729, 'westock_dividend', 3.974, 0.8, 1.2, NULL)")
    db.commit()
    assert rebuild_adj(db, "sh600519") == 10     # 因子变了要整只重写
    assert factors_at(db, "sh600519", 20250728)[0] == 1.0


def test_rebuild_all_codes_when_code_none(db):
    seed_daily_bars(db, "sh600519", 3)
    seed_daily_bars(db, "sz000001", 4)
    assert rebuild_adj(db) == 7


def test_to_raw_uses_library_adj_close(db):
    # 库内 adj = raw × factor，所以 raw = adj ÷ factor；拿自行构造的值做往返会把方向写反
    seed_daily_bars(db, "sh600519", 2, last_date=20250728, last_close=337.00)
    seed_daily_bars(db, "sh600519", 1, start_date=20250729, close=340.00)
    db.execute("INSERT INTO corporate_actions VALUES ('sh600519', 20250729, 'westock_dividend', 3.974, 0.8, 1.2, NULL)")
    db.commit()
    rebuild_adj(db, "sh600519")
    from core.adjust import to_raw

    adj_close, factor = db.execute(
        "SELECT close, factor FROM daily_bars_adj WHERE code='sh600519' AND date=20250729").fetchone()
    assert adj_close == pytest.approx(340.00 * factor)
    assert to_raw(adj_close, factor) == pytest.approx(340.00)


def test_ex_ref_rounds_half_up():
    assert ex_ref(10.005, 0.0, 0.0, 0.0) == 10.01       # round(10.005, 2) 是 10.0（银行家舍入）
    assert ex_ref(10.015, 0.0, 0.0, 0.0) == 10.02


def test_factors_at_raises_when_missing(db):
    seed_daily_bars(db, "sh600519", 3)
    rebuild_adj(db, "sh600519")
    with pytest.raises(KeyError):
        factors_at(db, "sh600519", 20990101)


def test_same_day_two_cash_events_aggregate():
    # 前收 100，同日两笔现金 10 与 20：正确因子 = 100/70 = 1.42857
    # 各自拿同一个前收算再连乘 = (100/90)×(100/80) = 1.3889 —— 这个差别就是「差 2 倍」的近亲
    a = CorpAction("sh600519", 20251030, 10.0, 0.0, 0.0)
    b = CorpAction("sh600519", 20251030, 20.0, 0.0, 0.0)
    one = cum_factors([a, b], [20251030], {20251030: 100.0})[20251030]
    assert one[0] == pytest.approx(100.0 / 70.0, rel=1e-4)


def test_same_day_two_splits_aggregate():
    a = CorpAction("sh600519", 20251030, 0.0, 0.8, 0.0)
    b = CorpAction("sh600519", 20251030, 0.0, 0.0, 1.2)
    one = cum_factors([a, b], [20251030], {20251030: 100.0})[20251030]
    assert one[1] == pytest.approx(1.0 / 3.0)          # 量因子：1/(1+0.8+1.2)


def test_implied_factor_missing_raises():
    bad = CorpAction("sh600519", 20251030, 0.0, 0.0, 0.0, source="snapshot_implied", implied_factor=None)
    with pytest.raises(ValueError):
        cum_factors([bad], [20251030], {})


def test_rebuild_after_mid_backfill(db):
    """raw 从 [28,30] 补回 29 变三行时必须真的重建 adj（只比 max_date 会漏掉）。"""
    seed_daily_bars(db, "sh600519", 2, last_date=20251028, last_close=10.0)
    seed_daily_bars(db, "sh600519", 1, start_date=20251030, close=10.5)
    db.commit()
    rebuild_adj(db, "sh600519")
    # 现状：raw = [10-27, 10-28, 10-30]，中间缺 29
    assert db.execute("SELECT count(*) FROM daily_bars_adj WHERE code='sh600519'").fetchone()[0] == 3

    # 补回 10-29 那根（eod 缺口补拉的场景），actions 没变、max_date 没变
    db.execute("INSERT OR REPLACE INTO daily_bars VALUES ('sh600519', 20251029, 10.2, 10.2, 10.2, 10.2, 1e6, 1e7, 'backfill')")
    db.commit()
    rebuild_adj(db, "sh600519")
    raw = [r[0] for r in db.execute("SELECT date FROM daily_bars WHERE code='sh600519' ORDER BY date")]
    adj = [r[0] for r in db.execute("SELECT date FROM daily_bars_adj WHERE code='sh600519' ORDER BY date")]
    assert raw == adj == [20251027, 20251028, 20251029, 20251030]


def test_rebuild_idempotent(db):
    """没变化时重复 rebuild 必须是 0 行写入（跳过条件生效）。"""
    seed_daily_bars(db, "sh600519", 5)
    rebuild_adj(db, "sh600519")
    assert rebuild_adj(db, "sh600519") == 0


# ---- 复权成交量必须逐行对齐（历史盲区：漏乘 vol_factor 时全绿）-----------


def test_adj_volume_equals_raw_times_vol_factor(db):
    """`daily_bars_adj.volume` 必须等于 `raw.volume × vol_factor`，**逐行**。

    旧测试只核对日期行数和 `close × factor`，把 `core/adjust.py` 的
    `volume * vol_factor` 改成 `volume`（漏乘）之后 461 个测试**全绿**——
    量比、放量检测会静默偏掉，箱体判定跟着错。
    """
    # 除权日必须落在**已有 bar 之后，且后面还有 bar**，否则因子算不出来（全是 1.0）
    seed_daily_bars(db, "sh600519", 3, last_date=20250724, last_close=340.00)
    db.execute("INSERT INTO corporate_actions VALUES ('sh600519', 20250725, 'westock_dividend', 0.0, 1.2, 0.0, NULL)")
    db.commit()
    seed_daily_bars(db, "sh600519", 3, start_date=20250728, close=200.00)
    rebuild_adj(db, "sh600519")

    raw = db.execute("SELECT date, volume FROM daily_bars WHERE code='sh600519' ORDER BY date"
                     ).fetchall()
    adj = db.execute("SELECT date, volume, factor, vol_factor FROM daily_bars_adj"
                     " WHERE code='sh600519' ORDER BY date").fetchall()
    assert len(raw) == len(adj)
    for (d_r, vol_r), (d_a, vol_a, factor, vol_f) in zip(raw, adj):
        assert d_r == d_a
        assert abs(vol_a - vol_r * vol_f) < 1e-6, (
            f"{d_r}: adj.volume={vol_a} 应为 raw.volume({vol_r}) × vol_factor({vol_f})")
        # **只有送转**时两因子互为倒数（价格÷、量×）；含现金分红时 price 因子
        # 还含常数项，不互为倒数，所以这里只断言 vol_factor 与「送转比」一致
        assert vol_f <= 1.0 + 1e-9, "vol_factor 应 ≤ 1（除权后成交量放大）"
