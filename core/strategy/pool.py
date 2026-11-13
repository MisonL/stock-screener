"""观察池生成（spec §4.4.1 排除 + §4.6 因子 + §4.3 排序 + §9.1 内存上限）。

一次出池分两趟，**不是**为提速，是为控内存（§9.1：一次把 5577 只 × 1700 根全读进内存约
2 GB，`jobs` 的 1 GB 上限扛不住）：

1. **纯 SQL 粗筛**（`_coarse_screen`）：只碰 `stock_meta` / `stock_stats` /
   `stock_status_history`，不加载任何 bar 序列——有效 bar 数、20 日均额、ST、停牌天数四道
   门槛在 SQL 里先剔一遍。`stock_stats` 是物化表，由 `jobs/after_close.py` 出池前
   `refresh_stock_stats()` 刷一次；表为空时这里自己也刷一次（否则粗筛读到空表、退回全量）。
2. **按 500 只分批精算**：只对幸存者 `annotate → find_box → decline_window →
   compute_factors → score`，每批算完即丢，峰值内存只与批次大小有关。

**`as_of` 截断所有序列到该日（含）**：粗筛的 `count(*)` / 20 日均额、精算的 bar 序列、
指数序列、当日因子全部按 `date <= as_of` 取——回放历史日期时不能用到未来数据。

**`candidates` 与 `size` 必须分开**（brief 的硬要求）：`candidates` 是过完全部门槛、参与
排序的只数（截断前），`size = min(candidates, pool_top_n)` 才写进 `pool` 表。池子告警按
`candidates` 判：拿 `size` 判，溢出永远显示成 `pool_top_n`，`pool_overflow` 永不触发。
"""
from __future__ import annotations

import json
import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from core.adjust import ex_ref
from core.clock import TradingCalendar
from core.models import Bar, Reject
from core.strategy.box import find_box
from core.strategy.factors import compute_factors
from core.strategy.filters import exclude
from core.strategy.scoring import score
from core.strategy.window import annotate, decline_window

log = logging.getLogger(__name__)

# 分批加载 bar 的只数上限：决定峰值内存（500 × 1700 根 ≈ 数万行，远低于 1 GB）。
_BATCH = 500
# 「近 20 日均额」与「近 250 个交易日」都按**有 bar 的交易日**数（§4.4.1、§4.6.6）。
_AMOUNT_DAYS = 20
_HIGH_LOOKBACK = 250

# 逐只精算的漏斗阶段（顺序固定；被某一阶段淘汰的票要计入它**之前**各阶段的通过数）。
_PER_CODE = ("data", "exright", "box", "box_amount", "decline", "B8", "B4", "A")

_INDEX_CODE = "sh000300"


@dataclass(frozen=True)
class PoolReport:
    """一次出池的结果。

    `candidates` = 过完全部门槛、参与排序的只数（截断前）；`size` = 实际写进 `pool` 表的
    行数（`min(candidates, pool_top_n)`）；`funnel` 各阶段只到 `candidates` 为止；
    `rejects` 是 `code → (阶段, 理由)`；`alerts` 是 `pool_low` / `pool_overflow`。
    """

    market: str
    candidates: int
    size: int
    funnel: dict[str, int]
    rejects: dict[str, tuple[str, str]]
    alerts: list[str]


