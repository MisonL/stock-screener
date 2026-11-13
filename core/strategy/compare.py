"""`compare_pattern`：形态相似度（§7.5.4）。

**返回 `(data, reason)`，不返回信封**：调用方（`llm/tools.py`）会再套一层信封，
这里若自己返回 `{"ok": false}` 就会变成 `{"ok": true, "data": {"ok": false}}`——
模型的 ok 判定与 grounding 的路径取值全部错位。同理不能 import `llm.tools`（循环导入）。

四个曾经算错的地方（都有回归测试钉着）：

1. **窗口右端不含今天**。`find_box` 的箱体窗口是 `rows[start:start+n]`（右端是昨天，
   见 core/strategy/box.py），拿 `[-n:]` 会把今日未收盘 bar 吃进来，盘中 15:00 与
   15:10 得到两个不同答案。
2. **`date <= today` 不等于「已收盘」**。盘中回填会写入当日未完成 bar，
   参考窗口与 `after_20d` 都必须按 `close_time` 判定。
3. **两段长度不等不能头对齐截断**。那等于拿当前窗口最旧的一段去比参考窗口最新的一段。
4. **停牌分母的终点必须是 `ref_end`/今天，不是「最后一根实际 bar」**——否则尾部停牌
   完全不可见；分母拿不到（指数缺 bar）时必须 fail-closed，不能返回残缺序列的相关系数。
"""
from __future__ import annotations

import math
import re

from core.clock import TradingCalendar
from core.market import classify_market
from core.strategy.box import find_box, Reject
from core.strategy.pool import (
    _ex_refs, _load_actions, _load_adj, _load_bars, _load_status, _load_suspensions,
    _resume_dates, _st_lookup,
)
from core.strategy.window import annotate

ADJUST = "adj"
MIN_SEGMENT = 5
AFTER_DAYS = 20
_DATE = re.compile(r"^\d{8}$")


def _parse_date(raw) -> int | None:
    """只接受 `YYYYMMDD` 或 `YYYY-MM-DD`。

    `int("2026/08/03")` 会抛、`"2026-8-3"` 会被静默解析成 7 位假日期 202683——
    两者都必须变成 `ref_window_insufficient`，而不是异常或错窗口。
    """
    text = str(raw or "").strip()
    if text.count("-") == 2:
        text = text.replace("-", "")
    return int(text) if _DATE.match(text) else None


def _limit_date(conn, clock) -> int:
    """可用的最后一根**已收盘** bar。盘中（未到 close_time）今日 bar 不算。"""
    today = int(clock.now().strftime("%Y%m%d"))
    cal = TradingCalendar(conn)
    close_time = cal.close_time(today)
    if close_time is None:
        return today                       # 非交易日：今天没有 bar，取到今天也无害
    if clock.now().strftime("%H:%M") >= close_time:
        return today
    try:
        return cal.prev(today)
    except LookupError:
        return today


def _adj_closes(conn, code: str, *, end: int, start: int | None = None) -> list[tuple[int, float]]:
    sql = ("SELECT date, close FROM daily_bars_adj WHERE code = ? AND date <= ?"
           " AND date >= ? ORDER BY date")
    return [(r[0], r[1]) for r in conn.execute(sql, (code, end, start or 0))]


def _current_window(conn, cfg, code: str, limit: int) -> list[float] | None:
    """当前窗口 = 该股最新池子的 N\\* 根（右端是昨天）；不在池里就现跑 `find_box`。"""
    row = conn.execute(
        "SELECT box_days FROM pool WHERE code = ? AND trade_date <= ?"
        " ORDER BY trade_date DESC LIMIT 1", (code, limit)).fetchone()
    n_max = int(cfg.strategy.box.max_lookback)
    if row and row[0]:
        n = min(int(row[0]), n_max)        # box_days 是数据不是配置，必须钳到 max_lookback
        # **箱体窗口右端是昨天**（find_box 的口径）：最后那根是今天，去掉它
        series = _adj_closes(conn, code, end=limit)
        tail = series[-(n + 1):-1] if len(series) > n else series[-n:]
        return ([c for _, c in tail], tail[-1][0]) if len(tail) >= n else None

    market = classify_market(code)
    if market is None:
        return None
    p, s = cfg.market(market), cfg.strategy
    raw = _load_bars(conn, [code], limit).get(code, [])
    adj = _load_adj(conn, [code], limit).get(code, [])
    if not raw or len(adj) != len(raw):
        return None
    cal = TradingCalendar(conn)
    rows = annotate(
        raw, adj, market,
        _st_lookup(_load_status(conn, [code]).get(code, [])),
        _ex_refs(raw, _load_actions(conn, [code]).get(code, [])),
        _resume_dates(_load_suspensions(conn, [code]).get(code, []), cal, limit),
    )
    box = find_box(rows, p, s.box)
    if isinstance(box, Reject):
        return None
    # **箱体窗口右端是昨天**：`rows[start:start+n]`，不是 `[-n:]`
    window = rows[box.start: box.start + box.n]
    closes = [r.c for r in window]
    return (closes, window[-1].date) if len(closes) >= MIN_SEGMENT else None


