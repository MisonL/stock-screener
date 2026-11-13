"""观察池个股的盘中判据：C1 突破、C2 量能、C3 追高、C′ 破位、滞涨、封板、停牌、除权跳过。

三条容易写错的执行口径：

1. **除权日不判 C/C′**：`q.prev_close` 与库里的 `close_raw` 差超过 `price_eps`，说明今天有
   除权，快照的价格与「昨天收盘」不同口径，判出来的涨跌幅全是错的（§4.6.9 情形 B）。
2. **停牌看 `now − 快照时间戳`，不是 `Quote.ts` 自比**：同一份 ts 重复返回时自比恒为 0，
   永远判不出停牌。判据用调用方每 tick 传进来的 `now`。
3. **量能比必须过 `freezer`**：14:57 起的累计量不含收盘竞价，继续折算会系统性低估。

`primes` 的口径：`TickState` 的第一次 tick 只建状态不判定——没有上一刻就没有「突破」这个概念。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.clock import default_session
from core.intraday.curve import c2_threshold, projected_full_day
from core.market import classify_market, limit_prices


@dataclass(frozen=True)
class Detection:
    """一条盘中判定。`watch=True` 表示来自自选股（D6：观察池恒 False，salience 靠它区分 1.3/1.0）。"""

    kind: str
    code: str
    payload: dict = field(default_factory=dict)
    extended: bool = False
    panic: bool = False
    sealed: bool = False
    watch: bool = False


@dataclass
class TickState:
    """每只票一个：第一次 tick 只建状态；停牌期间不判；复牌首帧不判。"""

    last_ts: datetime | None = None
    last_changed: datetime | None = None
    halted: bool = False
    resumed_first: bool = False
    primed: bool = False
    prices: deque = field(default_factory=lambda: deque(maxlen=64))

    def note(self, ts: datetime | None, price: float | None, now: datetime) -> None:
        """记录本 tick：时间戳变化才刷新 `last_changed`（停牌时 ts 会停住）。"""
        if ts is not None and (self.last_ts is None or ts != self.last_ts):
            self.last_ts = ts
            self.last_changed = now
        if price is not None:
            self.prices.append((now, price))


def eval_pool_stock(q, row, p, s, hm: str, st: TickState, freezer, now: datetime) -> list[Detection]:
    """观察池个股的判定入口。`p` = `Config.market(m)`，`s` = `Config.strategy`（两个不同对象）。"""
    code = q.code
    market = row.market or classify_market(code) or ""
    st_status = "normal"

    if q.prev_close is not None and row.close_raw is not None:
        if abs(q.prev_close - row.close_raw) > s.universe.price_eps:
            return [Detection("data_anomaly", code, {"reason": "exright", "prev_close": q.prev_close})]

    # **只在连续竞价里推进「时间戳变化」的计时基准**：集合竞价（09:15–09:25）
    # 上游本来就不逐笔推送，时间戳静止是正常现象。规格写的是「**盘中**停牌」，
    # 前提是「此刻本该在动」——漏了这个前提，实测 11-11 开盘 10 分钟内误报 116 条。
    # 只加「连续竞价才判」还不够：`last_changed` 是竞价窗口里设上的，
    # 到 09:30 开盘第一笔 `now - last_changed` 早已超标，误判只是被推迟到开盘那一刻。
    # 所以竞价期间**不推进也不判决**，开盘第一笔重新起算。
    trading = _in_continuous_trading(hm)
    if trading:
        st.note(q.ts, q.price, now)
    else:
        # 非连续竞价（集合竞价 / 午休）：只记价格序列（箱体与量能要用），
        # 并把计时基准**清空**——否则 11:29 的基准会带到 13:00 第一笔，
        # 91 分钟的「静止」立刻被判成停牌（开盘 09:30 同理）。
        if q.price is not None:
            st.prices.append((now, q.price))
        st.last_changed = None
    halted = trading and _is_halted(st, now, s)
    primed = st.primed
    st.primed = True

    if halted:
        st.halted = True
        st.resumed_first = True
        return [Detection("data_anomaly", code, {"reason": "halt"})]
    if st.halted:
        st.halted = False
        st.resumed_first = False
        return []                       # 复牌首帧只作状态切换，不判
    if st.resumed_first:
        st.resumed_first = False
        return []
    if not primed:
        return []

    out: list[Detection] = []
    price = q.price if q.price is not None else row.close_raw
    prev_close = q.prev_close if q.prev_close is not None else row.close_raw
    limits = limit_prices(round(prev_close, 2), market, st_status, _ymd(now))

    sealed = _sealed(q, limits)
    if sealed is not None:
        if sealed == "up":
            out.append(Detection("breakout", code, {"score": row.score, "limit_up": limits[0],
                                                    "box_vol_multiple": None, "volume_ratio": q.volume_ratio},
                                 sealed=True))
        else:
            out.append(Detection("breakdown", code, {"score": row.score, "limit_down": limits[1],
                                                     "box_vol_multiple": None, "volume_ratio": q.volume_ratio},
                                 sealed=True))
        return out

    c2 = c2_threshold(hm, s)
    if c2 is None:
        return out

    multiple = _box_vol_multiple(q, row, hm, s, freezer)
    if multiple is None:
        return out

    base = {"score": row.score, "box_vol_multiple": multiple, "volume_ratio": q.volume_ratio}
    c1_buffer = p.c1_buffer
    c3 = p.c3
    upper = row.box_high_raw

    if price > upper * (1 + c1_buffer) and price > row.close_raw * (1 + s.intraday.c1_over_prev_close) and multiple >= c2:
        payload = dict(base, limit_up=limits[0] if limits else None)
        out.append(Detection("breakout", code, payload, extended=price > upper * (1 + c3)))
    elif price < row.box_low_raw * (1 - c1_buffer) and multiple >= s.intraday.c2_down:
        payload = dict(base, limit_down=limits[1] if limits else None)
        out.append(Detection("breakdown", code, payload, panic=price < row.box_low_raw * (1 - c3)))
    elif upper * (1 - p.near_breakout) <= price <= upper * (1 + c1_buffer):
        out.append(Detection("near_breakout", code, dict(base)))

    # 已经突破/破位的同一 tick 不算「滞涨」——价刚动过，滞涨的前提不成立
    if all(d.kind not in ("breakout", "breakdown") for d in out) and _is_stall(
        st, price, multiple, limits, now, s
    ):
        out.append(Detection("stall", code, dict(base)))
    return out


def _in_continuous_trading(hm: str) -> bool:
    """`hm` 是否落在**连续竞价**时段（不含开盘/收盘集合竞价与午休）。

    - 上午：`open_time`（09:30）– 11:30 半开
    - 下午：13:00 – `close_time`（14:57）**半开**

    14:57–15:00 是收盘集合竞价（`config/markets.yaml` 的 `session.close_time`，
    `core/clock.py` 与 `jobs/eod.py` 都写明收盘是 14:57 不是 15:00），
    这段时间上游时间戳同样静止，判停牌会误报。
    时刻取自配置，不在本模块另写一套常量。字符串比较即可：`hm` 是 `HH:MM` 定宽。
    """
    open_time, close_time = default_session()
    return (open_time <= hm < "11:30") or ("13:00" <= hm < close_time)


def _is_halted(st: TickState, now: datetime, s) -> bool:
    if st.last_changed is None:
        return False
    return (now - st.last_changed).total_seconds() >= s.intraday.halt_stale_s


def _sealed(q, limits) -> str | None:
    """一字板：四价相同且收在涨跌停价上（§4.6.6 独立通道，不受 09:45 门槛约束）。"""
    if limits is None:
        return None
    high, low, price = q.high, q.low, q.price
    if high is None or low is None or price is None:
        return None
    if round(high, 2) != round(low, 2) or round(price, 2) != round(high, 2):
        return None
    if abs(price - limits[0]) <= 0.01:
        return "up"
    if abs(price - limits[1]) <= 0.01:
        return "down"
    return None


def _box_vol_multiple(q, row, hm: str, s, freezer) -> float | None:
    if q.volume is None or not row.box_vol_mean:
        return None
    projected = projected_full_day(q.volume, hm, s.intraday.curve)
    if projected is None:
        return None
    return freezer.ratio(q.code, hm, projected / row.box_vol_mean)


def _is_stall(st: TickState, price: float, multiple: float, limits, now: datetime, s) -> bool:
    cfg = s.intraday.stall
    if multiple < cfg.vol:
        return False
    if limits is not None:
        room_up = (limits[0] - price) / limits[0]
        room_down = (price - limits[1]) / limits[1]
        if min(room_up, room_down) <= cfg.limit_gap:
            return False
    window = now - timedelta(minutes=cfg.window_min)
    past = [px for ts, px in st.prices if ts >= window]
    if not past:
        return False
    low = min(past)
    if low <= 0:
        return False
    return (price - low) / low < cfg.max_gain


def _ymd(dt: datetime) -> int:
    return int(dt.strftime("%Y%m%d"))
