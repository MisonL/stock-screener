"""23 个只读工具（§7.5.2 的 13 个 + 盯盘专用 4 个 + westock-mcp 封装 6 个，D22）。

**判定顺序（计划 Task 3 硬约束）**：`invoke` 一进来先拦 4 个 mcp 写工具——任何回合
（含 chat）被模型直接调用一律 `needs_confirm`，写操作只能从确认卡回调执行；这一步在
委派之前，不碰手机、也不依赖 `mcp` 是否为 None。其余 `mcp_*` 委派给注入的 `WestockMcp`
（Task 6），最后才是本地实现。

所有返回都是统一信封 `envelope()`，异常一律 `ok: false`，绝不抛给调用方。
6 个 mcp 工具的 schema 声明在本文件（`MCP_TOOL_SCHEMAS`，Task 6 原样引用，不另造一份）；
`mcp=None` 时工具仍在 schema 里，`.invoke` 返回 `mcp_unavailable`——23/19 的工具数
断言在 Task 3 就钉死，Task 6 上线后无需改 schema。
"""
from __future__ import annotations

import copy

import json
from collections import OrderedDict
from typing import Any

from llm.schemas import _CODE, _DATE_OPT, MCP_TOOL_SCHEMAS, _fn, _opt  # noqa: F401  (再导出)
from llm.schemas import envelope  # noqa: F401
from core.adjust import to_raw
from core.clock import TradingCalendar
from core.datasource.westock import WestockCli
from core.http import RateLimiter, UpstreamError, UpstreamTimeout
from core.market import classify_market
from core.models import Reject
from core.snapcache import SnapshotCache
from core.strategy.box import find_box
from core.strategy.factors import compute_factors
from core.strategy.pool import (
    _ex_refs,
    _load_actions,
    _load_adj,
    _load_bars,
    _load_factors,
    _load_index,
    _load_status,
    _load_suspensions,
    _resume_dates,
    _st_lookup,
)
from core.strategy.scoring import score
from core.strategy.filters import exclude
from core.strategy.window import annotate, decline_window

WESTOCK_MIN_INTERVAL_S = 0.3          # agent 进程自己的限速器（D18：只打 westock，≥300ms）
CACHE_LIMIT = 256
# 四类 westock CLI 工具的进程内 TTL 缓存（计划 Task 3 定死）：key = (tool, sorted(args))，
# 只存成功信封，命中返回上次信封（as_of 保持原值——出处校验按 as_of 判时效）。
CACHE_TTL_S = {"get_chip": 600, "get_notices": 300, "get_fund_flow": 60, "get_sector_history": 60}

MCP_WRITE_TOOLS = ("mcp_add_watch", "mcp_remove_watch", "mcp_set_alert", "mcp_cancel_alert")
MCP_READ_TOOLS = ("mcp_read_watchlist", "mcp_read_alerts")

KINDS = ("event", "heartbeat", "chat", "alarm", "auction", "open", "close")


# ---------------------------------------------------------------- schema 组装


# 13 个基线工具（§7.5.2）
_BASE_SCHEMAS = [
    _fn("get_quote", "最新价、涨跌幅、成交量、量比（实时快照缓存）", {"code": _CODE}),
    # **不暴露 adj**（spec §7.5.2 复权基准第 3 条：adj 只在 compare_pattern 内部用）。
    # 模型拿复权序列跟 get_quote 的实时 raw 价比较，是 spec 自己点名的「校验全过、结论全错」陷阱
    _fn("get_kline", "日线 OHLCV（不复权，与实时价同基准）",
        {"code": _CODE, "days": _opt("integer")}),
    _fn("get_pattern", "策略层现算：N*、箱体上下沿（raw 基准）、各因子原值、总分、是否在池",
        {"code": _CODE}),
    _fn("get_pool", "观察池 Top N（默认最新一日）", {"date": _DATE_OPT, "limit": _opt("integer")}),
    _fn("get_signals", "信号列表（默认最新一日）", {"date": _DATE_OPT, "limit": _opt("integer")}),
    _fn("get_sector_rotation", "行业/概念板块当日涨跌幅排行与领涨股"),
    _fn("get_sector_history", "板块指数日线；主源 westock CLI，失败回落本地等权合成",
        {"code": {"type": "string", "description": "板块代码（pt… 前缀）或板块名"}, "days": _opt("integer")}),
    _fn("get_market_breadth", "涨跌家数、涨停跌停数、两市成交额"),
    _fn("get_corporate_actions", "除权除息事件（日期、每股现金、送转比例）", {"code": _CODE}),
    _fn("get_chip", "筹码：获利比例、平均成本、集中度（westock，仅 A 股）", {"code": _CODE}),
    _fn("get_notices", "近期公告标题与时间（westock）", {"code": _CODE, "n": _opt("integer")}),
    _fn("get_fund_flow", "主力/超大单/中单/小单净流入（westock）", {"code": _CODE}),
    _fn("compare_pattern", "形态相似度与差异点（复权 z-score + 皮尔逊）",
        {"code": _CODE, "ref_start": {"type": "string", "description": "参考窗口起点 YYYYMMDD"},
         "ref_end": _opt("string")}),
]