def _zscore(values: list[float]) -> list[float] | None:
    """常数序列返回 `None`（std=0）：相似度无定义，给 0.0 是冒充「完全不像」。"""
    if not values:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(var)
    if std == 0:
        return None
    return [(v - mean) / std for v in values]


def _pearson(a: list[float], b: list[float]) -> float:
    """皮尔逊对平移/缩放不变（所以段内不必再 z-score，N*=400 时那纯是白算）。"""
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def _rmse(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n == 0:
        return 0.0
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)) / n)


def _segments(cur: list[float], ref: list[float], seg_len: int) -> list[dict]:
    """取相关最高的 3 段，**按 ref 等距切窗、互不重叠**——滑窗取 max 会让 top3
    全是同一段的相邻滑动，自比时三段 0.98+ 毫无信息量。段内不做 z-score。"""
    out: list[dict] = []
    offset = 0
    while offset + seg_len <= len(ref) and len(out) < 3:
        sub_ref = ref[offset: offset + seg_len]
        best = None
        for cur_off in range(0, len(cur) - seg_len + 1):
            r = _pearson(cur[cur_off: cur_off + seg_len], sub_ref)
            if best is None or r > best["similarity"]:
                best = {"similarity": round(r, 4), "offset": offset, "cur_offset": cur_off}
        out.append(best)
        offset += seg_len
    return out


def _after_20d(rows: list[tuple[int, float]], base_close: float) -> dict:
    """参考窗口**之后**的 20 根：收益与最大回撤（事实，不是预测）。

    基准是参考窗口**最后一根**的收盘（不是「之后第一根」）——否则丢掉
    `ref_end → 次日` 的跳空，20 个区间也缩成 19 个。`n` 必须带回：不足 20 根时
    模型若当成「20 日」播报就是造假。
    """
    window = rows[:AFTER_DAYS]
    if not window or not base_close:
        return {"return": None, "max_drawdown": None, "n": len(window)}
    peak = base_close
    mdd = 0.0
    for _, close in window:
        peak = max(peak, close)
        mdd = min(mdd, close / peak - 1 if peak else 0.0)
    return {"return": round(window[-1][1] / base_close - 1, 4),
            "max_drawdown": round(mdd, 4),
            "n": len(window)}


def _ref_is_sufficient(conn, start: int, anchor: int, ref_rows, want: int,
                       *, pinned: bool) -> bool:
    """停牌判据，两关都过才放行。日历为空一律 fail-closed——返回残缺序列的
    相关系数比不答更糟（spec §7.5.4）。

    1. **窗口必须真的结束在它该结束的位置**：`end` 给定时是 `end` 前最后一个交易日，
       否则是 `start` 起第 `want` 个交易日。尾部停牌会让最后一根提前，这一关直接发现
       （只看「最后一根实际 bar」的分母永远看不见尾部停牌）。
    2. **区间内停牌不得过半**：分母是日历，分子是实际拿到的根数，区间取**实际用到的那段**。
    """
    cal = TradingCalendar(conn)
    if not cal.days or not ref_rows:
        return False
    days = sorted(cal.days)
    if not days or not ref_rows:
        return False
    if pinned:
        # 给了 ref_end：窗口必须结束在 `anchor` 前最后一个交易日。尾部停牌会让最后一根
        # 提前，这一关就发现了——只用「最后一根实际 bar」当分母永远看不见尾部停牌。
        last_avail = max((d for d in days if d <= anchor), default=None)
        if last_avail is None or last_avail != anchor or ref_rows[-1][0] != anchor:
            return False
        used_start = ref_rows[0][0]
        expected = sum(1 for d in days if used_start <= d <= anchor)
        return expected > 0 and len(ref_rows) >= expected / 2

    # 没给 ref_end：窗口语义是「从 start 起 N* 根」。分母取这 N* 根**跨过的日历天数**，
    # 实际根数不足一半即视为停牌过半（尾部停牌同样会让跨过的天数膨胀）。
    if len(ref_rows) < want:
        return False
    horizon = [d for d in days if d >= start]
    if len(horizon) < want:                 # 历史不够长，构不出 N* 窗口
        return False
    spanned = sum(1 for d in days if start <= d <= ref_rows[-1][0])
    return spanned > 0 and len(ref_rows) >= spanned / 2


