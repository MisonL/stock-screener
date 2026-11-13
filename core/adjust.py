"""自算乘法复权因子与 `daily_bars_adj` 重建。

**只存 raw**：`daily_bars` 是唯一事实来源；因子只读 `daily_bars_adj`，读不到就抛错，
不回落 raw（§6.1）。复权公式：`ref = round((前收 − Σ现金)/(1 + Σ送转), 2)`，
`f = 前收/ref`，同日多事件先聚合。

`factor` 是「当日乘到 raw 上得到 adj」的累计乘法因子；`vol_factor` 是成交量的对应缩放
（`1/Π(1+送转)`）——price×f、volume×vol_factor 时成交额不变。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from core.clock import SH
from core.datasource.westock import CorpAction


def ex_ref(prev_close: float, cash: float, bonus: float, tran: float) -> float:
    """除权参考价，**四舍五入**到分（与 `limit_prices` 同一套规则）。

    不用 Python 的 `round()`：它是银行家舍入（`round(10.005, 2) == 10.0`），
    而交易所的参考价是四舍五入；`.xx5` 边界上两者差一分钱，会被下游当成除权事件误判。
    """
    value = (Decimal(str(prev_close)) - Decimal(str(cash))) / (Decimal(1) + Decimal(str(bonus)) + Decimal(str(tran)))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def day_factor(prev_close: float, action: CorpAction) -> float:
    """单一事件的日因子。快照反推的事件直接用记录下来的 `implied_factor`。

    `snapshot_implied` 的 cash/bonus/tran 全是 0，走公式路径恒得 1.0——不显式保存
    反推因子，快照除权会被静默吞掉。
    """
    if action.source == "snapshot_implied":
        return _implied(action)
    return prev_close / ex_ref(prev_close, action.cash_per_share, action.bonus_ratio, action.tran_ratio)


def _implied(action) -> float:
    """快照反推因子必须存在、为正、有限——缺了就是上游没给 f[4]，不能当 1.0 吞掉。"""
    value = action.implied_factor
    if value is None or not (value > 0) or value == float("inf"):
        raise ValueError(f"{action.code} {action.ex_date} 的 snapshot_implied 缺有效 implied_factor")
    return float(value)


def cum_factors(
    actions: list[CorpAction], dates: list[int], prev_closes: dict[int, float]
) -> dict[int, tuple[float, float]]:
    """每个日期 → `(累计价格因子, 累计量因子)`；事件日当日即生效。"""
    by_date: dict[int, list] = {}
    for action in actions:
        by_date.setdefault(action.ex_date, []).append(action)
    ordered = sorted(by_date)

    out: dict[int, tuple[float, float]] = {}
    for date in dates:
        factor = 1.0
        share = 1.0
        for ex_date in ordered:
            if ex_date > date:
                break
            group = by_date[ex_date]
            implied = [a for a in group if a.source == "snapshot_implied"]
            official = [a for a in group if a.source != "snapshot_implied"]
            if official:
                prev = prev_closes.get(ex_date)
                if prev is None:
                    continue          # 缺前收就算不出这次事件的因子，跳过而不是猜
                # 同日多条必须先聚合成一次事件：(P − Σ现金)/(1 + Σ送转)，
                # 各自拿同一个前收算因子再连乘会得到不同的数（10+20 现金：1.3889 vs 1.4286）
                cash = sum(a.cash_per_share for a in official)
                bonus = sum(a.bonus_ratio for a in official)
                tran = sum(a.tran_ratio for a in official)
                factor *= prev / ex_ref(prev, cash, bonus, tran)
                share *= 1 + bonus + tran
            else:
                factor *= _implied(implied[0])
        out[date] = (factor, 1.0 / share)
    return out


def _load_actions(conn, code: str) -> list[CorpAction]:
    """同日两源并存时 `westock_dividend` 优先；`snapshot_implied` 只补前者没有的日期。"""
    rows = conn.execute(
        "SELECT code, ex_date, cash_per_share, bonus_ratio, tran_ratio, source, implied_factor "
        "FROM corporate_actions WHERE code = ? ORDER BY ex_date, source",
        (code,),
    ).fetchall()
    by_date: dict[int, CorpAction] = {}
    for row in rows:
        action = CorpAction(
            code=row[0], ex_date=row[1], cash_per_share=row[2], bonus_ratio=row[3],
            tran_ratio=row[4], source=row[5], implied_factor=row[6],
        )
        current = by_date.get(action.ex_date)
        if current is None or current.source == "snapshot_implied":
            by_date[action.ex_date] = action
    return [by_date[d] for d in sorted(by_date)]


def _hash(actions: list[CorpAction]) -> str:
    payload = json.dumps(
        [[a.ex_date, a.cash_per_share, a.bonus_ratio, a.tran_ratio, a.source, a.implied_factor] for a in actions],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def rebuild_adj(conn, code: str | None = None) -> int:
    """重建 `daily_bars_adj`，返回写入行数。

    **跳过条件是「actions 没变」且「raw 最大日期没变」**（D11）：只新增 raw bar 时不跳过，
    但要只补新增的那几行（`run_eod` 追加当日 bar 后必须立刻能在 adj 里读到）。
    """
    codes = [code] if code else [r[0] for r in conn.execute("SELECT DISTINCT code FROM daily_bars")]
    return sum(_rebuild_one(conn, c) for c in codes)


def _rebuild_one(conn, code: str) -> int:
    raw = conn.execute(
        "SELECT date, open, high, low, close, volume FROM daily_bars WHERE code = ? ORDER BY date",
        (code,),
    ).fetchall()
    if not raw:
        return 0

    actions = _load_actions(conn, code)
    actions_hash = _hash(actions)
    raw_max = raw[-1][0]
    state = conn.execute(
        "SELECT actions_hash, raw_max_date, raw_count, raw_sum FROM adj_state WHERE code = ?",
        (code,)).fetchone()

    # 跳过条件是「actions 没变」且「raw 行集合没变」。只比 raw_max_date 会漏掉
    # 「中间补回一根」：raw 从 [28,30] 变成 [28,29,30] 时最大日仍是 30，缓存却认为无事发生，
    # 结果 raw 三行、adj 两行，整只票以 no_adj 被淘汰。
    raw_count = len(raw)
    raw_sum = sum(d for d, *_ in raw)
    if (state is not None and state[0] == actions_hash and state[1] == raw_max
            and state[2] == raw_count and state[3] == raw_sum):
        return 0                                   # 什么都没变

    existing = {r[0] for r in conn.execute("SELECT date FROM daily_bars_adj WHERE code = ?", (code,))}
    same_actions = state is not None and state[0] == actions_hash

    dates = [r[0] for r in raw]
    prev_closes: dict[int, float] = {}
    by_date = {r[0]: r for r in raw}
    for action in actions:
        earlier = [d for d in dates if d < action.ex_date]
        if earlier:
            prev_closes[action.ex_date] = by_date[max(earlier)][4]

    factors = cum_factors(actions, dates, prev_closes)
    now = datetime.now(SH).isoformat()

    with conn:                                       # 单事务
        if not same_actions:
            conn.execute("DELETE FROM daily_bars_adj WHERE code = ?", (code,))
            targets = dates
        else:
            targets = [d for d in dates if d not in existing]
        for d in targets:
            _, open_, high, low, close, volume = by_date[d]
            factor, vol_factor = factors[d]
            conn.execute(
                "INSERT OR REPLACE INTO daily_bars_adj"
                "(code, date, open, high, low, close, volume, factor, vol_factor) VALUES (?,?,?,?,?,?,?,?,?)",
                (code, d, open_ * factor, high * factor, low * factor, close * factor,
                 volume * vol_factor, factor, vol_factor),
            )
        conn.execute(
            "INSERT INTO adj_state(code, actions_hash, raw_max_date, raw_count, raw_sum, rebuilt_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET actions_hash=excluded.actions_hash, "
            "raw_max_date=excluded.raw_max_date, raw_count=excluded.raw_count, "
            "raw_sum=excluded.raw_sum, rebuilt_at=excluded.rebuilt_at",
            (code, actions_hash, raw_max, raw_count, raw_sum, now),
        )
    return len(targets)


def factors_at(conn, code: str, date: int) -> tuple[float, float]:
    """该日累计因子。**读不到就抛 `KeyError`，不回落 (1, 1)**（§6.1：因子只读 adj）。

    静默回落会让「桥接 raw 价」拿到 1.0，箱顶价直接写成复权价——错得很安静。
    """
    row = conn.execute(
        "SELECT factor, vol_factor FROM daily_bars_adj WHERE code = ? AND date = ?", (code, date)
    ).fetchone()
    if row is None:
        raise KeyError(f"{code} {date} 在 daily_bars_adj 里没有因子行")
    return (row[0], row[1])


def to_raw(price_adj: float, factor: float) -> float:
    """复权价 → raw 价。`daily_bars_adj = raw × factor`，所以这里**除**。"""
    return price_adj / factor


def verify_adj(conn, code: str, qfq=None) -> list[tuple[int, float]]:
    """除权日「自算因子日收益 vs qfq 日收益」的偏差；不给 qfq 数据时返回空列表。

    qfq 由 Task 11 的 `jobs/verify_adj.py` 从腾讯 `qfqday` 取来传进来——本函数不碰网络。
    """
    if not qfq:
        return []
    adj = {r[0]: r[1] for r in conn.execute(
        "SELECT date, close FROM daily_bars_adj WHERE code = ? ORDER BY date", (code,))}
    qfq_map = {b.date: b.close for b in qfq}      # 同日多条取最后一条：上游不该有重复日
    dates = sorted(d for d in adj if d in qfq_map)
    out: list[tuple[int, float]] = []
    for prev, cur in zip(dates, dates[1:]):
        if prev == 0:
            continue
        ours = adj[cur] / adj[prev] - 1
        theirs = qfq_map[cur] / qfq_map[prev] - 1
        out.append((cur, ours - theirs))
    return out