# 4 个盯盘专用（§5.4.5）
_AGENT_SCHEMAS = [
    _fn("get_index_snapshot", "5 个指数（上证/深证成指/创业板指/科创50/沪深300）实时点位与成交额"),
    _fn("get_event_context", "某事件触发前后的相关事件（同板块、同时段）",
        {"event_id": {"type": "integer"}}),
    _fn("get_own_history", "自己说过什么、排了什么闹钟（防重复说话）",
        {"date": _DATE_OPT}),
    _fn("get_watch_quote", "自选股实时行情（自选股独立于观察池）"),
]

_MCP_BY_NAME = {t["function"]["name"]: t for t in MCP_TOOL_SCHEMAS}

# 三套 schema（D22 字面口径）：chat 全含 23；event == heartbeat == 19（23 − 4 个写工具）
_CHAT_TOOLS = [dict(t) for t in _BASE_SCHEMAS + _AGENT_SCHEMAS + MCP_TOOL_SCHEMAS]
_EVENT_TOOLS = [dict(t) for t in _BASE_SCHEMAS + _AGENT_SCHEMAS
                + [_MCP_BY_NAME[n] for n in MCP_READ_TOOLS]]

# 6 种 wake_kind（+chat）先映射到 3 套 schema：heartbeat/alarm/auction/open/close 复用同一套
_KIND_TO_FAMILY = {"event": "event", "chat": "chat",
                   "heartbeat": "heartbeat", "alarm": "heartbeat",
                   "auction": "heartbeat", "open": "heartbeat", "close": "heartbeat"}


# ---------------------------------------------------------------- 信封


# ---------------------------------------------------------------- 工具注册表


