"""每日增量（15:05 快照 append）：把当日快照落成 raw 日线。

一天只做四件事，顺序固定：

1. **快照与入库**：按 600 只/请求拉全宇宙（`stock_meta`）快照 → 快照时间戳不是今天就
   整体跳过（非交易日/上游还停在上一个交易日）→ 每只由快照组装当日 `Bar` → 过
   `Bar.is_placeholder` 的三重门（含时间门：当日 bar 只在 `close_time + 5min` 之后才定盘）。
2. **除权**：`f[4]`（快照前收）与库里上一根 raw 收盘差超过 `universe.price_eps` 即当日除权，
   当日没有 `westock_dividend` 记录时补一条 `source='snapshot_implied'`（**`INSERT OR IGNORE`**：
   Task 22 的盘中路径也写同一个 `(code, ex_date, source)`，两个写者同键值一致，先到者胜）。
3. **停牌与缺口**：当日量为 0 或时间戳停滞 = 停牌，不写 bar；最后一根 bar 早于上一交易日
   且不是停牌 = 缺口，单只 `kline_daily(code, 30)` 补拉。
4. **名称与复权**：名称前缀（`ST`/`*ST`/`S`/`SST`/`退`）变化时关闭旧 `stock_status_history`
   区间、开新区间；对改动过的代码 `rebuild_adj`（新增当日 bar 必须立刻能在 adj 里读到）。

**只 append 当日**，不补历史：历史回填是 `jobs/backfill.py` 的事，两者不同时运行（D18）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from core.adjust import rebuild_adj
from core.clock import SH, TradingCalendar, snapshot_is_today
from core.models import Bar

# 快照 600 只/请求（§6.1.2.2 的运营值）
CHUNK = 600
# 缺口补拉只取最近 30 根：缺口是「最后一天没落库」，不是重放全历史
GAP_COUNT = 30
# 单次 eod 最多补拉多少只。**必须有上限**：某天 eod 没跑成时，次日
# `last_date != prev_day` 对全宇宙成立，5577 只 × 300ms ≈ 28 分钟，而且
# 这段时间独占 jobs 唯一的限速出口，三循环全部排队。上游被 WAF 封过一次
# （spec:2481「不是 60 秒能恢复的，代价是整条 K 线链路不可用」），
# 无上限补拉正好撞在这个雷上。超出的记 `gaps_deferred`，交给 backfill。
GAP_MAX = 200
# 当日 bar 只有到 close_time + 5min 才是收盘 bar（S-A13）
CLOSE_BUFFER_MIN = 5
# 决定 #8：首次部署按当时状态写一条从上线日（1990-01-01）起的区间
FIRST_DEPLOY_DATE = 19900101
# 停牌日历的回看窗口（D24：近 250 日）
SUSPENSION_DAYS = 250

# 名称前缀 → stock_status_history.st_status。**顺序不能换**（`*ST` 先于 `ST`，
# `SST`/`ST`/`S` 都是「特别处理」的历史写法，数字 F12）。
ST_PREFIXES: tuple[tuple[str, str], ...] = (
    ("退", "delisting"),
    ("*ST", "*ST"),
    ("SST", "ST"),
    ("ST", "ST"),
    ("S", "ST"),
)


@dataclass
class EodReport:
    skipped: bool = False       # 非交易日 / 错日快照 / 时间门未到，整体没做事
    appended: int = 0
    placeholders: int = 0
    exrights: int = 0
    gaps: int = 0
    gaps_deferred: int = 0      # 超过 `GAP_MAX` 被推迟的缺口票数（交给 backfill）
    reused_snapshot: bool = False   # 复用了盘中 market 快照（省掉 10 个请求）
    suspended: int = 0
    stale: int = 0              # 快照是今天的但早于收盘（15:05 拿到 10:00 那份）


def st_status_of(name: str) -> str:
    """名称 → `st_status`（`normal` / `ST` / `*ST` / `delisting`）。"""
    text = (name or "").strip()
    if not text:
        return "normal"
    for prefix, status in ST_PREFIXES:
        if text.startswith(prefix):
            return status
    return "normal"


def run_eod(cfg, reg, conn, clock) -> EodReport:
    """15:05 的每日增量。`reg` 是 `SourceRegistry`，`clock` 是 `Clock`。"""
    today = clock.today()
    report = EodReport()

    codes = [row[0] for row in conn.execute("SELECT code FROM stock_meta ORDER BY code")]
    if not codes:
        report.skipped = True                 # 宇宙还没回填，什么也没有可标的
        return report

    today_date = _as_date(today)

    cal = TradingCalendar(conn)
    close_time = cal.close_time(today) or str(cfg.markets.session.close_time)
    now = clock.now()
    if _minutes(now.time()) < _minutes(close_time) + CLOSE_BUFFER_MIN:
        report.skipped = True                 # 时间门：当日 bar 还没定盘
        return report

    # 快照放最后取：只有确定要出 bar 才花请求，且此时已知 close_time（复用判据要用）
    reused = _reuse_market_snapshot(conn, codes, today, close_time, now)
    if reused is not None:
        quotes, source_of = reused
        report.reused_snapshot = True
    else:
        quotes, source_of = _snapshot(reg, codes)

    # **交易日闸必须在拿到快照之后**：复用的缓存行和现拉的结果都要过这一关，
    # 否则非交易日（上游还停在上一交易日）会被当成当天出 bar。
    if not any(q.ts is not None and snapshot_is_today(q.ts, today_date) for q in quotes.values()):
        report.skipped = True
        return report

    prev_day = _prev(cal, today)
    eps = float(cfg.strategy.universe.price_eps)

    changed: set[str] = set()
    suspended: set[str] = set()
    filled: set[str] = set()
    # **两遍共用的尝试配额**：成功与失败都消耗，避免「第一遍没闸门、
    # 失败被两遍各拉一次」把限流保护绕过去（实测 205 只全失败会发 410 次）。
    attempted: set[str] = set()
    reported: set[str] = set()          # 本轮已补拉并计入 gaps 的票，第二遍不再重复补
    last_dates: dict[str, int | None] = {}

    with conn:                                # 跨表写入单事务
        for code in codes:
            # 只看到「今天之前」的最后一根：重复跑 eod 时昨天那根仍是除权比对的基准，
            # 拿今天自己的 bar 当天会天天误判一次除权。
            last = conn.execute(
                "SELECT date, close FROM daily_bars WHERE code = ? AND date < ? "
                "ORDER BY date DESC LIMIT 1",
                (code, today),
            ).fetchone()
            last_date, last_close = (last[0], last[1]) if last else (None, None)
            last_dates[code] = last_date

            quote = quotes.get(code)
            if quote is None:
                _log(conn, code, 0, "empty", now)
                continue

            _sync_status(conn, code, quote.name, today, prev_day)

            if _is_suspended(quote, today_date):
                suspended.add(code)
                report.suspended += 1
                _log(conn, code, 0, "empty", now)
                continue

            if _is_stale(quote, today_date, close_time, now):
                # 陈旧快照（15:05 拿到 10:00 那份）：量只累计了半天，写成收盘 bar 会把
                # 量能压低、并让 C2 的折算基准永久偏小。记 stale 而不是 suspended——
                # 它今天可能是正常交易的，只是我们这份快照太旧。
                report.stale += 1
                _log(conn, code, 0, "error", now)
                continue

            # 判除权前先补昨日：缺 T-1 时 last_close 是 T-2 的收盘，拿它跟今天的 f[4] 比
            # 会把「正常涨跌 + 缺少一根」误判成除权，写出一个不存在的 snapshot_implied。
            if prev_day is not None and last_date is not None and last_date < prev_day:
                # 配额在**发请求前**认领：失败也消耗，且同一只票两遍只试一次
                gapped = False
                if code not in attempted:
                    if len(attempted) >= GAP_MAX:
                        # **不要 `continue`**：配额只推迟「K 线补拉」，当天快照 bar
                        # 必须照常写。用一次补拉请求换掉一天的数据，方向反了。
                        attempted.add(code)
                        if _defer_gap(conn, code, last_date, now):
                            report.gaps_deferred += 1
                    else:
                        attempted.add(code)
                        gapped = _backfill_gap(reg, conn, code, now)
                if gapped:
                    report.gaps += 1
                    _clear_gap(conn, code)
                    filled.add(code)
                    reported.add(code)
                    row = conn.execute(
                        "SELECT date, close FROM daily_bars WHERE code = ? AND date < ? "
                        "ORDER BY date DESC LIMIT 1", (code, today)).fetchone()
                    if row is not None:
                        last_date, last_close = row[0], row[1]
                        last_dates[code] = last_date

            # **只有确认手里的收盘就是 T-1 才敢反推除权**：补拉失败/被上限推迟时
            # `last_close` 还是 T-2 的，拿它跟今天的 `prev_close`（T-1）比，
            # 会把「正常涨跌 + 缺一根」写成一个不存在的除权因子，污染复权价
            # （实测偏低约 1.96%），而复权价正是选股输入。
            comparable = prev_day is None or last_date == prev_day
            if (comparable and last_close is not None and quote.prev_close is not None
                    and abs(quote.prev_close - last_close) > eps):
                if _write_implied(conn, code, today, last_close / quote.prev_close):
                    report.exrights += 1
                    changed.add(code)

            bar = _bar(code, quote, today, source_of.get(code, "tencent"))
            if bar.is_placeholder(today, prev_close=last_close, close_time=close_time, now=now):
                report.placeholders += 1
                _log(conn, code, 0, "empty", now)
                continue

            conn.execute(
                "INSERT OR REPLACE INTO daily_bars(code,date,open,high,low,close,volume,amount,source) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (bar.code, bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount, bar.source),
            )
            report.appended += 1
            # 注意：**不要**把 last_dates 覆盖成 today。缺口检查比的是「追加之前的最后一根」，
            # 覆盖掉就永远看不出 20261028 → 20261030 这种中间空洞（gaps 恒为 0）。
            changed.add(code)
            _log(conn, code, 1, "ok", now)

    # 缺口补拉必须在 rebuild_adj 之前：补进来的 bar 也要进 adj，否则 raw/adj 长度不等，
    # _evaluate 会以 no_adj 把这只票整只淘汰（等于补了个寂寞）。
    # 判据是「有没有**昨天**那根」（上一交易日），不是「有没有今天之前某一根」：
    # 后者会从 9 月一直补到 10 月，把每只票的 30 根重拉一遍。
    if prev_day is not None:
        for code in codes:
            if code in suspended:
                continue
            # **`filled` 也要排除**：它只可能在第一遍被加进来，但万一以后控制流
            # 改成多遍，缺这一条就会对同一只票反复补拉。
            # **`attempted` 是两遍共用的配额**：第一遍试过的（无论成败）不再试
            if code in reported or code in filled or code in attempted:
                continue
            last_date = last_dates.get(code)
            if last_date == prev_day:
                continue
            if len(attempted) >= GAP_MAX:
                attempted.add(code)
                if _defer_gap(conn, code, last_date, now):
                    report.gaps_deferred += 1
                continue
            attempted.add(code)
            if _backfill_gap(reg, conn, code, now):
                report.gaps += 1
                _clear_gap(conn, code)
                filled.add(code)

    for code in sorted(changed | filled):
        rebuild_adj(conn, code)               # Task 10：新增 bar 必须立刻进 adj
    return report


# ---- 周期任务（jobs/main.py 调度） ---------------------------------------


def fetch_actions(westock, codes, years: int = 10, max_requests: int | None = None):
    """**只取数、不写库**（纯网络）。返回 `({code: [CorpAction]}, 已处理 code 集合)`。

    失败返回 `(None, set())`。第二项是**真正收到响应的代码**，游标只该按它推进：
    限频伪装成空/残缺数据时，没出现的票必须留到下一轮重试。

    单独拆出来是为了让定时任务能「锁外取数、锁内写库」：一次取数要发好几个
    请求、还带退避，放进写锁里会把每秒的 tick 和 30 秒的心跳全堵住
    （参见 `test_members_job_does_not_fetch_inside_the_lock` 记录的那次故障）。
    """
    return westock.batch_dividend(list(codes), years=int(years), max_requests=max_requests)


def store_actions(conn, actions) -> int:
    """把取到的除权事件写库（**只写、不联网**）。同日两源并存时 westock 是真值。"""
    written = 0
    with conn:
        for code, items in actions.items():
            for action in items:
                # 同日两源并存时 westock 是真值：先删掉快照反推的那条，再写官方那条
                conn.execute(
                    "DELETE FROM corporate_actions WHERE code = ? AND ex_date = ? AND source = 'snapshot_implied'",
                    (code, action.ex_date),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO corporate_actions"
                    "(code, ex_date, source, cash_per_share, bonus_ratio, tran_ratio, implied_factor) "
                    "VALUES (?,?,?,?,?,?,NULL)",
                    (code, action.ex_date, action.source, action.cash_per_share,
                     action.bonus_ratio, action.tran_ratio),
                )
                written += 1
    if written:
        # **写完官方除权事件必须立刻重建复权表**：否则在下一次 EOD/回填之前，
        # 所有读 `daily_bars_adj` 的消费者拿到的仍是按旧事件算的价格。
        # 滚动刷新常在 EOD 之后或次日盘前跑，正好卡在这个窗口里。
        # `rebuild_adj(code=…)` 用 `actions_hash` 判断，未变的票会跳过，很便宜。
        for code in actions:
            rebuild_adj(conn, code)
    return written


def refresh_actions(cfg, westock, conn, codes, max_requests: int | None = None) -> int:
    """取数 + 写库一步做完。返回写入行数；取数失败返回 `None`。

    **仅供测试与一次性脚本使用**：它会联网，在生产调度里应改用
    `fetch_actions()` + `store_actions()`，把网络请求放到写锁外面
    （见 `jobs/main.py::_actions`）。用本函数排定时任务会把 tick 堵住。
    """
    actions, _done = fetch_actions(westock, codes, max_requests=max_requests)
    if actions is None:
        return None
    return store_actions(conn, actions)


#: 每轮最多发几个**批次**。上游实测连发 8–10 个请求就限频；停牌那 1 个请求也要
#: 占额度，所以分红只发 4 个批次（4 + 1 = 5，留足余量）。原来取 6，
#: 启动时有相当概率把停牌挤成限频失败，而一次失败要等下周才重试。
#:
#: **注意这是"批次"不是"请求"**：`_fetch_chunk` 内部撞限频还会重试（最多
#: `retries + 1` 次），所以实际请求数上限是 `4 × 3 + 1 = 13`。整轮耗时有
#: `budget_s` 封顶（见 `batch_dividend`），不会无限拖。
ACTIONS_MAX_REQUESTS = 4


def actions_cursor(conn, codes) -> list:
    """按游标取出「本轮要拉的那一段」股票代码。

    全市场 5000 多只、每轮只能发 4 个请求（40 只），一次拉完不可能。
    游标存在 `job_state('refresh_actions.next')`，每轮从上次停下的地方继续，
    走到末尾就从头开始——滚动刷新，循环覆盖全市场。
    """
    codes = sorted(codes)
    if not codes:
        return []
    row = conn.execute("SELECT value FROM job_state WHERE name='refresh_actions.next'").fetchone()
    start_code = str(row[0]) if row and row[0] else codes[0]
    # 游标可能指向已退市的代码：用二分找出第一个 >= 游标的
    lo, hi = 0, len(codes)
    while lo < hi:
        mid = (lo + hi) // 2
        if codes[mid] < start_code:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(codes):
        lo = 0                                # 到末尾了，从头上绕回去
    return codes[lo:]


def advance_actions_cursor(conn, codes, pulled, start=None) -> None:
    """把游标推进到"本轮**连续**确认过的最后一只之后"。

    `pulled` 是**确实收到响应的代码**（`batch_dividend` 的第二个返回值）。
    关键是"连续"：限频时常出现"开头几段正常、中间某段只回 1 只、后面又正常"，
    若按 `max(pulled)` 推进，中间那段没确认的代码就被整段跳过了——要等滚动一整圈
    （约 3 天）才再遇到。

    `start` 是本轮的起点代码（没给就从头算）。换算成"从 start 开始的最长连续前缀"。
    """
    codes = sorted(codes)
    if not codes:
        return
    code_set = set(codes)
    pulled = {c for c in (pulled or ()) if c in code_set}

    # **起点**：显式给了就用它；没给就沿用已保存的游标（不是"从头算"）。
    # 后者是兼容旧的"直接三参调用"：连续两轮调用时，第二轮必须接着第一轮的
    # 结果往后走，而不是回到开头重算。
    if start is None:
        row = conn.execute(
            "SELECT value FROM job_state WHERE name = 'refresh_actions.next'").fetchone()
        cursor = str(row[0]) if row and row[0] else None
        # 游标可能指向已退市代码：用二分找第一个 >= 游标的，与 `actions_cursor` 同口径
        begin = len(codes)
        if cursor:
            i, j = 0, len(codes)
            while i < j:
                mid = (i + j) // 2
                if codes[mid] < cursor:
                    i = mid + 1
                else:
                    j = mid
            begin = i
        if begin >= len(codes):
            begin = 0                    # 游标已经走到末尾：从头开始
    else:
        begin = len(codes)
        i, j = 0, len(codes)
        while i < j:
            mid = (i + j) // 2
            if codes[mid] < start:
                i = mid + 1
            else:
                j = mid
        begin = i if i < len(codes) else 0   # 起点不存在（退市）：从头开始

    if not pulled:
        # **空确认集时游标不动**：留在本轮起点，下次重试同一段。
        # （原来直接归零回 `codes[0]`，等于把已经走过的部分又重头拉一遍。）
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO job_state(name, value) VALUES ('refresh_actions.next', ?)",
                (codes[begin],))
        return

    # 从 begin 往后找最长连续命中，遇到第一个断点就停
    idx = begin
    while idx < len(codes) and codes[idx] in pulled:
        idx += 1
    if idx == begin:
        nxt = codes[begin]                   # 起点本身没拿到：不动
    elif idx < len(codes):
        nxt = codes[idx]                     # 停在断点
    else:
        nxt = codes[0]                       # 整段连续确认到底：回到起点
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO job_state(name, value) VALUES ('refresh_actions.next', ?)",
            (nxt,))


def store_suspensions(conn, rows, cal=None, codes=None) -> int:
    """把 `(code, start, 复牌日或None)` 落 `suspension_calendar`。

    **必须把复牌日往前推一个交易日**：下游两个消费者（
    `core/strategy/pool.py::_resume_dates` 用 `bisect_right`、
    `jobs/eod.py::resume_dates` 用 `end_date == 上一交易日`）都按
    「`end_date` 是**最后一个停牌日**」来解释。接口给的 `resumptionDate`
    是**实际复牌日**——原样写入会让策略晚一个交易日才识别到复牌，
    `resume_cooldown` 也跟着晚。没有日历时退回"复牌日 - 1 天"这种日粒度近似。
    """
    wanted = None if codes is None else set(codes)
    written = 0
    with conn:
        for code, begin, resumption in rows:
            if wanted is not None and code not in wanted:
                continue
            finish = _last_suspended_day(resumption, cal)
            conn.execute(
                "INSERT OR REPLACE INTO suspension_calendar(code, start_date, end_date) VALUES (?,?,?)",
                (code, begin, finish),
            )
            written += 1
        # **顺手闭合"未定复牌"的旧区间**：只靠本轮快照永远不会闭合，
        # 复牌后的每个交易日都会被当成停牌（见 `_close_resumed_from_bars`）。
        _close_resumed_from_bars(conn)
    return written


def _close_resumed_from_bars(conn) -> int:
    """用**实际成交证据**闭合"未定复牌"的停牌区间，返回闭合条数。

    接口只给"当前正在停牌"的快照：某只票今天复牌后就不在返回里了，
    那条 `end_date IS NULL` 的行永远留着，于是**复牌之后的每个交易日都被算成停牌**
    ——`stock_stats.suspended_250` 虚高、`resume_cooldown` 也永远不会触发。

    闭合依据只能是"复牌后真的有成交"（`daily_bars` 里出现了该股、且日期晚于
    停牌起始日的 bar）。**不能因为"这次快照里没有它就闭合"**：数据源失败、
    请求残缺时同样没有它，那样会伪造复牌日。

    `end_date` 落"最后一个停牌日"= 复牌后第一根 bar 的前一个交易日。
    """
    rows = conn.execute(
        "SELECT code, start_date FROM suspension_calendar WHERE end_date IS NULL"
    ).fetchall()
    if not rows:
        return 0
    closed = 0
    for code, start in rows:
        # **必须有成交量才算"恢复交易"**：停牌期间有的数据源仍会给一根
        # volume=0 / amount=0 的占位 bar（`core/datasource/tencent.py` 接受零量），
        # 拿它当复牌证据会伪造复牌日，且把停牌天数少算——`suspended_250`
        # 偏低会让本该剔除的票留在池里。
        # 只看 volume 不看 amount：部分数据源本来就没有成交额。
        first_bar = conn.execute(
            "SELECT min(date) FROM daily_bars"
            " WHERE code = ? AND date > ? AND volume > 0",
            (code, int(start))).fetchone()
        if not first_bar or first_bar[0] is None:
            continue                    # 还没有可信成交：保持 NULL（仍在停牌/无证据）
        resumed = int(first_bar[0])
        last_suspended = _last_trading_day_before(conn, resumed)
        if last_suspended is None or last_suspended < int(start):
            continue
        conn.execute(
            "UPDATE suspension_calendar SET end_date = ? WHERE code = ? AND start_date = ?",
            (last_suspended, code, int(start)))
        closed += 1
    return closed


def _last_trading_day_before(conn, day: int) -> int | None:
    """`day` 之前最近的一个有 sh000300 bar 的交易日。"""
    row = conn.execute(
        "SELECT max(date) FROM daily_bars WHERE code = 'sh000300' AND date < ?",
        (day,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _last_suspended_day(resumption, cal) -> int | None:
    """复牌日 → 最后一个停牌日。`None`（未定复牌）原样返回。"""
    if resumption is None:
        return None
    if cal is not None:
        try:
            return cal.prev(resumption)
        except LookupError:
            pass                       # 日历不完整：退回按自然日近似
    return resumption - 1


def fetch_suspensions(westock):
    """**只取数、不写库**（纯网络）。返回 `[(code, start, 复牌日或None)]` 或 `None`。"""
    end = datetime.now(SH).date()
    start = end - timedelta(days=SUSPENSION_DAYS)
    return westock.suspension(start.isoformat(), end.isoformat())


def refresh_suspensions(cfg, westock, conn, codes=None, cal=None) -> int:
    """停牌日历（D24）：一次区间查询落 `suspension_calendar`。

    `codes` 给定时只保留这几只（`jobs/main.py` 传 `stock_meta` 全体，测试传单只）。
    `cal` 是交易日历，用来把复牌日换算成"最后一个停牌日"（见 `store_suspensions`）。

    **会联网**：生产调度里应改用 `fetch_suspensions()` + `store_suspensions()`，
    把请求放到写锁外面（见 `jobs/main.py::_suspensions`）。
    """
    rows = fetch_suspensions(westock)
    if rows is None:
        return None                     # 取数失败（`[]` 才是"今天没人停牌"）
    return store_suspensions(conn, rows, cal=cal, codes=codes)


def resume_dates(conn, cal, today: int) -> set[str]:
    """复牌日期 = 上一交易日 的 code 集合（`universe.resume_cooldown` 的输入）。"""
    prev_day = _prev(cal, today)
    if prev_day is None:
        return set()
    rows = conn.execute("SELECT code FROM suspension_calendar WHERE end_date = ?", (prev_day,))
    return {row[0] for row in rows}


def fetch_sector_members(sina) -> list[tuple[str, str, list[str]]]:
    """**只发请求、不碰数据库**：返回 `[(sector, name, members)]`。

    单独拆出来是为了让调用方能「先抓完、再加锁写」——原来 `refresh_sector_members`
    把请求和写库交替做在同一把锁里，抓完几十个板块要好几分钟，把每秒的 tick 和
    30 秒的心跳全堵在锁上（曾经有一次心跳因等锁超过 30 秒被直接跳过）。
    """
    out: list[tuple[str, str, list[str]]] = []
    for sector, name in sina.fetch_sector_list():
        out.append((sector, name, list(sina.fetch_sector_members(sector))))
    return out


def write_sector_members(conn, sectors: list[tuple[str, str, list[str]]]) -> int:
    """把 `fetch_sector_members` 的结果写库（**纯本地操作，应持写锁调用**）。"""
    written = 0
    stamp = datetime.now(SH).isoformat()
    with conn:
        for sector, name, members in sectors:
            conn.execute(
                "INSERT OR REPLACE INTO sector_meta(sector, name, kind) VALUES (?,?,'industry')",
                (sector, name),
            )
            conn.execute("DELETE FROM sector_member WHERE sector = ?", (sector,))
            conn.executemany(
                "INSERT OR REPLACE INTO sector_member(sector, code, updated_at) VALUES (?,?,?)",
                [(sector, code, stamp) for code in members],
            )
            written += len(members)
    return written


def refresh_sector_members(sina, conn) -> int:
    """每天 08:50 刷行业成分股，返回写入的成分股条数。

    参数是 `SinaSource` 实例（`reg.sina`），不是 registry：板块没有降级路由。

    **保留这个一次做完的入口**（测试与脚本用）；`jobs/main.py` 的定时任务走
    `fetch_sector_members` + `write_sector_members` 两段，以便把网络请求移出锁。
    """
    return write_sector_members(conn, fetch_sector_members(sina))


# ---- 内部件 ---------------------------------------------------------------


def _reuse_market_snapshot(conn, codes: list[str], today: int, close_time: str,
                           now: datetime):
    """复用 `snapshot_cache('market')`：它和 eod 要的是**同一个请求**。

    market 循环与 eod 用同一 universe、同一 CHUNK、同一 registry 出口，
    15:05 时盘中循环刚写过一份（`_in_session` 含 15:05）。直接复用能省 10 个请求。

    判据必须**同时**满足，否则宁可自己拉：
    - 缓存日期是今天（隔夜的不能用）
    - `as_of` 不早于**定盘时刻**（`close_time` + `CLOSE_BUFFER_MIN`）。注意不能只比
      `close_time`：14:57 只是集合竞价开始，真正的收盘价 15:00 才定；market 循环
      一直拉到 15:05，所以 14:57:05 那份缓存是「合法存在但早于收盘」的，拿它当
      收盘 bar 会把 14:57–15:00 的收盘竞价量额静默丢掉（`_is_stale` 拦的正是这个）。
    """
    row = conn.execute("SELECT as_of, payload FROM snapshot_cache WHERE scope = 'market'"
                       ).fetchone()
    if not row or not row[1]:
        return None
    try:
        import json as _json

        body = _json.loads(row[1])
        as_of = datetime.fromisoformat(body.get("as_of") or row[0])
    except (TypeError, ValueError):
        return None
    if as_of.date() != _as_date(today):
        return None
    try:
        hh, mm = (int(x) for x in close_time.split(":"))
    except (ValueError, AttributeError):
        return None
    settled_min = hh * 60 + mm + CLOSE_BUFFER_MIN
    if as_of.hour * 60 + as_of.minute < settled_min:
        return None                      # 早于定盘：陈旧，宁可自己拉

    want = set(codes)
    quotes: dict = {}
    source_of: dict[str, str] = {}
    for item in body.get("rows") or []:
        code = item.get("code")
        if code not in want:
            continue
        quotes[code] = _quote_from_row(item)
        source_of[code] = item.get("source") or "tencent"
    if not quotes:
        return None
    return quotes, source_of


def _quote_from_row(item: dict):
    """`snapshot_cache` 的一行 → `Quote` 形状（`run_eod` 只读这几个字段）。"""
    from types import SimpleNamespace

    ts = item.get("ts")
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            ts = None
    return SimpleNamespace(code=item.get("code"), name=item.get("name"),
                           price=item.get("price"), prev_close=item.get("prev_close"),
                           open=item.get("open"), high=item.get("high"),
                           low=item.get("low"), volume=item.get("volume"),
                           amount=item.get("amount"), ts=ts)


def _snapshot(reg, codes: list[str]) -> tuple[dict, dict]:
    """按 600 只/请求拉全宇宙；返回值名字是实际应答的源（写 `daily_bars.source`）。"""
    quotes: dict = {}
    source_of: dict[str, str] = {}
    for start in range(0, len(codes), CHUNK):
        part = codes[start:start + CHUNK]
        chunk, used = reg.snapshot(part, scope="eod")
        for code, quote in chunk.items():
            quotes[code] = quote
            source_of[code] = used
    return quotes, source_of


def _defer_gap(conn, code: str, missing_through: int, now: datetime) -> bool:
    """把推迟的缺口**落库**，否则下次运行无从得知（内存计数随进程消失）。

    没有这条记录时，今天的 bar 照样追加 → 明天 `last_date` 已是昨天、eod 认为
    没有缺口；`backfill` 的默认 resume 又按 `fetch_log(backfill, ok)` 跳过。
    两条续跑路径之后历史空洞仍然在。
    """
    # **先查再写**：UPSERT 的 `rowcount`/`lastrowid` 在 DO UPDATE 分支上不可靠
    # （实测恒报「有变更」），而两遍循环会重复命同一只票，必须按「是否新增」计数，
    # 否则 `gaps_deferred` 与 `gap_pending` 条数对不上。
    existed = conn.execute("SELECT 1 FROM gap_pending WHERE code = ?", (code,)).fetchone()
    if existed:
        conn.execute(
            "UPDATE gap_pending SET missing_through = MIN(missing_through, ?) WHERE code = ?",
            (int(missing_through or 0), code))
        return False
    conn.execute(
        "INSERT INTO gap_pending(code, missing_through, first_seen, attempts) VALUES (?,?,?,0)",
        (code, int(missing_through or 0), now.isoformat()))
    return True


def _clear_gap(conn, code: str) -> None:
    conn.execute("DELETE FROM gap_pending WHERE code = ?", (code,))


def _is_suspended(quote, today: date) -> bool:
    """停牌判据：当日量为 0、四价缺字段，或快照时间戳不是今天（停滞）。"""
    if quote.volume is None or quote.volume == 0:
        return True
    if quote.price is None or quote.open is None or quote.high is None or quote.low is None:
        return True
    return quote.ts is None or not snapshot_is_today(quote.ts, today)


def _is_stale(quote, today: date, close_time: str, now: datetime) -> bool:
    """快照是否早于收盘（陈旧）：15:05 跑 eod 时拿到 10:00 那份，量只累计了半天，
    写成收盘 bar 会把量能压低一截，还骗过 C2 的后续判定。

    判据是「时间戳早于 close_time」，**不设宽限**——14:56 的快照只差 1 分钟，
    但它不含收盘集合竞价的量，写进 daily_bars 会把量能基准永久压小。"""
    if quote.ts is None:
        return True
    ts = quote.ts if quote.ts.tzinfo is not None else quote.ts.replace(tzinfo=SH)
    h, m = close_time.split(':')[:2]
    close_at = datetime.combine(ts.astimezone(SH).date(), time(int(h), int(m)), tzinfo=SH)
    return ts.astimezone(SH) < close_at


def _bar(code: str, quote, today: int, source: str) -> Bar:
    """快照 → 当日 raw bar。四价 `f[5]/f[33]/f[34]/f[3]` 已由数据源按索引归位到 Quote。"""
    return Bar(
        code=code,
        date=today,
        open=quote.open,
        high=quote.high,
        low=quote.low,
        close=quote.price,
        volume=quote.volume or 0.0,
        amount=quote.amount or 0.0,
        source=source,
    )


def _write_implied(conn, code: str, ex_date: int, implied_factor: float) -> bool:
    """写一条快照反推的除权事件；已有 `westock_dividend` 或已写过的同日记录则不动。"""
    official = conn.execute(
        "SELECT 1 FROM corporate_actions WHERE code = ? AND ex_date = ? AND source = 'westock_dividend'",
        (code, ex_date),
    ).fetchone()
    if official is not None:
        return False
    if not (implied_factor and implied_factor > 0):
        return False
    existing = conn.execute(
        "SELECT implied_factor FROM corporate_actions"
        " WHERE code = ? AND ex_date = ? AND source = 'snapshot_implied'", (code, ex_date)).fetchone()
    if existing is not None:
        # 盘中快照已写过（口径可能是「现价/昨收」，差一个数量级）。eod 有真实日线，
        # 是更权威的一方：值不同就**覆盖**，相同则不算一次新发现（避免 exrights 重复计数）
        if existing[0] is not None and abs(float(existing[0]) - float(implied_factor)) < 1e-9:
            return False
        conn.execute(
            "UPDATE corporate_actions SET implied_factor = ?"
            " WHERE code = ? AND ex_date = ? AND source = 'snapshot_implied'",
            (implied_factor, code, ex_date))
        return True
    cur = conn.execute(
        "INSERT OR IGNORE INTO corporate_actions"
        "(code, ex_date, source, cash_per_share, bonus_ratio, tran_ratio, implied_factor) "
        "VALUES (?,?,'snapshot_implied',0,0,0,?)",
        (code, ex_date, implied_factor),
    )
    return cur.rowcount > 0


def _sync_status(conn, code: str, name: str, today: int, prev_day: int | None) -> None:
    """名称前缀变化时关旧区间、开新区间（决定 #8：首次部署写 `19900101`）。"""
    status = st_status_of(name)
    row = conn.execute(
        "SELECT start_date, st_status FROM stock_status_history "
        "WHERE code = ? ORDER BY start_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO stock_status_history(code, start_date, end_date, st_status) "
            "VALUES (?,?,NULL,?)",
            (code, FIRST_DEPLOY_DATE, status),
        )
        return
    if row[1] == status:
        return
    conn.execute(
        "UPDATE stock_status_history SET end_date = ? WHERE code = ? AND start_date = ?",
        (prev_day or today, code, row[0]),
    )
    conn.execute(
        "INSERT OR REPLACE INTO stock_status_history(code, start_date, end_date, st_status) "
        "VALUES (?,?,NULL,?)",
        (code, today, status),
    )


