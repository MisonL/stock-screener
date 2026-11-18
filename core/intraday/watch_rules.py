"""自选股 / 指数 / 板块 / 集合竞价四条盘中规则（§5.3）。

阈值全部来自 `intraday.watch / index_move / sector_move / auction`。

**这里的 `p` 是 `CFG.strategy.intraday`**（不是 `Config.market(m)`）——自选股与大盘规则
的参数全在 `intraday.*` 下，与观察池的 `by_market` 参数不共用同一个对象。
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Iterable, Mapping

from core.intraday.rules import Detection
from core.market import classify_market


class MoveRing:
    """近 `window_s` 秒的价格环：`.change()` 与「≥ window 秒前最近一个样本」比。"""

    def __init__(self, window_s: int):
        self.window_s = window_s
        self._buf: dict[str, deque] = {}

    def push(self, key: str, ts: datetime, value: float) -> None:
        buf = self._buf.setdefault(key, deque(maxlen=512))
        if buf and buf[-1][0] > ts:
            return                      # 乱序样本丢弃：窗口是单调时间轴
        buf.append((ts, value))

    def change(self, key: str, now: datetime) -> float | None:
        """相对涨跌幅；`None` = 窗口内没有足够老的样本（不判）。"""
        buf = self._buf.get(key)
        if not buf:
            return None
        cutoff = now.timestamp() - self.window_s
        base = None
        for ts, value in buf:            # 取「≥ window 秒前」里最新的那个
            if ts.timestamp() <= cutoff:
                base = value
            else:
                break
        latest = buf[-1][1]
        if base is None or not base:
            return None
        return latest / base - 1


def eval_watch(q, hist60, ma20_prev: float, p, hm: str, pct_threshold: float | None = None,
               price_eps: float = 0.011) -> list[Detection]:
    """自选股规则。`pct_threshold`（`watch_stock.pct_threshold`）非空时覆盖按市场的默认值。"""
    if hm < p.judge_start:
        return []
    prev = q.prev_close
    if prev is None or q.price is None:
        return []
    # 当日除权：`f[4]` 与库里的收盘差超过 price_eps → 口径不同，涨跌幅全是错的 → 整只暂停。
    # 用 `universe.price_eps`（0.011，浮点噪声 ~1e-4 远小于它），不能拿 1e-9 当阈值——
    # 那会把「上游给的前收多了半分钱」的票整只静音。
    if hist60 and abs(prev - hist60[-1].close) > price_eps:
        return []
    if pct_threshold is None:
        pct_threshold = p.watch.pct.get(classify_market(q.code) or "cn_main", 0.05)
    if prev <= 0:
        return []
    chg = q.price / prev - 1
    reasons = []
    if abs(chg) >= pct_threshold:
        reasons.append("pct")
    if q.volume_ratio is not None and q.volume_ratio >= p.watch.vol_ratio:
        reasons.append("vol")
    if hist60:
        recent = hist60[-p.watch.extreme_days:]
        high = max(b.high for b in recent)
        low = min(b.low for b in recent)
        # 只在「现价已在极值之内（距离非负）」时算贴近，跌破前低不叫「近 60 日低」
        gap_high = (high - q.price) / high if high > 0 else -1
        gap_low = (q.price - low) / low if low > 0 else -1
        if 0 <= gap_high <= p.watch.near_extreme:
            reasons.append("near_high")
        if 0 <= gap_low <= p.watch.near_extreme:
            reasons.append("near_low")
    if ma20_prev and _crossed(q, hist60, ma20_prev):
        reasons.append("ma20")
    if not reasons:
        return []
    return [Detection("watch_rule", q.code, {"reason": "+".join(reasons), "chg": chg}, watch=True)]


def _crossed(q, hist60, ma20_prev: float) -> bool:
    """现价穿越 MA20：与前一日均线分列两侧。"""
    if not hist60:
        return False
    return (q.price - ma20_prev) * (hist60[-1].close - ma20_prev) < 0


def eval_index(ring: MoveRing, prices, now: datetime, p) -> list[Detection]:
    """指数异动：`window_s` 内变动 ≥ `threshold`。

    `prices` 既可以是「代码 → 现价」的映射（本函数顺便入环），也可以是代码列表
    （环里已有样本，由调用方推入）——三循环与单测的两种调用方式都支持。
    """
    out = []
    threshold = p.index_move.threshold
    if isinstance(prices, Mapping):
        for code, price in prices.items():
            ring.push(code, now, price)
        codes = list(prices)
    else:
        codes = list(prices or p.index_move.codes)
    for code in codes:
        chg = ring.change(code, now)
        if chg is not None and abs(chg) >= threshold:
            out.append(Detection("index_move", code, {"chg": chg, "scope": "market"}))
    return out


def eval_sector(ring: MoveRing, rows: Iterable, now: datetime, p) -> list[Detection]:
    """板块异动：成分股数不足 `min_members` 的板块直接跳过（小样本板块噪音大）。"""
    out = []
    threshold = p.sector_move.threshold
    for row in rows:
        if getattr(row, "chg", None) is None:
            continue
        price = row.price
        if price is None or not (price > 0) or price == float("inf"):
            continue                              # 坏行不入环：None/inf 都会在 change() 里造出假事件
        ring.push(row.code, now, price)
        chg = ring.change(row.code, now)
        if chg is None or abs(chg) < threshold:
            continue
        if _members(row) < p.sector_move.min_members:
            continue
        out.append(Detection("sector_move", row.code, {"chg": chg, "scope": "sector"}))
    return out


def _members(row) -> int:
    value = getattr(row, "members", None)
    if value is not None:
        return int(value)
    return len(getattr(row, "codes", ()) or ())


def eval_auction(q, baseline_20d: list[float] | None, p, in_focus: bool = True) -> list[Detection]:
    """集合竞价：高低开 ±`gap`，或竞价量 ≥ 20 日竞价均量 × `vol_mult`。

    **基线不足 `baseline_days` 时仍然报高低开**，只是不做量能异常判定——早盘最值得知道的
    就是高开/低开，因为撮合基线没攒够就把整条规则关掉是过度保守。

    **池外的票只报竞价量异常，不报高低开**（`in_focus=False`）：
    全市场 5000 多只票里每天天然有几百只涨跌超过 2%，把它们全都算作"竞价异动"
    等于制造噪声——实测一天 860 条，其中**没有一条**是量能异常，全是单纯高低开，
    且没有一条属于自选。这类事件把 `salience` 分布整个压死（97.6% 被抑制），
    阈值怎么调都调不出来。池内的票照常报高低开：那才是你会关心的。
    """
    if q.prev_close is None or q.price is None or q.prev_close <= 0:
        return []
    cfg = p.auction
    chg = q.price / q.prev_close - 1
    days = len(baseline_20d or ())
    reasons = []
    if in_focus and abs(chg) >= cfg.gap:
        reasons.append("high_open" if chg > 0 else "low_open")
    if days >= cfg.baseline_days and q.volume is not None:
        mean = sum(baseline_20d) / days
        if mean > 0 and q.volume >= mean * cfg.vol_mult:
            reasons.append("auction_vol")
    if not reasons:
        return []
    return [Detection("auction_anomaly", q.code,
                      {"reason": "+".join(reasons), "chg": chg, "baseline_days": days})]