def refresh_stock_stats(conn, as_of: int) -> int:
    """物化粗筛字段（`n_bars` / `amount_20d` / `suspended_250`），返回表内行数。

    两段 SQL，**不能合成一句**：`suspended_250` 要按「交易日」算，只能从
    `suspension_calendar` 与交易日历的交集得出，`GROUP BY daily_bars` 算不出来。

    第一段的两个坑（都会让整表刷新失败或全灭）：
    - `amount_20d` 必须 `COALESCE(..., 0)`——最近 20 日一根 bar 都没有的票（长停牌、
      退市整理）`avg(CASE …)` 是 NULL，而该列是 `REAL NOT NULL`；
    - `n_bars` 必须无条件 `count(*)`——加 `WHERE date >= :d20` 会把每只都数成 20，
      `n_bars >= min_bars` 全灭。
    """
    cal = TradingCalendar(conn)
    days = [d for d in sorted(cal.days) if d <= as_of]
    if not days:
        return conn.execute("SELECT count(*) FROM stock_stats").fetchone()[0]

    d20 = days[-_AMOUNT_DAYS] if len(days) >= _AMOUNT_DAYS else days[0]
    window = days[-_HIGH_LOOKBACK:]
    first = window[0]

    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO stock_stats(code, n_bars, amount_20d, suspended_250, updated_at) "
            "SELECT code, count(*), COALESCE(avg(CASE WHEN date >= ? THEN amount END), 0), 0, "
            "datetime('now') FROM daily_bars WHERE date <= ? GROUP BY code",
            (d20, as_of),
        )
        for code, start, end in conn.execute(
            "SELECT code, start_date, end_date FROM suspension_calendar"
        ).fetchall():
            finish = as_of if end is None else min(int(end), as_of)
            lo = max(int(start), first)
            if finish < lo:
                continue
            n = bisect_right(window, finish) - bisect_left(window, lo)
            if n <= 0:
                continue
            conn.execute("UPDATE stock_stats SET suspended_250 = ? WHERE code = ?", (n, code))
    return conn.execute("SELECT count(*) FROM stock_stats").fetchone()[0]


def build_pool(conn, cfg, market: str, as_of: int) -> PoolReport:
    """给 `market` 出一份 `as_of` 日的观察池，写 `pool` 表并返回 `PoolReport`。"""
    p = cfg.market(market)
    s = cfg.strategy
    u = s.universe

    if conn.execute("SELECT 1 FROM stock_stats LIMIT 1").fetchone() is None:
        # 没刷过物化表 = 「纯 SQL 过滤」会读到空表 → 这里补一次，不退回全量加载
        refresh_stock_stats(conn, as_of)

    funnel: dict[str, int] = {}
    rejects: dict[str, tuple[str, str]] = {}
    codes, stats = _coarse_screen(conn, market, as_of, p, u, funnel, rejects)
    for stage in _PER_CODE:
        funnel[stage] = 0

    cal = TradingCalendar(conn)
    index_rows = _load_index(conn, as_of)
    ranked: list[dict] = []

    for start in range(0, len(codes), _BATCH):
        chunk = codes[start : start + _BATCH]
        bars = _load_bars(conn, chunk, as_of)
        adj = _load_adj(conn, chunk, as_of)
        factors = _load_factors(conn, chunk, as_of)
        actions = _load_actions(conn, chunk)
        susp = _load_suspensions(conn, chunk)
        status = _load_status(conn, chunk)

        for code in chunk:
            outcome = _evaluate(code, market, as_of, p, s, u, cal, index_rows,
                                bars.get(code, []), adj.get(code, []), factors.get(code),
                                actions.get(code, []), susp.get(code, []),
                                status.get(code, []), stats[code])
            if isinstance(outcome, tuple):
                key, reason = outcome
                rejects[code] = (_reject_stage(key), reason)
                for passed in _PER_CODE[: _PER_CODE.index(key)]:
                    funnel[passed] += 1
                continue
            for passed in _PER_CODE:
                funnel[passed] += 1
            outcome["code"] = code
            ranked.append(outcome)

    ranked.sort(key=lambda item: (-item["score"].score, item["code"]))
    top = ranked[: int(u.pool_top_n)]
    _write_pool(conn, market, as_of, top)

    alerts: list[str] = []
    if len(ranked) < int(u.pool_alert_min):
        alerts.append("pool_low")
    if len(ranked) > int(u.pool_overflow):
        alerts.append("pool_overflow")

    log.info(
        "pool %s as_of=%s candidates=%d size=%d alerts=%s",
        market, as_of, len(ranked), len(top), alerts or "-",
    )
    return PoolReport(
        market=market,
        candidates=len(ranked),
        size=len(top),
        funnel=funnel,
        rejects=rejects,
        alerts=alerts,
    )


# ---------------------------------------------------------------- 第一趟：纯 SQL 粗筛