def _backfill_gap(reg, conn, code: str, now: datetime) -> bool:
    """单只补拉最近 30 根；上游全挂时不拖垮整轮 eod。

    **只写今天之前的 bar**：`kline_daily` 会带上今天的盘中 K 线，直接 INSERT OR REPLACE
    会把刚验收的快照收盘价/成交量覆盖成上游那一刻的值（15:05 的收盘被 14:00 的覆盖）。
    **只写主源的 bar**：新浪日线在本设计里只用于校验，不落 `daily_bars`（决定 #3）。
    """
    from core.http import ForbiddenHost, UpstreamBlocked, UpstreamError, UpstreamTimeout

    try:
        bars, used = reg.kline_daily(code, GAP_COUNT)
    except (UpstreamError, UpstreamBlocked, UpstreamTimeout, ForbiddenHost):
        # **不覆盖已成功的 ok 行**：`snapshot_eod` 那一行表示「今天的快照已入库」，
        # 改写成 error/0 之后，事后完全看不出这只票其实成功了
        # （补拉失败另记 `gap_backfill` 用途，见下）。
        # 单独记一行 `gap_backfill`（schema 004 加的 purpose），不去碰
        # `snapshot_eod` 那行——那一行代表「今天的快照已入库」
        _log(conn, code, 0, "error", now, purpose="gap_backfill")
        return False
    today = int(now.strftime("%Y%m%d"))
    bars = [b for b in bars if b.date < today and (used == "tencent" or b.source == "tencent")]
    if bars:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO daily_bars(code,date,open,high,low,close,volume,amount,source) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(b.code, b.date, b.open, b.high, b.low, b.close, b.volume, b.amount, b.source) for b in bars],
            )
    return True


