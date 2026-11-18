"""盘中 Tick：把快照喂进规则链与检测器（spec §5.1 的三循环 → 检测 → 派发）。

**跨线程纪律（硬性）**：`on_pool`（pool 线程）、`on_sector`（sector 线程）、
`tick()`（APScheduler 的 1 秒 job 线程）、`capture_auction`（09:25:30 定时线程）
共用主线程创建的一个 `conn`（`check_same_thread=False`）。SQLite 的 `busy_timeout`
只挡**另一个连接**，挡不住同一连接跨线程的交错事务——所以「检测 + ingest +
dispatcher.handle」「dispatcher.tick()」「竞价写库」全部在同一把锁内串行。
调度器注册的是 `tick.tick`（加锁包装），**不是** `dispatcher.tick`。
"""
from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace

from core.clock import snapshot_is_today
from core.intraday.detector import _PRICE_KINDS, EventDetector
from core.intraday.rules import TickState, eval_pool_stock
from core.intraday.watch_rules import MoveRing, eval_auction, eval_index, eval_sector, eval_watch


def _is_pool(focus_value) -> bool:
    """`focus_of(code)` 的返回值是不是"池内"。

    它可能是档位名（`"pool"` / `"outside"`），也可能是数值权重（测试里常用 `"1.0"`）。
    两种都要认——只比字符串会让数值形式全被判成池外，竞价高低开被整批静音。
    """
    if isinstance(focus_value, (int, float)):
        return float(focus_value) >= 1.0
    raw = str(focus_value).strip()
    if raw == "pool":
        return True
    if raw in ("outside", "market", "sector", ""):
        return False
    try:
        return float(raw) >= 1.0
    except (TypeError, ValueError):
        return False
from core.market import classify_market
from notify.templates import BriefRow


def _hm(now: datetime) -> str:
    return now.strftime("%H:%M")