# 五道粗筛门槛的 SQL 片段按顺序累积：每一阶段都带上前面所有谓词，得到「过了这一关」的集合，
# 相邻两个集合的差就是这一关淘汰的票（漏斗与 rejects 因此永远自洽）。
_STAGES: tuple[tuple[str, str, str], ...] = (
    ("b_share", " AND NOT (m.code LIKE 'sh900%' OR m.code LIKE 'sz200%')", "b_share"),
    ("bars", " AND COALESCE(ss.n_bars, 0) >= :min_bars", "bars"),
    ("amount", " AND ss.amount_20d >= :min_amount", "amount_20d"),
    ("st", " AND COALESCE(h.st_status, 'normal') = 'normal'", "st"),
    ("suspended", " AND COALESCE(ss.suspended_250, 0) <= :suspend_max", "suspended"),
)

_JOIN = (
    " FROM stock_meta m"
    " LEFT JOIN stock_stats ss ON ss.code = m.code"
    " LEFT JOIN stock_status_history h ON h.code = m.code AND h.start_date <= :as_of"
    "   AND (h.end_date IS NULL OR h.end_date >= :as_of)"
    " WHERE m.market = :market"
)


def _coarse_screen(conn, market, as_of, p, u, funnel, rejects):
    """第一趟：不加载 bar 序列，只凭物化字段把 5577 只压到几百只。"""
    params = {
        "market": market,
        "as_of": as_of,
        "min_bars": int(u.min_bars),
        "min_amount": float(p.min_amount_20d),
        "suspend_max": int(u.suspend_max_in_250),
    }
    funnel["meta"] = conn.execute(
        "SELECT count(*) FROM stock_meta WHERE market = ?", (market,)
    ).fetchone()[0]

    sql = (
        "SELECT m.code AS code, m.name AS name, COALESCE(h.st_status, 'normal') AS st,"
        " COALESCE(ss.n_bars, 0) AS n_bars, COALESCE(ss.amount_20d, 0) AS amount_20d,"
        " COALESCE(ss.suspended_250, 0) AS suspended_250"
        + _JOIN
    )
    rows = {row["code"]: row for row in conn.execute(sql, params)}

    predicates = ""
    for stage, predicate, reason in _STAGES:
        predicates += predicate          # 谓词累积：每一关都带着前面所有关
        kept = {row["code"]: row for row in conn.execute(sql + predicates, params)}
        for code in rows.keys() - kept.keys():
            rejects[code] = (stage, reason)
        rows = kept
        funnel[stage] = len(rows)

    return sorted(rows), rows


# ---------------------------------------------------------------- 第二趟：分批精算


def _evaluate(code, market, as_of, p, s, u, cal, index_rows,
              raw, adj, factor_row, actions, susp, status, stats):
    """一只票的整条精算链；返回载荷 dict，或 `(stage, reason)` 表示淘汰。"""
    if not raw or len(adj) != len(raw):
        # 只存 raw，因子只读 adj 且不回落 raw（§6.1）；缺行是数据问题，单只淘汰不拖垮整池
        return ("data", "no_adj")
    if any(int(a["ex_date"]) == as_of for a in actions):
        return ("exright", "exright")

    if factor_row is None:
        # 当日因子行缺失：raw/adj 桥接会拿 1.0 当因子，箱顶价直接写成复权价（错得很安静）
        return ("data", "no_factor")
    factor, vol_factor = factor_row
    if factor <= 0 or vol_factor <= 0:
        return ("data", "bad_factor")

    rows = annotate(
        raw, adj, market,
        _st_lookup(status),
        _ex_refs(raw, actions),
        _resume_dates(susp, cal, as_of),
    )

    box = find_box(rows, p, s.box)
    if isinstance(box, Reject):
        return (box.stage, box.reason)

    window = rows[box.start : box.start + box.n]
    box_amount = sum(r.amount for r in window) / box.n
    reason = exclude(
        code, {"name": stats["name"], "st_status": stats["st"]},
        stats["n_bars"], stats["amount_20d"], box_amount, stats["suspended_250"],
        stats["st"], p, u,
    )
    if reason is not None:
        # 走到这里只可能是 box_amount（其余各条已在第一趟 SQL 里剔过）
        return ("box_amount", reason)

    decl = decline_window(rows, box.start, s.decline.tail_max)
    if decl is None:
        return ("decline", "no_decline")

    fv = compute_factors(rows, box, decl, index_rows, p, s)
    if isinstance(fv, Reject):
        return (fv.stage, fv.reason)
    if fv.box_vol_mean_adj <= 0:
        # C2 的分母：写进 pool 后盘中每个 tick 都要除它，零值会让整条池循环抛异常
        return ("B4", "zero_box_vol")

    return {
        "score": score(fv, p, s),
        "box": box,
        "fv": fv,
        "close_raw": rows[-1].raw_c,
        "factor": factor,
        "vol_factor": vol_factor,
    }