def _log(conn, code: str, n: int, status: str, now: datetime,
         purpose: str = "snapshot_eod", keep_ok: bool = False) -> None:
    """写 `fetch_log`。

    `keep_ok=True` 时不覆盖已经 `ok` 的行：同一个 `(code, purpose)` 只有一行，
    后来的失败不该把「已成功」抹掉。
    """
    if keep_ok:
        conn.execute(
            "INSERT INTO fetch_log(code, purpose, n, status, ts) VALUES (?,?,?,?,?) "
            "ON CONFLICT(code, purpose) DO UPDATE SET n=excluded.n, status=excluded.status,"
            " ts=excluded.ts WHERE fetch_log.status <> 'ok'",
            (code, purpose, n, status, now.isoformat()),
        )
        return
    conn.execute(
        "INSERT INTO fetch_log(code, purpose, n, status, ts) VALUES (?,?,?,?,?) "
        "ON CONFLICT(code, purpose) DO UPDATE SET n=excluded.n, status=excluded.status, ts=excluded.ts",
        (code, purpose, n, status, now.isoformat()),
    )


def _prev(cal: TradingCalendar, today: int) -> int | None:
    try:
        return cal.prev(today)
    except LookupError:
        return None


def _as_date(ymd: int) -> date:
    return datetime.strptime(str(ymd), "%Y%m%d").date()


def _minutes(value) -> int:
    """`time(14, 57)` 或 `"14:57"` → 当日第几分钟（跨小时比较也正确）。"""
    parts = str(value).split(":")
    return int(parts[0]) * 60 + int(parts[1])