class IntradayTick:
    def __init__(self, cfg, conn, detector: EventDetector, dispatcher, clock):
        self.cfg = cfg
        self.conn = conn
        self.detector = detector
        self.dispatcher = dispatcher
        self.clock = clock
        self.lock = threading.Lock()
        self.ring = MoveRing(int(cfg.strategy.intraday.index_move.window_s))
        self._states: dict[str, TickState] = {}
        self._freezer = None                      # 惰性建：一次实例被三个市场共用
        self._day = clock.today()                 # 长驻状态所属的交易日（跨日整体重置）

    # ---- 锁内包装 --------------------------------------------------------

    def tick(self) -> None:
        """调度器的 1 秒 job 调这里（不是 `dispatcher.tick`——那会绕过锁）。"""
        with self.lock:
            today = self.clock.today()
            if today != self._day:
                self._reset_day(today)
            self.dispatcher.tick()

    def _reset_day(self, today: int) -> None:
        """跨日重置全部长驻状态。

        `VolumeFreezer` 的冻结量比、每股票状态、检测器的武装/seq/冷却都是「当日」语义；
        不重置的话，昨天 14:57 冻结的量能比会被今天原样复用，昨天的 breakout 也不会
        重新武装（第二天再突破直接静默）。
        """
        self._day = today
        self._states.clear()
        self._freezer = None
        self.ring = MoveRing(int(self.cfg.strategy.intraday.index_move.window_s))
        det = self.detector
        for name in ("_armed", "_seq", "_last_fire"):
            cache = getattr(det, name, None)
            if isinstance(cache, dict):
                cache.clear()
        fired = getattr(det, "_daily_fired", None)
        if isinstance(fired, set):
            fired.clear()
        det.seed_today()                          # 今天的 event_log（重启/跨日后）重新播种

    # ---- 池内循环 --------------------------------------------------------

    def on_pool(self, quotes: dict, now: datetime) -> None:
        with self.lock:
            dets = self._pool_dets(quotes, now)
            dets += self._watch_dets(quotes, now)
            dets += self._index_dets(quotes, now)
            self._ingest(dets, now)

    def on_sector(self, rows: list, now: datetime) -> None:
        with self.lock:
            dets = eval_sector(self.ring, rows, now, self.cfg.strategy.intraday)
            # 板块快照只含板块行、不含个股报价：绝不能拿它当「完整报价集」重新武装个股
            self._ingest(dets, now, rearm=False)

    # ---- 竞价 ------------------------------------------------------------

    def capture_auction(self, quotes: dict, baselines: dict[str, list[float] | None],
                        focus_of) -> None:
        """09:25:30：写 `auction_daily`（股/元）+ 检测进 `event_log` + 交开盘简报。

        报价来自 pool/market 快照缓存，**本身不发上游请求**（也就不记 `auction` scope）。
        `ts` 不是今天的行整行跳过：非交易日 / 错日快照不能污染 `auction_daily`。
        """
        with self.lock:
            now = self.clock.now()
            today = int(now.strftime("%Y%m%d"))
            today_date = now.date()
            dets: list = []
            # **池外票只报竞价量异常**：全市场每天几百只高低开 2%，那不是异动是常态。
            # 把池内（含自选）以外的都算进来，`auction_anomaly` 一天 860 条，
            # 把 salience 分布压死、阈值失效（实测 97.6% 被抑制）。
            focus = focus_of      # 它本身就是 `focus(code) -> 档位` 这个函数
            with self.conn:
                for code, q in quotes.items():
                    if q.volume is None:
                        continue
                    if q.ts is not None and not snapshot_is_today(q.ts, today_date):
                        continue
                    self.conn.execute(
                        "INSERT OR REPLACE INTO auction_daily(code, trade_date, auction_vol, auction_amt) "
                        "VALUES (?,?,?,?)",
                        (code, today, q.volume, q.amount or 0.0),
                    )
                    in_focus = True if focus is None else _is_pool(focus(code))
                    dets += eval_auction(q, baselines.get(code), self.cfg.strategy.intraday,
                                         in_focus=in_focus)
            self.detector.ingest(dets, focus_of)
            rows, overflow, pool_date = self._brief_rows(quotes, today)
            baseline_days = max((len(v or ()) for v in baselines.values()), default=0)
            if self.dispatcher is not None:
                self.dispatcher.open_brief(
                    rows, len(rows), overflow, pool_date, baseline_days=baseline_days,
                    auction_detections=dets)

    def _brief_rows(self, quotes: dict, today: int) -> tuple[list[BriefRow], int, int]:
        """池内 Top N 的 `BriefRow`（模板按属性取值，传 `list[str]` 会 AttributeError）。"""
        pool = self._pool_rows()
        top = int(self.cfg.notify.open_brief_top)
        rows: list[BriefRow] = []
        for code, row in pool.items():
            q = quotes.get(code)
            if q is None or q.price is None:
                continue
            chg = (q.price / q.prev_close - 1) if q.prev_close else 0.0
            rows.append(BriefRow(
                code6=code[2:] if len(code) > 6 else code,
                name=q.name or code,
                price=float(q.price),
                score=float(row.score or 0.0),
                chg=float(chg),
            ))
        rows.sort(key=lambda r: r.score, reverse=True)
        pool_row = self.conn.execute(
            "SELECT max(trade_date) FROM pool WHERE trade_date < ?", (today,)).fetchone()
        pool_date = int(pool_row[0]) if pool_row and pool_row[0] else today
        return rows[:top], max(0, len(rows) - top), pool_date

    # ---- 内部 ------------------------------------------------------------

    def _state(self, code: str) -> TickState:
        st = self._states.get(code)
        if st is None:
            st = TickState()
            self._states[code] = st
        return st

    def _freezer_of(self):
        from core.intraday.curve import VolumeFreezer

        if self._freezer is None:
            self._freezer = VolumeFreezer(self.cfg.strategy.intraday.freeze_at)
        return self._freezer

    def _pool_rows(self) -> dict[str, dict]:
        today = int(self.clock.now().strftime("%Y%m%d"))
        row = self.conn.execute(
            "SELECT max(trade_date) FROM pool WHERE trade_date < ?", (today,)).fetchone()
        if not row or not row[0]:
            return {}
        return {r[0]: SimpleNamespace(market=r[1], box_high_raw=r[2], box_low_raw=r[3],
                                      close_raw=r[4], box_vol_mean=r[5], score=r[6])
                for r in self.conn.execute(
            "SELECT code, market, box_high_raw, box_low_raw, close_raw, box_vol_mean, score "
            "FROM pool WHERE trade_date = ?", (row[0],))}

    def _pool_dets(self, quotes: dict, now: datetime) -> list:
        rows = self._pool_rows()
        out = []
        hm = _hm(now)
        for code, q in quotes.items():
            row = rows.get(code)
            if row is None:
                continue
            dets = eval_pool_stock(
                q, row, self.cfg.market(row.market), self.cfg.strategy, hm,
                self._state(code), self._freezer_of(), now,
            )
            # 只看**本只票**的检测：`out[-1]` 是全列表末项，前面某只除权会让后面每只都写一条
            if any(d.kind == "data_anomaly" and d.payload.get("reason") == "exright" for d in dets):
                self._record_implied(code, now, q, prev_raw=getattr(row, "close_raw", None))
            out += dets
        return out

    def _record_implied(self, code: str, now: datetime, q=None, prev_raw=None) -> None:
        """除权日快照留痕：写一条 `snapshot_implied`（OR IGNORE，与 eod 的写者同键）。

        **`implied_factor` 必须写**：留 NULL 时 `core.adjust._implied` 会抛 ValueError，
        整条复权链（以及依赖它的所有因子）直接崩。算不出正因子就不写，宁缺一条也不能留毒行。

        **口径必须与 eod 一致：`上一根 raw close / 快照昨收`**。写成「快照现价 / 快照昨收」
        得到的是**当日涨跌幅**（实测 5.12/5.02=1.0199），而两融除权时它差一个数量级
        （正确值 10.04/5.02=2.0）；盘中写进去后 eod 的 `INSERT OR IGNORE` 会保留这个错值。
        """
        factor = None
        # 触发本次检测的就是池子里的 T−1 raw 收盘（`eval_pool_stock` 拿它跟快照昨收比），
        # 所以它才是正确分子；daily_bars 只作兜底
        prev_raw_close = prev_raw or self._prev_raw_close(code, now)
        if prev_raw_close and q is not None and q.prev_close:
            try:
                candidate = float(prev_raw_close) / float(q.prev_close)
            except (TypeError, ValueError, ZeroDivisionError):
                candidate = None
            if candidate is not None and candidate > 0 and candidate != float("inf"):
                factor = candidate
        if factor is None:
            return
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO corporate_actions(code, ex_date, source, implied_factor) "
                "VALUES (?, ?, 'snapshot_implied', ?)",
                (code, int(now.strftime("%Y%m%d")), factor),
            )

    def _prev_raw_close(self, code: str, now: datetime) -> float | None:
        """上一根**已收盘**的不复权收盘（除权前一日），与 eod 的 `last_close` 同口径。"""
        row = self.conn.execute(
            "SELECT close FROM daily_bars WHERE code = ? AND date < ?"
            " ORDER BY date DESC LIMIT 1", (code, int(now.strftime("%Y%m%d")))).fetchone()
        return float(row[0]) if row and row[0] else None

    def _watch_dets(self, quotes: dict, now: datetime) -> list:
        out = []
        hm = _hm(now)
        watch = {r[0]: r[1] for r in self.conn.execute(
            "SELECT code, pct_threshold FROM watch_stock")}
        for code, q in quotes.items():
            if code not in watch:
                continue
            hist, ma20 = self._watch_hist(code)
            out += eval_watch(q, hist, ma20, self.cfg.strategy.intraday, hm,
                              pct_threshold=watch[code],
                              price_eps=self.cfg.strategy.universe.price_eps)
        return out

    def _watch_hist(self, code: str):
        rows = self.conn.execute(
            "SELECT date, high, low, close FROM daily_bars WHERE code = ? ORDER BY date DESC LIMIT 60",
            (code,)).fetchall()
        if not rows:
            return [], 0.0
        hist = [SimpleBar(r[0], r[1], r[2], r[3]) for r in rows]
        closes = [r[0] for r in self.conn.execute(
            "SELECT close FROM daily_bars WHERE code=? ORDER BY date DESC LIMIT 20", (code,))]
        ma20 = sum(closes) / len(closes)
        return hist, ma20

    def _index_dets(self, quotes: dict, now: datetime) -> list:
        prices = {code: q.price for code, q in quotes.items()
                  if code in _INDEX_CODES and q.price is not None}
        if not prices:
            return []
        return eval_index(self.ring, prices, now, self.cfg.strategy.intraday)

    def focus_of(self):
        """`_focus_of` 的公开别名（`jobs/main.py` 的竞价任务要传它给 `capture_auction`）。"""
        return self._focus_of()

    def _focus_of(self):
        """code → focus 档位：池内 `"pool"`，其余 `"outside"`。

        **这里刻意不含自选**：`detector._focus()` 判权重时先看 `det.watch`
        直接返回 `focus.watch`，自选的加权不走这个函数。

        **但竞价判定要用它**，而竞价的"值不值得报"与"salience 权重"是两回事：
        用户只加自选、没进候选池的票，照样是他最关心的。所以竞价那边不能用
        这个函数，要用 `auction_scope_of()`（见下）。
        """
        codes = set(self._pool_rows())

        def focus(code: str) -> str:
            return "pool" if code in codes else "outside"

        return focus

    def auction_scope_of(self):
        """竞价专用：池内 **或自选** 都算"重点"，恒返回 `"pool"` / `"outside"`。

        原先把 `focus_of()` 直接传给 `capture_auction`，而它只查候选池——
        单独自选、没进池的票被判 `outside`，高开 +5% 也不产生事件（v0.2.70 回归）。
        """
        pool = set(self._pool_rows())
        watch = {r[0] for r in self.conn.execute("SELECT code FROM watch_stock")}

        def scope(code: str) -> str:
            return "pool" if (code in pool or code in watch) else "outside"

        return scope

    def _ingest(self, dets: list, now: datetime, *, rearm: bool = True) -> None:
        """喂检测器。**空列表也要喂**：`ingest` 的重新武装就靠「本 tick 没再出现」实现。

        `rearm=False`（板块线程）时禁止改动个股的武装位——板块快照没有个股报价，
        拿它当完整报价集会把这轮没出现的 breakout 全部重新武装，回箱后再突破就重复推。
        """
        if not dets and not rearm:
            return
        focus = self._focus_of()
        if rearm:
            events = self.detector.ingest(dets, focus)
        else:
            armed = {k: v for k, v in self.detector._armed.items() if k[0] in _PRICE_KINDS}
            events = self.detector.ingest(dets, focus)
            self.detector._armed.update(armed)
        if events and self.dispatcher is not None:
            self.dispatcher.handle(events)


class SimpleBar:
    """`eval_watch` 需要的最小 bar 形状（`high`/`low`/`close`）。"""

    __slots__ = ("date", "high", "low", "close")

    def __init__(self, date: int, high: float, low: float, close: float):
        self.date = date
        self.high = high
        self.low = low
        self.close = close


_INDEX_CODES = ("sh000001", "sz399001", "sz399006", "sh000688")
