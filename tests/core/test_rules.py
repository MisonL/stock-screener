"""Task 20：池内票的盘中规则（C1/C2/C3/C′、封板、停牌、除权跳过）。"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import load_config
from core.intraday.curve import VolumeFreezer, curve_at
from core.intraday.rules import Detection, TickState, eval_pool_stock
from core.market import limit_prices
from core.models import Quote

CFG = load_config(Path("config"))
S = CFG.strategy
P = CFG.market("cn_main")
BOX_H, BOX_L = 10.0, 8.0
T0 = datetime(2026, 10, 30, 10, 0)


def q(**kw):
    """默认：现价在箱体内、昨收 9.40、量比字段 1.0、时间 10:00。"""
    base = dict(code="sh600519", name="贵州茅台", price=9.5, prev_close=9.4, open=9.45,
                high=9.9, low=9.4, volume=1000, amount=1.0e7, volume_ratio=1.0,
                limit_up=None, limit_down=None, ts=T0, raw=[])
    base.update(kw)
    return Quote(**base)


def row(**kw):
    base = dict(trade_date=20261029, market="cn_main", code="sh600519", rank=1, score=80.0,
                factors=json.dumps({"b4": 0.41}), days=23, box_high_raw=BOX_H, box_low_raw=BOX_L,
                box_high_adj=BOX_H, box_low_adj=BOX_L, close_raw=9.4, box_vol_mean=1.0e6,
                box_vol_multiple=1.0, a_group_discounted=0, near_breakout=0)
    base.update(kw)
    return SimpleNamespace(**base)


def _ts(hm: str) -> datetime:
    return datetime(2026, 10, 30, int(hm[:2]), int(hm[3:]))


def at(hm: str, *, ratio: float = 1.0, flat: bool = False, row_kw=None, **kw):
    """以 hm 时刻调用；`ratio` 是要达成的横盘放量倍数（volume 反算自量能曲线）。

    先喂一根 10 秒前的「热身」tick：`TickState` 的第一个 tick 只建状态不判定，
    而这一批用例要测的是 C1/C2 本身，不是首帧语义。
    """
    share = curve_at(hm, S.intraday.curve) or 0.19
    volume = kw.pop("volume", None)
    if volume is None:
        volume = ratio * 1.0e6 * share
    now = _ts(hm)
    st, freezer = TickState(), VolumeFreezer()
    base_row = row(**(row_kw or {}))
    price = kw.get("price", 9.5)
    warm = now - timedelta(seconds=10)
    # 热身价默认比现价低 2%：C1 类用例不该顺带触发 stall（滞涨要求 30 分钟内涨幅 <0.5%）
    eval_pool_stock(q(ts=warm, price=price if flat else price * 0.98, volume=volume,
                      prev_close=base_row.close_raw), base_row, P, S,
                    warm.strftime("%H:%M"), st=st, freezer=freezer, now=warm)
    return eval_pool_stock(q(ts=now, volume=volume, **kw), base_row, P, S, hm,
                           st=st, freezer=freezer, now=now)


def kinds(ds):
    return [d.kind for d in ds]


def one(ds):
    assert len(ds) == 1, ds
    return ds[0]


# ---- 除权 / 停牌 ---------------------------------------------------------

def test_exright_day_skips_and_records():          # Review Focus #2
    ds = at("10:30", price=10.5, ratio=3.0, prev_close=11.35)
    assert kinds(ds) == ["data_anomaly"] and ds[0].payload["reason"] == "exright"


def test_halt_suspends_then_first_quote_not_judged():   # Review Focus #3
    st, freezer = TickState(), VolumeFreezer()
    q1 = q(ts=T0, price=BOX_H * 1.05, volume=3.0e6 * curve_at("10:00", S.intraday.curve))
    assert kinds(eval_pool_stock(q1, row(), P, S, "10:00", st=st, freezer=freezer, now=T0)) == []   # primed=False

    later = T0 + timedelta(seconds=61)
    ds = eval_pool_stock(q1, row(), P, S, "10:01", st=st, freezer=freezer, now=later)   # 同一 ts，now 走了 61 秒
    assert kinds(ds) == ["data_anomaly"] and ds[0].payload["reason"] == "halt" and st.halted

    resumed = q(ts=T0 + timedelta(seconds=62), price=BOX_H * 1.09,
                volume=3.0e6 * curve_at("10:00", S.intraday.curve))
    ds = eval_pool_stock(resumed, row(), P, S, "10:01", st=st, freezer=freezer,
                         now=T0 + timedelta(seconds=62))
    assert "breakout" not in kinds(ds)          # 复牌首帧不判


def test_tick_state_primes_on_first_tick():
    st = TickState()
    assert st.primed is False
    eval_pool_stock(q(), row(), P, S, "10:30", st=st, freezer=VolumeFreezer(), now=_ts("10:30"))
    assert st.primed is True


# ---- C1/C2/C3 ------------------------------------------------------------

def test_breakout_needs_above_yesterday_close():   # 昨收已在上沿×1.02，今天价没更高 → 不重复触发
    st, freezer = TickState(), VolumeFreezer()
    volume = 3.0e6 * curve_at("10:30", S.intraday.curve)
    base = row(box_high_raw=10.0, close_raw=10.20)
    eval_pool_stock(q(ts=_ts("10:29"), price=10.00, prev_close=10.20, volume=volume), base, P, S, "10:29",
                    st=st, freezer=freezer, now=_ts("10:29"))
    assert kinds(eval_pool_stock(q(ts=_ts("10:30"), price=10.21, prev_close=10.20, volume=volume),
                                 base, P, S, "10:30", st=st, freezer=freezer, now=_ts("10:30"))) == []


def test_strict_c2_before_10():                    # 09:45–10:00 用 c2_strict=2.5
    assert kinds(at("09:50", price=10.06, ratio=2.5)) == ["breakout"]
    assert kinds(at("09:50", price=10.06, ratio=2.4999)) == []


def test_c2_boundary_is_ge():                      # 测试 F-14：判据是 >= 不是 >
    assert kinds(at("10:05", price=10.06, ratio=1.5)) == ["breakout"]
    assert kinds(at("10:05", price=10.06, ratio=1.4999)) == []
    assert kinds(at("10:05", price=7.9, ratio=1.3)) == ["breakdown"]      # c2_down 1.3 含等号


def test_extended_marked_not_dropped():
    d = one(at("10:30", price=BOX_H * 1.08, ratio=3.0))
    assert d.kind == "breakout" and d.extended


def test_panic_breakdown_emitted():
    d = one(at("10:30", price=BOX_L * 0.90, ratio=1.4))
    assert d.kind == "breakdown" and d.panic


def test_breakout_payload_carries_score_and_limit_up():
    d = one(at("10:30", price=10.06, ratio=3.0))
    assert d.payload["score"] == 80.0
    assert d.payload["limit_up"] == limit_prices(9.4, "cn_main", "normal", 20261030)[0]


def test_near_breakout_inside_band():
    assert "near_breakout" in kinds(at("10:30", price=BOX_H * 0.995, ratio=1.0))
    assert "near_breakout" not in kinds(at("10:30", price=BOX_H * 0.5, ratio=1.0))


def test_box_vol_multiple_is_not_f49():
    d = one(at("10:30", price=10.06, ratio=3.0, volume_ratio=0.5))
    assert d.payload["box_vol_multiple"] == pytest.approx(3.0, abs=0.01)
    assert d.payload["volume_ratio"] == 0.5


# ---- 封板 / stall --------------------------------------------------------

def test_sealed_from_0930():
    up = limit_prices(9.4, "cn_main", "normal", 20261030)[0]
    d = one(at("09:31", price=up, high=up, low=up))
    assert d.kind == "breakout" and d.sealed


def test_limit_prices_half_up_inside_rules():      # 测试 F-03
    assert limit_prices(10.25, "cn_main", "normal", 20261028) == (11.28, 9.23)
    up = limit_prices(10.25, "cn_main", "normal", 20261030)[0]
    d = one(at("09:31", prev_close=10.25, price=up, high=up, low=up,
               row_kw={"close_raw": 10.25, "box_high_raw": 10.30, "box_low_raw": 8.50}))
    assert d.payload["limit_up"] == 11.28


def test_stall_excluded_near_limit():
    up = limit_prices(9.4, "cn_main", "normal", 20261030)[0]
    assert "stall" not in kinds(at("11:00", price=up * 0.998, high=up, low=9.4, ratio=2.0, flat=True))


def test_stall_positive_case():
    ds = at("11:00", price=9.5, ratio=1.6, flat=True)
    assert "stall" in kinds(ds)


def test_stall_volume_below_threshold():
    assert "stall" not in kinds(at("11:00", price=9.5, ratio=1.4, flat=True))


def test_stalled_window_uses_price_ring():
    st, freezer = TickState(), VolumeFreezer()
    hm = "10:00"
    volume = 2.0e6 * curve_at(hm, S.intraday.curve)
    for minutes, price in ((0, 9.40), (5, 9.50), (10, 9.60)):
        eval_pool_stock(q(ts=T0 + timedelta(minutes=minutes), price=price, volume=volume),
                        row(), P, S, hm, st=st, freezer=freezer, now=T0 + timedelta(minutes=minutes))
    ds = eval_pool_stock(q(ts=T0 + timedelta(minutes=11), price=9.61, volume=volume),
                         row(), P, S, hm, st=st, freezer=freezer, now=T0 + timedelta(minutes=11))
    assert "stall" not in kinds(ds)          # 30 分钟内涨了 2%+，不算滞涨


# ---- D6 ------------------------------------------------------------------

def test_pool_detection_watch_false():
    assert at("10:30", price=10.06, ratio=3.0)[0].watch is False


def test_no_halt_judged_before_continuous_trading():
    """**开盘集合竞价期间不能判停牌**：那时上游时间戳本来就静止。

    曾经09:16–09:26 产生 116 条 `reason=halt`，涉及 116 只票、
    每票只 1 条，而且这些票当天都有大量 breakout/stall（确在正常交易）。
    原因是判据「快照 ts 连续 60 秒不变」在集合竞价（09:15–09:25）成立——
    上游此刻不逐笔推送，时间戳自然不动。规格说的是「**盘中**停牌」，
    前提是「此刻本该在动」。
    """
    st, freezer = TickState(), VolumeFreezer()
    q1 = q(ts=T0, price=BOX_H * 1.05, volume=3.0e6 * curve_at("10:00", S.intraday.curve))
    eval_pool_stock(q1, row(), P, S, "09:16", st=st, freezer=freezer, now=T0)      # primed

    # 集合竞价里 ts 静止 61 秒：不该判停牌
    later = T0 + timedelta(seconds=61)
    ds = eval_pool_stock(q1, row(), P, S, "09:17", st=st, freezer=freezer, now=later)
    assert [d.kind for d in ds if d.payload.get("reason") == "halt"] == [], \
        "集合竞价期间把时间戳静止判成了停牌"
    assert not st.halted

    # 连续竞价里同样的静止仍然要判（原有语义不能丢）
    st2, fz2 = TickState(), VolumeFreezer()
    eval_pool_stock(q1, row(), P, S, "10:00", st=st2, freezer=fz2, now=T0)
    ds = eval_pool_stock(q1, row(), P, S, "10:01", st=st2, freezer=fz2,
                         now=T0 + timedelta(seconds=61))
    assert any(d.payload.get("reason") == "halt" for d in ds), "连续竞价里停牌判据失效了"


def test_no_halt_on_first_tick_after_auction():
    """集合竞价期间累积的「时间戳没动」不能带进 09:30 判成停牌。

    `last_changed` 是在竞价窗口里被 `note()` 设上的（09:16 首次见到该票），
    到 09:30 开盘那一下 `now - last_changed` 早已超过 60 秒——就算上游此刻
    刚开始推送，第一笔也会立刻被判停牌。修 `41063e4` 只是把误判**推迟**到 09:30，
    没有消除。正确做法：只在**连续竞价期间**推进 `last_changed` 的计时基准。
    """
    st, freezer = TickState(), VolumeFreezer()
    q1 = q(ts=T0, price=BOX_H * 1.05, volume=3.0e6 * curve_at("10:00", S.intraday.curve))

    # 竞价窗口里先见两笔（ts 不变），第二笔已晚于首笔 120 秒
    eval_pool_stock(q1, row(), P, S, "09:20", st=st, freezer=freezer, now=T0)
    eval_pool_stock(q1, row(), P, S, "09:25", st=st, freezer=freezer,
                    now=T0 + timedelta(seconds=120))

    # 09:30 开盘第一笔：同一 ts，距上一笔只过了 40 秒。
    # 40 < 60 秒阈值，所以「只看 halted 分支」是测不出来的——
    # 只有真的把竞价期间的 `last_changed` 清掉，这条才有意义。
    ds = eval_pool_stock(q1, row(), P, S, "09:30", st=st, freezer=freezer,
                         now=T0 + timedelta(seconds=160))
    assert [d.payload.get("reason") for d in ds if d.kind == "data_anomaly"] == [], \
        "开盘第一笔就把竞价期间累积的静止判成了停牌"
    assert not st.halted


def test_no_halt_on_first_tick_after_lunch_break():
    """午休 11:30–13:00 的静止不能带进 13:00 第一笔判成停牌。

    与开盘同理：`last_changed` 停在 11:29，13:00 第一笔 `now - last_changed`
    是 91 分钟，远超 60 秒阈值。非交易时段必须把基准清空。
    """
    st, freezer = TickState(), VolumeFreezer()
    q1 = q(ts=T0, price=BOX_H * 1.05, volume=3.0e6 * curve_at("10:00", S.intraday.curve))
    eval_pool_stock(q1, row(), P, S, "11:29", st=st, freezer=freezer, now=T0)
    eval_pool_stock(q1, row(), P, S, "12:30", st=st, freezer=freezer,
                    now=T0 + timedelta(seconds=3600))
    ds = eval_pool_stock(q1, row(), P, S, "13:00", st=st, freezer=freezer,
                         now=T0 + timedelta(seconds=5460))
    assert [d.payload.get("reason") for d in ds if d.kind == "data_anomaly"] == [], \
        "午休的静止被带进 13:00 判成了停牌"


def test_no_halt_during_closing_auction():
    """14:57–15:00 是**收盘集合竞价**，时间戳同样静止，不能判停牌。

    `config/markets.yaml` 的 `session.close_time` 是 14:57（`core/clock.py` 注释写明
    「收盘 14:57 不是 15:00」，`jobs/eod.py` 也写明 14:57–15:00 是收盘集合竞价）。
    原实现把下午上界写成 `<= "15:00"`，于是 14:58 的静止快照被判成 halt。
    """
    st, freezer = TickState(), VolumeFreezer()
    base = q(ts=T0, price=BOX_H * 1.05, volume=3.0e6 * curve_at("14:50", S.intraday.curve))
    eval_pool_stock(base, row(), P, S, "14:56", st=st, freezer=freezer, now=T0)
    ds = eval_pool_stock(base, row(), P, S, "14:58", st=st, freezer=freezer,
                         now=T0 + timedelta(seconds=120))
    assert [d.payload.get("reason") for d in ds if d.kind == "data_anomaly"] == [], \
        "收盘集合竞价的静止被判成了停牌"
    assert not st.halted