class ToolRegistry:
    """23 个工具的 schema 与执行入口。

    `mcp` 是 Task 6 的 `WestockMcp` 实例（读方法名 = 工具名去掉 `mcp_` 前缀）；
    `cli` 属性是 westock CLI（惰性创建 `WestockCli`，测试注入假体：`reg.cli = FakeCli()`）。
    """

    def __init__(self, conn, cfg, clock, mcp=None):
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.mcp = mcp
        self.cli: Any = None                 # None = 首次 westock 调用时惰性创建
        self.westock_limiter = RateLimiter(WESTOCK_MIN_INTERVAL_S)
        self._snap = SnapshotCache(lambda: conn)
        self._cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._returns: dict[str, list[dict]] = {}

    # ---- 回合与查询 -------------------------------------------------------

    def begin_turn(self) -> None:
        """新回合开始：清空本轮返回记录（Task 10 的回合循环在回合开头调）。"""
        self._returns.clear()

    @property
    def returns_this_turn(self) -> dict[str, list[dict]]:
        """本轮每个工具的全部返回信封（Task 5 出处校验按它取值）。"""
        return {name: list(envs) for name, envs in self._returns.items()}

    def schema(self, kind: str) -> list[dict]:
        if kind not in KINDS:
            raise ValueError(f"unknown wake_kind: {kind!r}（合法值：{KINDS}）")
        family = _KIND_TO_FAMILY[kind]
        return copy.deepcopy(_CHAT_TOOLS if family == "chat" else _EVENT_TOOLS)

    # ---- invoke：判定顺序固定 ---------------------------------------------

    def invoke(self, name: str, args: dict | None, kind: str) -> dict:
        """永远返回信封；异常一律 ok:false，绝不抛给调用方。`kind` 必传。

        **整个函数体都在 try 里**：kind 校验、`dict(args or {})` 的归一化也一样——
        模型传 `args=[...]`（合法 JSON 数组）会在 try 外抛 ValueError，契约就破了。
        """
        if kind not in KINDS:
            # kind 是**调用方**（我们自己的代码）传的，不是模型输出：编程错误就该炸
            raise ValueError(f"unknown wake_kind: {kind!r}（合法值：{KINDS}）")
        now = self.clock.now().isoformat()
        try:
            raw_args = args or {}
            if kind not in KINDS:
                raise ValueError(f"unknown wake_kind: {kind!r}（合法值：{KINDS}）")
            if not isinstance(raw_args, dict):
                raise TypeError(f"args 必须是对象，收到 {type(raw_args).__name__}")
            env = self._dispatch(name, dict(raw_args), now)
        except Exception as exc:                          # noqa: BLE001 —— 兜底，不抛给调用方
            env = envelope(ok=False, reason=f"tool_error:{type(exc).__name__}", as_of=now)
        self._returns.setdefault(name, []).append(env)
        return env

    def _dispatch(self, name: str, args: dict, now: str) -> dict:
        # ① 写工具：任何回合（含 chat）都拦在委派之前，不碰手机、不依赖 mcp 是否为 None
        if name in MCP_WRITE_TOOLS:
            return envelope(ok=False, reason="needs_confirm", as_of=now)
        # ② 其余 mcp_* 委派给注入的 WestockMcp
        if name in _MCP_BY_NAME:
            if self.mcp is None:
                return envelope(ok=False, reason="mcp_unavailable", as_of=now)
            return self._delegate_mcp(name, args, now)
        # ③ 本地实现
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return envelope(ok=False, reason="unknown_tool", as_of=now)
        return handler(args, now)

    def _delegate_mcp(self, name: str, args: dict, now: str) -> dict:
        method = name[len("mcp_"):]
        try:
            raw = getattr(self.mcp, method)(**args)
        except Exception as exc:                          # noqa: BLE001
            return envelope(ok=False, reason=f"mcp_error:{type(exc).__name__}", as_of=now)
        if isinstance(raw, dict) and "ok" in raw:
            env = dict(raw)
            env.setdefault("data", None)
            env.setdefault("as_of", now)
            env.setdefault("reason", None)
            env.setdefault("adjust", None)
            return env
        return envelope(data=raw, as_of=now)

    # ---- 本地工具 ---------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat()

    def _need(self, args: dict, key: str) -> bool:
        return args.get(key) not in (None, "")

    def _cli_instance(self):
        if self.cli is None:
            self.cli = WestockCli()
        return self.cli

    def _cached(self, tool: str, args: dict, now: str, compute) -> dict:
        """进程内 TTL 缓存：只存成功信封；命中返回上次信封（as_of 保持原值）；LRU 256。"""
        key = (tool, tuple(sorted(args.items())))
        hit = self._cache.get(key)
        if hit is not None:
            expires, env = hit
            if self.clock.now().timestamp() < expires:
                self._cache.move_to_end(key)
                return env
            self._cache.pop(key, None)
        env = compute()
        if env.get("ok"):
            self._cache[key] = (self.clock.now().timestamp() + CACHE_TTL_S[tool], env)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_LIMIT:
                self._cache.popitem(last=False)
        return env

    def _westock_call(self, tag: str, call) -> dict:
        """限速 → 调 CLI；超时 → westock_timeout，其它上游错误 → westock_error。"""
        now = self._now()
        self.westock_limiter.acquire()
        try:
            return envelope(data=call(), as_of=now)
        except UpstreamTimeout:
            return envelope(ok=False, reason="westock_timeout", as_of=now)
        except UpstreamError:
            return envelope(ok=False, reason="westock_error", as_of=now)

    def _tool_get_quote(self, args, now):
        if not self._need(args, "code"):
            return envelope(ok=False, reason="bad_args", as_of=now)
        code = str(args["code"])
        for scope in ("pool", "market"):
            row = self._snap.row_for(scope, code)
            if row is not None:
                snap = self._snap.get(scope)
                return envelope(data=row, as_of=snap.get("as_of") or now, adjust="raw")
        return envelope(ok=False, reason="no_quote", as_of=now)

    def _tool_get_kline(self, args, now):
        if not self._need(args, "code"):
            return envelope(ok=False, reason="bad_args", as_of=now)
        code = str(args["code"])
        adjust = args.get("adjust") or "raw"              # §7.5.2：默认 raw（与实时价同基准）
        if adjust not in ("raw", "adj"):
            return envelope(ok=False, reason="bad_adjust", as_of=now)
        days = min(max(int(args.get("days") or 120), 1), 1700)
        table = "daily_bars" if adjust == "raw" else "daily_bars_adj"
        rows = self.conn.execute(
            f"SELECT date, open, high, low, close, volume FROM {table}"
            " WHERE code = ? ORDER BY date DESC LIMIT ?", (code, days)).fetchall()
        bars = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                for r in reversed(rows)]
        return envelope(data={"code": code, "adjust": adjust, "bars": bars}, as_of=now, adjust=adjust)

    def _tool_get_pattern(self, args, now):
        if not self._need(args, "code"):
            return envelope(ok=False, reason="bad_args", as_of=now)
        code = str(args["code"])
        market = classify_market(code)
        if market is None:
            return envelope(ok=False, reason="bad_code", as_of=now)
        p, s = self.cfg.market(market), self.cfg.strategy
        as_of = self.clock.today()
        raw = _load_bars(self.conn, [code], as_of).get(code, [])
        adj = _load_adj(self.conn, [code], as_of).get(code, [])
        if not raw or len(adj) != len(raw):
            return envelope(ok=False, reason="no_data", as_of=now)
        # **与 _evaluate 同一顺序的三条拒绝**：少一条，模型就会把池外的票当
        # 「合格形态」，in_pool=False 与 score 并存自相矛盾（实测除权日 61.3 分）
        actions = _load_actions(self.conn, [code]).get(code, [])
        if any(int(a["ex_date"]) == as_of for a in actions):
            return envelope(ok=False, reason="reject_exright_exright", as_of=now)
        anchor = adj[-1].date                             # 因子桥接日 = 最新复权 bar（= 池子的 trade_date）
        factor_row = _load_factors(self.conn, [code], anchor).get(code)
        if factor_row is None:
            return envelope(ok=False, reason="no_factor", as_of=now)
        factor, _vol_factor = factor_row
        if factor <= 0 or _vol_factor <= 0:
            return envelope(ok=False, reason="reject_data_bad_factor", as_of=now)
        cal = TradingCalendar(self.conn)
        status_rows = _load_status(self.conn, [code]).get(code, [])
        susp = _load_suspensions(self.conn, [code]).get(code, [])
        rows = annotate(
            raw, adj, market,
            _st_lookup(status_rows),
            _ex_refs(raw, actions),
            _resume_dates(susp, cal, as_of),
        )
        box = find_box(rows, p, s.box)
        if isinstance(box, Reject):
            return envelope(ok=False, reason=f"reject_{box.stage}_{box.reason}", as_of=now)
        decl = decline_window(rows, box.start, s.decline.tail_max)
        if decl is None:
            return envelope(ok=False, reason="reject_decline_no_decline", as_of=now)
        fv = compute_factors(rows, box, decl, _load_index(self.conn, as_of), p, s)
        if isinstance(fv, Reject):
            return envelope(ok=False, reason=f"reject_{fv.stage}_{fv.reason}", as_of=now)
        # st 取**当日生效区间**的状态（与粗筛 SQL 同口径）；_st_lookup 是给逐根 bar
        # 判涨跌停用的「日期→状态」函数，不是状态字符串，直接传会把函数当 ST 标记
        st_row = self.conn.execute(
            "SELECT st_status FROM stock_status_history WHERE code = ? AND start_date <= ?"
            " AND (end_date IS NULL OR end_date >= ?) ORDER BY start_date DESC LIMIT 1",
            (code, as_of, as_of)).fetchone()
        st_status = st_row[0] if st_row else "normal"
        # exclude 的口径与 _evaluate 相同：st/n_bars/amount_20d/suspended_250 走物化表
        stat = self.conn.execute(
            "SELECT COALESCE(ss.n_bars,0), COALESCE(ss.amount_20d,0), COALESCE(ss.suspended_250,0),"
            " m.name FROM stock_meta m LEFT JOIN stock_stats ss ON ss.code = m.code"
            " WHERE m.code = ?", (code,)).fetchone()
        window = rows[box.start : box.start + box.n]
        box_amount = sum(r.amount for r in window) / box.n if window else None
        excl = exclude(code, {"name": (stat[3] if stat else None) or "", "st_status": st_status},
                       stat[0] if stat else 0, stat[1] if stat else 0.0, box_amount,
                       stat[2] if stat else 0, st_status, p, s.universe)
        if excl is not None:
            return envelope(ok=False, reason=f"reject_exclude_{excl}", as_of=now)
        result = score(fv, p, s)
        pool_day = self.conn.execute("SELECT max(trade_date) FROM pool WHERE market = ?", (market,)).fetchone()[0]
        in_pool = bool(pool_day) and self.conn.execute(
            "SELECT 1 FROM pool WHERE trade_date = ? AND market = ? AND code = ?",
            (pool_day, market, code)).fetchone() is not None
        data = {
            "code": code, "market": market, "trade_date": anchor,
            "box_days": box.n, "box_high": to_raw(box.high, factor), "box_low": to_raw(box.low, factor),
            "pos_in_box": box.pos_in_box,
            "score": result.score,
            "factors": {k: item[0] for k, item in result.items.items()},
            "in_pool": in_pool,
        }
        return envelope(data=data, as_of=now, adjust="raw")

    def _tool_get_pool(self, args, now):
        date = args.get("date")
        day = self.conn.execute(
            "SELECT max(trade_date) FROM pool WHERE trade_date <= ?",
            (int(date) if date else 99999999,)).fetchone()[0]
        if day is None:
            return envelope(ok=False, reason="no_pool", as_of=now)
        limit = min(max(int(args.get("limit") or 50), 1), 500)
        rows = self.conn.execute(
            "SELECT trade_date, market, code, rank, score, factors, box_days, box_high_raw,"
            " box_low_raw, close_raw, box_vol_mean, pos_in_box FROM pool"
            " WHERE trade_date = ? ORDER BY rank LIMIT ?", (day, limit)).fetchall()
        out = [{"trade_date": r[0], "market": r[1], "code": r[2], "rank": r[3], "score": r[4],
                "factors": json.loads(r[5]), "box_days": r[6], "box_high_raw": r[7],
                "box_low_raw": r[8], "close_raw": r[9], "box_vol_mean": r[10], "pos_in_box": r[11]}
               for r in rows]
        return envelope(data={"trade_date": day, "rows": out}, as_of=now)

    def _tool_get_signals(self, args, now):
        date = args.get("date")
        where, params = "", []
        if date:
            where, params = " WHERE trade_date = ?", [int(date)]
        limit = min(max(int(args.get("limit") or 50), 1), 500)
        rows = self.conn.execute(
            "SELECT id, ts, trade_date, market, code, kind, level, payload FROM signals"
            f"{where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        out = [{"id": r[0], "ts": r[1], "trade_date": r[2], "market": r[3], "code": r[4],
                "kind": r[5], "level": r[6], "payload": json.loads(r[7])} for r in rows]
        return envelope(data={"rows": out}, as_of=now)

    def _tool_get_sector_rotation(self, args, now):
        snap = self._snap.get("sector")
        if snap["as_of"] is None:
            return envelope(ok=False, reason="no_snapshot", as_of=now)
        return envelope(data={"rows": snap["rows"]}, as_of=snap.get("as_of") or now)

    def _tool_get_market_breadth(self, args, now):
        snap = self._snap.get("market")
        rows = snap["rows"]
        if snap["as_of"] is None or not rows:
            return envelope(ok=False, reason="no_snapshot", as_of=now)
        up = sum(1 for r in rows if r.get("price") is not None and r.get("prev_close")
                 and r["price"] > r["prev_close"])
        down = sum(1 for r in rows if r.get("price") is not None and r.get("prev_close")
                   and r["price"] < r["prev_close"])
        flat = sum(1 for r in rows if r.get("price") is not None and r.get("prev_close")
                   and r["price"] == r["prev_close"])
        limit_up = sum(1 for r in rows if r.get("up") and r.get("price") is not None
                       and r["price"] >= r["up"])
        limit_down = sum(1 for r in rows if r.get("down") and r.get("price") is not None
                         and r["price"] <= r["down"])
        amount = sum(r["amount"] for r in rows if r.get("amount"))
        data = {"up": up, "down": down, "flat": flat, "limit_up": limit_up,
                "limit_down": limit_down, "total_amount": amount, "n": len(rows)}
        return envelope(data=data, as_of=snap.get("as_of") or now)

    def _tool_get_corporate_actions(self, args, now):
        if not self._need(args, "code"):
            return envelope(ok=False, reason="bad_args", as_of=now)
        rows = self.conn.execute(
            "SELECT ex_date, source, cash_per_share, bonus_ratio, tran_ratio"
            " FROM corporate_actions WHERE code = ? ORDER BY ex_date", (str(args["code"]),)).fetchall()
        out = [{"ex_date": r[0], "source": r[1], "cash_per_share": r[2],
                "bonus_ratio": r[3], "tran_ratio": r[4]} for r in rows]
        return envelope(data=out, as_of=now)

    def _tool_get_chip(self, args, now):
        if not self._need(args, "code"):
            return envelope(ok=False, reason="bad_args", as_of=now)
        code = str(args["code"])
        return self._cached("get_chip", args, now,
                            lambda: self._westock_call("chip", lambda: self._cli_instance().chip(code)))

    def _tool_get_notices(self, args, now):
        if not self._need(args, "code"):
            return envelope(ok=False, reason="bad_args", as_of=now)
        code = str(args["code"])
        n = min(max(int(args.get("n") or 10), 1), 50)
        call_args = dict(args, n=n)
        return self._cached("get_notices", call_args, now,
                            lambda: self._westock_call("notice", lambda: self._cli_instance().notice(code, n)))

    def _tool_get_fund_flow(self, args, now):
        if not self._need(args, "code"):
            return envelope(ok=False, reason="bad_args", as_of=now)
        code = str(args["code"])
        return self._cached("get_fund_flow", args, now,
                            lambda: self._westock_call("fund_flow", lambda: self._cli_instance().fund_flow(code)))

    def _tool_get_sector_history(self, args, now):
        if not self._need(args, "code"):
            return envelope(ok=False, reason="bad_args", as_of=now)
        code = str(args["code"])
        days = min(max(int(args.get("days") or 60), 1), 2000)

        def compute() -> dict:
            return self._sector_history(code, days)

        return self._cached("get_sector_history", dict(args, days=days), now, compute)

    def _sector_history(self, code: str, days: int) -> dict:
        """主源 westock CLI `kline <pt_code> --period day --limit <days>`（B5，豁免面内）；
        CLI 失败/超时 → 回落本地等权合成（`sector_member` ⋈ `daily_bars_adj`），信封标 method。"""
        now = self._now()
        cli = self._cli_instance()
        self.westock_limiter.acquire()
        pt = code if code.startswith("pt") else None
        if pt is None:
            try:
                hits = cli.search(code, "sector")
                pt = next((h.get("code") for h in hits if str(h.get("code", "")).startswith("pt")), None)
            except (UpstreamError, UpstreamTimeout):
                pt = None
        if pt is not None:
            try:
                self.westock_limiter.acquire()
                bars = cli.kline(pt, period="day", limit=days)
                return envelope(data={"code": pt, "method": "westock_cli", "bars": bars[-days:]}, as_of=now)
            except (UpstreamError, UpstreamTimeout):
                pass                                      # 回落本地
        members = [r[0] for r in self.conn.execute(
            "SELECT code FROM sector_member WHERE sector = ?", (code,))]
        if not members:
            return envelope(ok=False, reason="sector_unavailable", as_of=now)
        ph = ",".join("?" * len(members))
        closes: dict[int, list[float]] = {}
        for r in self.conn.execute(
                f"SELECT date, close FROM daily_bars_adj WHERE code IN ({ph}) ORDER BY date", members):
            closes.setdefault(r[0], []).append(r[1])
        bars = [{"date": d, "close": sum(v) / len(v)} for d, v in sorted(closes.items())][-days:]
        if not bars:
            return envelope(ok=False, reason="sector_unavailable", as_of=now)
        return envelope(data={"code": code, "method": "local_equal_weight",
                              "n_members": len(members), "bars": bars}, as_of=now)

    def _tool_get_index_snapshot(self, args, now):
        from jobs.snapshot import POOL_INDICES       # 与快照循环同一份指数名单（§6.1.2.2）
        rows, missing = [], []
        for code in POOL_INDICES:
            row = self._snap.row_for("pool", code)
            (rows.append(row) if row is not None else missing.append(code))
        if not rows:
            return envelope(ok=False, reason="no_snapshot", as_of=now)
        return envelope(data={"rows": rows, "missing": missing},
                        as_of=self._snap.get("pool").get("as_of") or now, adjust="raw")

    def _tool_get_event_context(self, args, now):
        event_id = args.get("event_id")
        if event_id is None:
            return envelope(ok=False, reason="bad_args", as_of=now)
        row = self.conn.execute(
            "SELECT id, ts, trade_date, market, kind, scope, target, payload, salience"
            " FROM event_log WHERE id = ?", (int(event_id),)).fetchone()
        if row is None:
            return envelope(ok=False, reason="no_event", as_of=now)
        related = self.conn.execute(
            "SELECT id, ts, trade_date, market, kind, scope, target, payload, salience"
            " FROM event_log WHERE trade_date = ? AND market = ? AND scope = ?"
            " AND target IS ? AND id <> ? ORDER BY ts LIMIT 20",
            (row[2], row[3], row[5], row[6], row[0])).fetchall()

        def to_dict(r):
            return {"id": r[0], "ts": r[1], "trade_date": r[2], "market": r[3], "kind": r[4],
                    "scope": r[5], "target": r[6], "payload": json.loads(r[7]), "salience": r[8]}

        return envelope(data={"event": to_dict(row), "related": [to_dict(r) for r in related]}, as_of=now)

    def _tool_get_own_history(self, args, now):
        day = int(args.get("date") or self.clock.today())
        prefix = f"{day // 10000:04d}-{day // 100 % 100:02d}-{day % 100:02d}"
        turns = self.conn.execute(
            "SELECT id, ts, wake_kind, decision, why, level, push_seq, schedule_next"
            " FROM agent_turn WHERE substr(ts, 1, 10) = ? ORDER BY ts", (prefix,)).fetchall()
        schedules = self.conn.execute(
            "SELECT id, created_at, due_at, target, note, status FROM agent_schedule"
            " WHERE substr(created_at, 1, 10) = ? OR status = 'pending' ORDER BY due_at",
            (prefix,)).fetchall()
        data = {
            "date": prefix,
            "turns": [{"id": r[0], "ts": r[1], "wake_kind": r[2], "decision": r[3], "why": r[4],
                       "level": r[5], "push_seq": r[6], "schedule_next": r[7]} for r in turns],
            "schedules": [{"id": r[0], "created_at": r[1], "due_at": r[2], "target": r[3],
                           "note": r[4], "status": r[5]} for r in schedules],
        }
        return envelope(data=data, as_of=now)

    def _tool_get_watch_quote(self, args, now):
        codes = [r[0] for r in self.conn.execute("SELECT code FROM watch_stock ORDER BY code")]
        if not codes:
            return envelope(data={"rows": [], "missing": []}, as_of=now, adjust="raw")
        rows, missing = [], []
        for code in codes:
            row = self._snap.row_for("pool", code) or self._snap.row_for("market", code)
            (rows.append(row) if row is not None else missing.append(code))
        as_of = self._snap.get("pool").get("as_of") or self._snap.get("market").get("as_of") or now
        return envelope(data={"rows": rows, "missing": missing}, as_of=as_of, adjust="raw")

    def _tool_compare_pattern(self, args, now):
        if not self._need(args, "code") or not self._need(args, "ref_start"):
            return envelope(ok=False, reason="bad_args", as_of=now)
        from core.strategy.compare import compare_pattern      # Task 4 已接通

        data, reason = compare_pattern(self.conn, self.clock, str(args["code"]),
                                       str(args["ref_start"]), args.get("ref_end"), cfg=self.cfg)
        if reason is not None:
            return envelope(ok=False, reason=reason, as_of=now)
        # core/strategy/compare.py 返回纯 data（不套信封），adjust 由它算好带回来
        return envelope(data=data, as_of=now, adjust=data.get("adjust", "adj"))