def compare_pattern(conn, clock, code: str, ref_start: str, ref_end: str | None = None,
                    cfg=None) -> tuple[dict | None, str | None]:
    """返回 `(data, reason)`：成功 `reason is None`，失败 `data is None`。"""
    if cfg is None:                                # 必填：None 会让 AttributeError 逃出契约
        return None, "ref_window_insufficient"
    start = _parse_date(ref_start)
    end = _parse_date(ref_end) if ref_end else None
    if start is None or (ref_end and end is None):
        return None, "ref_window_insufficient"

    limit = _limit_date(conn, clock)
    if end is not None and end > limit:
        end = limit

    cur_win = _current_window(conn, cfg, code, limit)
    if not cur_win:
        return None, "no_current_box"
    cur, cur_end = cur_win

    # 早于上市日：窗口前段根本不存在，谈相似度没有意义
    row = conn.execute("SELECT list_date FROM stock_meta WHERE code = ?", (code,)).fetchone()
    if row and row[0] and start < int(row[0]):
        return None, "ref_window_insufficient"

    # **区间必须被 ref_end 真的封住**：只传 start 会把 ref_end 之后的 bar 也读进来，
    # 「[2020-03, 2020-06] 这段形态」就变成了「从 2020-03 到今天的形态」
    # 参考窗口的右端锚：不晚于 `ref_end`、也不晚于当前窗口右端。
    # 两段必须结束在同一天，否则「第 i 根对第 i 根」根本对不上。
    anchor = min(end, cur_end) if end is not None else cur_end
    ref_rows = _adj_closes(conn, code, end=anchor, start=start)
    if end is None:
        ref_rows = ref_rows[:len(cur)]          # ref_end=None：长度等于当前 N*
    else:
        ref_rows = ref_rows[-len(cur):]         # 区间比 N* 长 → 取最近的一段
    if len(ref_rows) < MIN_SEGMENT:
        return None, "ref_window_insufficient"
    if not _ref_is_sufficient(conn, start, anchor, ref_rows, len(cur), pinned=end is not None):
        return None, "ref_window_insufficient"

    ref = [c for _, c in ref_rows]
    n = min(len(cur), len(ref))
    cur, ref = cur[-n:], ref[-n:]                  # **尾对齐**：绝不用头对齐截断
    cur_z, ref_z = _zscore(cur), _zscore(ref)
    seg_len = max(MIN_SEGMENT, n // 3)

    after_rows = _adj_closes(conn, code, end=limit, start=ref_rows[-1][0] + 1)
    data: dict = {
        "similarity": None if cur_z is None or ref_z is None else round(_pearson(cur_z, ref_z), 4),
        "rmse": None if cur_z is None or ref_z is None else round(_rmse(cur_z, ref_z), 4),
        "degenerate": cur_z is None or ref_z is None,      # 常数序列：相似度无定义
        "best_segments": [] if cur_z is None or ref_z is None else _segments(cur_z, ref_z, seg_len),
        "worst_point": None,
        "after_20d": _after_20d(after_rows, ref[-1]),
        "n": n,
        "adjust": ADJUST,
    }
    if cur_z is not None and ref_z is not None:
        diff = [(i, round(c - r, 4)) for i, (c, r) in enumerate(zip(cur_z, ref_z))]
        worst = max(diff, key=lambda t: abs(t[1]))
        data["worst_point"] = {"offset": worst[0], "diff": worst[1]}
    return data, None