def _reject_stage(key: str) -> str:
    """精算阶段的内部键 → `rejects` 的对外阶段名。

    两道「不是因子链上的一步」的剔除（除权日、§4.4.1 的 vetted 门槛）统一记成 `filter`，
    其余沿用因子链自己的阶段名（`box` / `decline` / `B8` / `B4` / `A`）。
    """
    return "filter" if key in ("exright", "box_amount") else key


def _write_pool(conn, market, as_of, top) -> None:
    """整池替换（同一天重跑幂等），跨表写入单事务。"""
    with conn:
        conn.execute("DELETE FROM pool WHERE trade_date = ? AND market = ?", (as_of, market))
        for rank, item in enumerate(top, 1):
            box, fv, result = item["box"], item["fv"], item["score"]
            factors = {
                "items": {
                    key: {"raw": raw, "ach": ach, "pts": pts}
                    for key, (raw, ach, pts) in result.items.items()
                },
                "score": result.score,
                "box_vol_mean_adj": fv.box_vol_mean_adj,
            }
            conn.execute(
                "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
                " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw,"
                " box_vol_mean, pos_in_box, a_group_discounted, b4_degenerate)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    as_of, market, item["code"], rank, result.score,
                    json.dumps(factors, ensure_ascii=False),
                    box.n, box.high, box.low,
                    box.high / item["factor"], box.low / item["factor"],
                    item["close_raw"], fv.box_vol_mean_adj / item["vol_factor"],
                    box.pos_in_box,
                    int(fv.a_group_discounted), int(fv.b4_degenerate),
                ),
            )


# ---------------------------------------------------------------- 数据加载


def _placeholders(codes) -> str:
    return ",".join("?" * len(codes))


def _load_index(conn, as_of) -> list[Bar]:
    return [
        Bar(code=_INDEX_CODE, date=r[0], open=r[1], high=r[2], low=r[3], close=r[4],
            volume=r[5], amount=r[6], source=r[7])
        for r in conn.execute(
            "SELECT date, open, high, low, close, volume, amount, source FROM daily_bars"
            " WHERE code = ? AND date <= ? ORDER BY date",
            (_INDEX_CODE, as_of),
        )
    ]


def _load_bars(conn, codes, as_of) -> dict[str, list[Bar]]:
    sql = (
        "SELECT code, date, open, high, low, close, volume, amount, source FROM daily_bars"
        " WHERE date <= ? AND code IN (" + _placeholders(codes) + ") ORDER BY code, date"
    )
    out: dict[str, list[Bar]] = {}
    for r in conn.execute(sql, (as_of, *codes)):
        out.setdefault(r[0], []).append(
            Bar(code=r[0], date=r[1], open=r[2], high=r[3], low=r[4], close=r[5],
                volume=r[6], amount=r[7], source=r[8])
        )
    return out


def _load_adj(conn, codes, as_of) -> dict[str, list[Bar]]:
    sql = (
        "SELECT code, date, open, high, low, close, volume FROM daily_bars_adj"
        " WHERE date <= ? AND code IN (" + _placeholders(codes) + ") ORDER BY code, date"
    )
    out: dict[str, list[Bar]] = {}
    for r in conn.execute(sql, (as_of, *codes)):
        out.setdefault(r[0], []).append(
            Bar(code=r[0], date=r[1], open=r[2], high=r[3], low=r[4], close=r[5],
                volume=r[6], amount=0.0, source="adj")
        )
    return out


def _load_factors(conn, codes, as_of) -> dict[str, tuple[float, float]]:
    """当日累计因子（`factor_T` / `vol_factor_T`）：raw 价与 C2 分母都靠它桥接（§4.6.9）。"""
    sql = (
        "SELECT code, factor, vol_factor FROM daily_bars_adj WHERE date = ? AND code IN ("
        + _placeholders(codes) + ")"
    )
    return {r[0]: (float(r[1]), float(r[2])) for r in conn.execute(sql, (as_of, *codes))}


def _load_actions(conn, codes) -> dict[str, list[dict]]:
    sql = (
        "SELECT code, ex_date, source, cash_per_share, bonus_ratio, tran_ratio, implied_factor"
        " FROM corporate_actions WHERE code IN (" + _placeholders(codes) + ")"
    )
    out: dict[str, list[dict]] = {}
    for r in conn.execute(sql, codes):
        out.setdefault(r[0], []).append(
            {
                "ex_date": r[1], "source": r[2], "cash_per_share": r[3],
                "bonus_ratio": r[4], "tran_ratio": r[5], "implied_factor": r[6],
            }
        )
    return out


def _load_suspensions(conn, codes) -> dict[str, list[tuple[int, int | None]]]:
    sql = (
        "SELECT code, start_date, end_date FROM suspension_calendar WHERE code IN ("
        + _placeholders(codes) + ")"
    )
    out: dict[str, list[tuple[int, int | None]]] = {}
    for r in conn.execute(sql, codes):
        out.setdefault(r[0], []).append((int(r[1]), None if r[2] is None else int(r[2])))
    return out


def _load_status(conn, codes) -> dict[str, list[tuple[int, str]]]:
    sql = (
        "SELECT code, start_date, st_status FROM stock_status_history WHERE code IN ("
        + _placeholders(codes) + ") ORDER BY code, start_date"
    )
    out: dict[str, list[tuple[int, str]]] = {}
    for r in conn.execute(sql, codes):
        out.setdefault(r[0], []).append((int(r[1]), r[2]))
    return out


# ---------------------------------------------------------------- 小件


def _st_lookup(rows: list[tuple[int, str]]):
    """`date → 当日 ST 状态`（判历史 bar 的涨跌停规则要用**那一天**的状态，§4.6.6）。"""

    def at(date: int) -> str:
        current = "normal"
        for start_date, status in rows:
            if start_date <= date:
                current = status
            else:
                break
        return current

    return at


def _ex_refs(raw: list[Bar], actions: list[dict]) -> dict[int, float]:
    """除权日 → 除权参考价（`annotate` 用它当「昨收」算涨跌停，§4.6.6）。"""
    index_of = {bar.date: i for i, bar in enumerate(raw)}
    refs: dict[int, float] = {}
    for action in actions:
        ex_date = int(action["ex_date"])
        i = index_of.get(ex_date)
        if i is None or i == 0:
            continue
        prev_close = raw[i - 1].close
        implied = action.get("implied_factor")
        if action.get("source") == "snapshot_implied" and implied:
            refs[ex_date] = round(prev_close / float(implied), 2)
        else:
            refs[ex_date] = ex_ref(prev_close, float(action["cash_per_share"]),
                                   float(action["bonus_ratio"]), float(action["tran_ratio"]))
    return refs


def _resume_dates(suspensions: list[tuple[int, int | None]], cal, as_of: int) -> set[int]:
    """复牌日 = 停牌 `end_date` 之后的**第一个交易日**（含「今天复牌」，D24）。"""
    days = sorted(d for d in cal.days if d <= as_of)
    out: set[int] = set()
    for _start, end in suspensions:
        finish = as_of if end is None else end
        i = bisect_right(days, finish)
        if i < len(days):
            out.add(days[i])
    return out
