"""Task 3：只读工具集（23 工具 / 3 套 schema / 统一信封 / TTL 缓存 / needs_confirm）。

全部用假 mcp、假 westock CLI、真 SQLite（conftest 的 conn），不碰网络。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.config import load_config
from core.http import RateLimiter, UpstreamError, UpstreamTimeout
from core.strategy.pool import build_pool
from tests.helpers import AS_OF, _trading_days_rev, seed_index_bars, seed_pool_stock

from llm.tools import MCP_TOOL_SCHEMAS, ToolRegistry, envelope

CFG = load_config(Path("config"))
NOW = datetime(2026, 11, 6, 10, 0, 0)
POOL_NOW = datetime(2026, 10, 30, 9, 40, 0)

SNAP_KEYS = {"ok", "data", "as_of", "reason", "adjust"}


class MutableClock:
    """测试用可拨时钟：`now()` 不自己走，测试手动改 `t`。"""

    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def today(self):
        return int(self.t.strftime("%Y%m%d"))


class FakeCli:
    """计数假 westock CLI：记录每次调用，前 `fail_times` 次抛注入的异常。"""

    def __init__(self, exc=None, fail_times=1):
        self.calls = []
        self.exc = exc
        self.fail_times = fail_times

    def _maybe_fail(self, tag, detail):
        self.calls.append((tag, detail))
        if self.exc is not None and self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc

    def chip(self, code):
        self._maybe_fail("chip", code)
        return [{"code": code, "profit_ratio": 0.42, "avg_cost": 31.5}]

    def notice(self, code, n=10):
        self._maybe_fail("notice", (code, n))
        return [{"title": "关于回购的公告", "ts": "2026-11-05"}] * min(n, 1)

    def fund_flow(self, code):
        self._maybe_fail("fund_flow", code)
        return [{"code": code, "main_net": 1.2e6}]

    def kline(self, code, period="day", limit=None):
        self._maybe_fail("kline", (code, period, limit))
        return [{"date": "2026-11-0%d" % (i + 1), "close": 1000.0 + i} for i in range(limit or 60)]

    def search(self, keyword, stype="sector"):
        self._maybe_fail("search", (keyword, stype))
        return [{"code": "pt01801081", "name": keyword}]


class FakeMcp:
    """假 WestockMcp：读方法记账返回信封；写方法一旦被碰就地引爆（写必须走确认卡）。"""

    def __init__(self):
        self.calls = []

    def read_watchlist(self, group=None):
        self.calls.append(("read_watchlist", group))
        return {"ok": True, "data": [{"code": "sh600519"}]}

    def read_alerts(self):
        self.calls.append(("read_alerts",))
        return {"ok": True, "data": [{"code": "sh600519", "kind": "up"}]}

    def add_watch(self, code, note=None):
        raise AssertionError("写工具不经确认卡不得触达 mcp")

    def remove_watch(self, code):
        raise AssertionError("写工具不经确认卡不得触达 mcp")

    def set_alert(self, code, kind, threshold):
        raise AssertionError("写工具不经确认卡不得触达 mcp")

    def cancel_alert(self, code, kind):
        raise AssertionError("写工具不经确认卡不得触达 mcp")


def make_reg(conn, mcp=None, clock=None):
    clock = clock or MutableClock(NOW)
    reg = ToolRegistry(conn, CFG, clock, mcp=mcp)
    reg.westock_limiter = RateLimiter(0.0)   # 测试不真睡 300ms
    return reg


def seed_snap(conn, scope, rows, as_of="2026-11-06T10:00:00+08:00"):
    conn.execute(
        "INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES (?,?,?)",
        (scope, as_of, json.dumps({"as_of": as_of, "source": "test", "rows": rows})),
    )
    conn.commit()


def quote_row(code, price, prev=10.0, **extra):
    row = {"code": code, "name": code, "price": price, "prev_close": prev, "open": price,
           "high": price, "low": price, "volume": 1e6, "amount": price * 1e6,
           "volume_ratio": 1.1, "up": prev * 1.1, "down": prev * 0.9, "ts": "2026-11-06T09:59:58"}
    row.update(extra)
    return row


# ---------------------------------------------------------------- schema 数与归属


def test_chat_schema_has_23_strict_tools(conn):
    reg = make_reg(conn)
    tools = reg.schema("chat")
    assert len(tools) == 23
    names = [t["function"]["name"] for t in tools]
    assert len(names) == len(set(names))
    for t in tools:
        assert t["type"] == "function"
        assert t["function"]["strict"] is True
        assert t["function"]["parameters"]["additionalProperties"] is False


def test_event_and_heartbeat_schema_are_19_with_mcp_reads(conn):
    reg = make_reg(conn)
    event = reg.schema("event")
    heartbeat = reg.schema("heartbeat")
    assert len(event) == 19 and len(heartbeat) == 19
    assert event == heartbeat
    names = {t["function"]["name"] for t in event}
    assert "mcp_read_watchlist" in names and "mcp_read_alerts" in names
    assert "mcp_add_watch" not in names and "mcp_set_alert" not in names
    # get_event_context / get_own_history 仍在 event schema（D22 明确不摘）
    assert "get_event_context" in names and "get_own_history" in names
    # 13 个基线 + 4 个盯盘工具全部在
    for n in ("get_quote", "get_kline", "get_pattern", "get_pool", "get_signals",
              "get_sector_rotation", "get_sector_history", "get_market_breadth",
              "get_corporate_actions", "get_chip", "get_notices", "get_fund_flow",
              "compare_pattern", "get_index_snapshot", "get_watch_quote"):
        assert n in names


def test_alarm_auction_open_close_map_to_heartbeat(conn):
    reg = make_reg(conn)
    hb = reg.schema("heartbeat")
    for kind in ("alarm", "auction", "open", "close"):
        assert reg.schema(kind) == hb
    assert len(reg.schema("chat")) == 23          # chat 不映射到 heartbeat


def test_mcp_tool_schemas_writes_require_confirm(conn):
    assert len(MCP_TOOL_SCHEMAS) == 6
    for t in MCP_TOOL_SCHEMAS:
        name = t["function"]["name"]
        if name in ("mcp_add_watch", "mcp_remove_watch", "mcp_set_alert", "mcp_cancel_alert"):
            assert "确认" in t["function"]["description"]


def test_unknown_kind_rejected(conn):
    reg = make_reg(conn)
    with pytest.raises(ValueError):
        reg.schema("lunch")
    with pytest.raises(ValueError):
        reg.invoke("get_quote", {"code": "sh600519"}, kind="brunch")


# ---------------------------------------------------------------- needs_confirm / mcp 委派


def test_write_tool_on_event_turn_needs_confirm(conn):
    """写工具在事件回合被模型直接调 → needs_confirm，且绝不触达 mcp（mcp=None 也不依赖）。"""
    reg = make_reg(conn, mcp=None)
    env = reg.invoke("mcp_set_alert", {"code": "sh600519", "kind": "up", "threshold": 33.0}, kind="event")
    assert env["ok"] is False and env["reason"] == "needs_confirm"
    assert set(env.keys()) == SNAP_KEYS


def test_write_tool_on_chat_turn_still_needs_confirm(conn):
    """chat 回合也不放行；mcp 在场也拦在委派之前（假 mcp 的写方法一碰就炸）。"""
    mcp = FakeMcp()
    reg = make_reg(conn, mcp=mcp)
    env = reg.invoke("mcp_add_watch", {"code": "sh600519", "note": "x"}, kind="chat")
    assert env["ok"] is False and env["reason"] == "needs_confirm"
    assert mcp.calls == []


def test_read_mcp_unavailable_without_mcp(conn):
    reg = make_reg(conn, mcp=None)
    env = reg.invoke("mcp_read_watchlist", {"group": "SYS"}, kind="heartbeat")
    assert env["ok"] is False and env["reason"] == "mcp_unavailable"


def test_read_mcp_delegates_to_injected_mcp(conn):
    mcp = FakeMcp()
    reg = make_reg(conn, mcp=mcp)
    env = reg.invoke("mcp_read_watchlist", {"group": "SYS"}, kind="event")
    assert mcp.calls == [("read_watchlist", "SYS")]
    assert env["ok"] is True and env["data"] == [{"code": "sh600519"}]
    assert set(env.keys()) == SNAP_KEYS
    env2 = reg.invoke("mcp_read_alerts", {}, kind="heartbeat")
    assert env2["ok"] is True


def test_mcp_read_error_still_envelope(conn):
    class Boom:
        def read_alerts(self):
            raise RuntimeError("net down")

    reg = make_reg(conn, mcp=Boom())
    env = reg.invoke("mcp_read_alerts", {}, kind="chat")
    assert env["ok"] is False and env["reason"].startswith("mcp_error")


# ---------------------------------------------------------------- 信封形状 / 本地工具


def test_every_envelope_has_fixed_shape(conn):
    seed_snap(conn, "pool", [quote_row("sh600519", 32.9)])
    seed_snap(conn, "market", [quote_row("sh600001", 9.9)])
    reg = make_reg(conn)
    for name, args in (("get_quote", {"code": "sh600519"}),
                       ("get_pool", {}),
                       ("get_signals", {}),
                       ("get_market_breadth", {}),
                       ("get_sector_rotation", {}),
                       ("get_index_snapshot", {}),
                       ("get_watch_quote", {}),
                       ("get_corporate_actions", {"code": "sh600519"})):
        env = reg.invoke(name, args, kind="chat")
        assert set(env.keys()) == SNAP_KEYS, name
        assert isinstance(env["ok"], bool) and env["as_of"], name


def test_unknown_tool_never_raises(conn):
    reg = make_reg(conn)
    env = reg.invoke("nope", {}, kind="chat")
    assert env["ok"] is False


def test_get_kline_defaults_raw(conn):
    from tests.helpers import seed_daily_bars
    days = seed_daily_bars(conn, "sh600519", 5, close=10.0)
    for d in days:
        conn.execute("INSERT OR REPLACE INTO daily_bars_adj(code,date,open,high,low,close,volume,factor,vol_factor)"
                     " VALUES ('sh600519',?,?,?,?,?,?,?,1.0)", (d, 30, 30, 30, 30, 1e6, 3.0))
    conn.commit()
    reg = make_reg(conn)
    env = reg.invoke("get_kline", {"code": "sh600519"}, kind="chat")
    assert env["adjust"] == "raw" and env["data"]["adjust"] == "raw"
    assert env["data"]["bars"][-1]["close"] == 10.0          # raw 表的价，不是 adj 的 30
    env2 = reg.invoke("get_kline", {"code": "sh600519", "days": 5, "adjust": "adj"}, kind="chat")
    assert env2["adjust"] == "adj" and env2["data"]["bars"][-1]["close"] == 30.0


def test_get_quote_from_snapshot(conn):
    seed_snap(conn, "pool", [quote_row("sh600519", 32.9)], as_of="2026-11-06T09:59:58+08:00")
    reg = make_reg(conn)
    env = reg.invoke("get_quote", {"code": "sh600519"}, kind="event")
    assert env["ok"] is True and env["data"]["price"] == 32.9
    assert env["as_of"] == "2026-11-06T09:59:58+08:00"
    miss = reg.invoke("get_quote", {"code": "sz000001"}, kind="event")
    assert miss["ok"] is False


def test_get_market_breadth_counts(conn):
    seed_snap(conn, "market", [quote_row("a", 11.0, up=12.0, down=8.0), quote_row("b", 10.0),
                               quote_row("c", 11.0, up=11.0), quote_row("d", 9.0, down=9.0)])
    reg = make_reg(conn)
    env = reg.invoke("get_market_breadth", {}, kind="heartbeat")
    assert env["ok"] is True
    assert (env["data"]["up"], env["data"]["down"], env["data"]["flat"]) == (2, 1, 1)
    assert env["data"]["limit_up"] == 1 and env["data"]["limit_down"] == 1


def test_get_pool_and_signals(conn):
    conn.execute("INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
                 " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean,"
                 " pos_in_box) VALUES (20261106,'cn_main','sh600519',1,88.5,'{}',40,10,9,10,9,10,1e6,0.5)")
    conn.execute("INSERT INTO signals(ts, trade_date, market, code, kind, level, event_id, seq, payload)"
                 " VALUES ('2026-11-06T10:00:00+08:00',20261106,'cn_main','sh600519','breakout','P1',1,1,'{}')")
    conn.commit()
    reg = make_reg(conn)
    env = reg.invoke("get_pool", {}, kind="chat")
    assert env["ok"] is True and env["data"]["rows"][0]["code"] == "sh600519"
    env2 = reg.invoke("get_signals", {}, kind="event")
    assert env2["ok"] is True and env2["data"]["rows"][0]["kind"] == "breakout"


def test_get_own_history_and_event_context(conn):
    conn.execute("INSERT INTO event_log(ts, trade_date, market, kind, scope, target, payload,"
                 " salience, dedupe_key) VALUES"
                 " ('2026-11-06T09:58:00+08:00',20261106,'cn_main','breakout','stock','sh600519','{}',0.9,'k1'),"
                 " ('2026-11-06T09:59:00+08:00',20261106,'cn_main','breakout','stock','sh600519','{}',0.8,'k2')")
    conn.execute("INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback, market)"
                 " VALUES ('2026-11-06T09:59:30+08:00','event','d',1,'speak',0,'cn_main')")
    conn.commit()
    reg = make_reg(conn)
    env = reg.invoke("get_event_context", {"event_id": 1}, kind="event")
    assert env["ok"] is True and len(env["data"]["related"]) == 1
    env2 = reg.invoke("get_own_history", {}, kind="chat")
    assert env2["ok"] is True and env2["data"]["turns"][0]["decision"] == "speak"


def test_get_watch_quote(conn):
    seed_snap(conn, "pool", [quote_row("sh600519", 32.9)])
    conn.execute("INSERT INTO watch_stock(code, note, source, added_at) VALUES ('sh600519',NULL,'manual','x')")
    conn.commit()
    reg = make_reg(conn)
    env = reg.invoke("get_watch_quote", {}, kind="heartbeat")
    assert env["ok"] is True and env["data"]["rows"][0]["code"] == "sh600519"


# ---------------------------------------------------------------- get_pattern（单票现算，raw 箱体）


def test_pattern_box_high_equals_pool_box_high_raw(db):
    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    seed_pool_stock(db, "sh600519", adj_factor=3.0358)
    build_pool(db, CFG, "cn_main", AS_OF)
    pool_row = db.execute("SELECT box_high_raw, box_low_raw, score FROM pool WHERE code='sh600519'").fetchone()
    assert pool_row is not None

    reg = make_reg(db, clock=MutableClock(datetime(2026, 10, 30, 9, 40)))
    env = reg.invoke("get_pattern", {"code": "sh600519"}, kind="chat")
    assert env["ok"] is True, env
    assert env["adjust"] == "raw"
    assert env["data"]["box_high"] == pytest.approx(pool_row[0])
    assert env["data"]["box_low"] == pytest.approx(pool_row[1])
    assert env["data"]["score"] == pytest.approx(pool_row[2])
    assert env["data"]["in_pool"] is True


def test_pattern_rejected_stock_is_ok_false(db):
    """没数据的票返回 ok:false 信封，绝不抛。"""
    reg = make_reg(db)
    env = reg.invoke("get_pattern", {"code": "sz000001"}, kind="chat")
    assert env["ok"] is False and set(env.keys()) == SNAP_KEYS


# ---------------------------------------------------------------- westock CLI 三工具 + 缓存


def test_westock_timeout_returns_ok_false_not_raise(conn):
    cli = FakeCli(exc=UpstreamTimeout("t"), fail_times=99)
    reg = make_reg(conn)
    reg.cli = cli
    env = reg.invoke("get_chip", {"code": "sh600519"}, kind="event")
    assert env["ok"] is False and env["reason"] == "westock_timeout"
    env2 = reg.invoke("get_fund_flow", {"code": "sh600519"}, kind="event")
    assert env2["ok"] is False and env2["reason"] == "westock_timeout"


def test_westock_passthrough_success(conn):
    cli = FakeCli()
    reg = make_reg(conn)
    reg.cli = cli
    env = reg.invoke("get_chip", {"code": "sh600519"}, kind="event")
    assert env["ok"] is True and env["data"][0]["profit_ratio"] == 0.42
    env2 = reg.invoke("get_notices", {"code": "sh600519", "n": 5}, kind="event")
    assert env2["ok"] is True
    assert cli.calls == [("chip", "sh600519"), ("notice", ("sh600519", 5))]


def test_cache_hit_calls_cli_once_and_keeps_as_of(conn):
    """60 秒内两次 → CLI 只调 1 次，且第二次信封 as_of 保持原值（时钟已拨走仍不变）。"""
    cli = FakeCli()
    clock = MutableClock(NOW)
    reg = make_reg(conn, clock=clock)
    reg.cli = cli
    env1 = reg.invoke("get_sector_history", {"code": "pt01801081", "days": 60}, kind="chat")
    assert env1["ok"] is True and env1["data"]["method"] == "westock_cli"
    clock.t = clock.t + timedelta(seconds=30)                # TTL 60s 内
    env2 = reg.invoke("get_sector_history", {"code": "pt01801081", "days": 60}, kind="chat")
    assert len(cli.calls) == 1
    assert env2["as_of"] == env1["as_of"] != clock.now().isoformat()
    assert env2["data"] == env1["data"]


def test_cache_key_ignores_arg_order(conn):
    """key 是 `tuple(sorted(args.items()))`：同样参数换个顺序必须命中同一份缓存。"""
    cli = FakeCli()
    reg = make_reg(conn)
    reg.cli = cli
    a = reg.invoke("get_sector_history", {"code": "pt01801081", "days": 60}, kind="chat")
    b = reg.invoke("get_sector_history", {"days": 60, "code": "pt01801081"}, kind="chat")
    assert a["ok"] and b["ok"]
    assert len(cli.calls) == 1 and b["as_of"] == a["as_of"]


def test_cache_expires_after_ttl(conn):
    cli = FakeCli()
    clock = MutableClock(NOW)
    reg = make_reg(conn, clock=clock)
    reg.cli = cli
    first = reg.invoke("get_sector_history", {"code": "pt01801081", "days": 60}, kind="chat")
    clock.t = clock.t + timedelta(seconds=61)                # 超过 60s TTL
    env = reg.invoke("get_sector_history", {"code": "pt01801081", "days": 60}, kind="chat")
    assert len(cli.calls) == 2
    assert env["as_of"] == clock.t.isoformat() and env["as_of"] != first["as_of"]


def test_cache_only_stores_success(conn):
    """ok:false 不进缓存：恢复后立刻重调会再打 CLI（共 2 次），第三次才命中缓存。"""
    cli = FakeCli(exc=UpstreamTimeout("t"), fail_times=1)
    reg = make_reg(conn)
    reg.cli = cli
    bad = reg.invoke("get_chip", {"code": "sh600519"}, kind="chat")
    assert bad["ok"] is False
    good = reg.invoke("get_chip", {"code": "sh600519"}, kind="chat")
    assert good["ok"] is True
    again = reg.invoke("get_chip", {"code": "sh600519"}, kind="chat")
    assert again["as_of"] == good["as_of"]
    assert len(cli.calls) == 2                               # 失败 1 次 + 成功 1 次，第三次走缓存


def test_cache_lru_eviction(conn):
    cli = FakeCli()
    reg = make_reg(conn)
    reg.cli = cli
    first = "sh600000"
    reg.invoke("get_fund_flow", {"code": first}, kind="chat")          # 第 1 个（最旧）
    for i in range(255):
        reg.invoke("get_fund_flow", {"code": f"sh6{i + 1000:04d}"}, kind="chat")
    assert len(cli.calls) == 256
    reg.invoke("get_fund_flow", {"code": "sh699999"}, kind="chat")     # 第 257 个 → 挤出最旧
    reg.invoke("get_fund_flow", {"code": first}, kind="chat")          # 已被挤出 → 重打 CLI
    assert len([c for c in cli.calls if c[1] == first]) == 2


def test_ttl_values_match_plan(conn):
    from llm.tools import CACHE_TTL_S
    assert CACHE_TTL_S == {"get_chip": 600, "get_notices": 300,
                           "get_fund_flow": 60, "get_sector_history": 60}


def test_sector_history_cli_call_shape(conn):
    cli = FakeCli()
    reg = make_reg(conn)
    reg.cli = cli
    reg.invoke("get_sector_history", {"code": "pt01801081", "days": 30}, kind="chat")
    assert cli.calls[0] == ("kline", ("pt01801081", "day", 30))


def test_sector_history_local_fallback(conn):
    """CLI 挂 → 回落 sector_member ⋈ daily_bars_adj 等权合成，method 标 local_equal_weight。"""
    cli = FakeCli(exc=UpstreamError("boom"), fail_times=99)
    for code, closes in (("sh600001", [10.0, 11.0]), ("sh600002", [20.0, 25.0])):
        conn.execute("INSERT INTO sector_member(sector, code, updated_at) VALUES ('半导体',?, 'x')", (code,))
        for d, c in zip((20261105, 20261106), closes):
            conn.execute("INSERT OR REPLACE INTO daily_bars_adj(code,date,open,high,low,close,volume,factor,vol_factor)"
                         " VALUES (?,?,?,?,?,?,1e6,1.0,1.0)", (code, d, c, c, c, c))
    conn.commit()
    reg = make_reg(conn)
    reg.cli = cli
    env = reg.invoke("get_sector_history", {"code": "半导体", "days": 10}, kind="chat")
    assert env["ok"] is True, env
    assert env["data"]["method"] == "local_equal_weight"
    closes = [b["close"] for b in env["data"]["bars"]]
    assert closes == pytest.approx([15.0, 18.0])             # (10+20)/2, (11+25)/2


def test_returns_this_turn_per_tool(conn):
    seed_snap(conn, "pool", [quote_row("sh600519", 32.9)])
    reg = make_reg(conn)
    reg.invoke("get_quote", {"code": "sh600519"}, kind="chat")
    reg.invoke("get_quote", {"code": "sh600519"}, kind="chat")
    reg.invoke("get_pool", {}, kind="chat")
    rets = reg.returns_this_turn
    assert len(rets["get_quote"]) == 2 and len(rets["get_pool"]) == 1
    assert rets["get_quote"][0]["ok"] is True
    reg.begin_turn()
    assert reg.returns_this_turn == {}


# ---------------------------------------------------------------- envelope 单元


def test_envelope_shape_and_error_path():
    now = "2026-11-06T10:00:00+08:00"
    ok = envelope({"a": 1}, as_of=now, adjust="raw")
    assert set(ok.keys()) == SNAP_KEYS and ok["ok"] is True and ok["data"] == {"a": 1}
    bad = envelope(ok=False, reason="x", as_of=now)
    assert bad["ok"] is False and bad["reason"] == "x" and bad["data"] is None
    with pytest.raises(TypeError):
        envelope()                                     # as_of 必传


def test_compare_pattern_tool_wraps_single_envelope(conn):
    """工具必须只套一层信封：core 返回 (data, reason)，自己再返回 ok:false 会得到双层。"""
    reg = make_reg(conn)
    env = reg.invoke("compare_pattern",
                     {"code": "sh600519", "ref_start": "2026-08-01"}, kind="chat")
    assert set(env) >= {"ok", "data", "as_of", "reason", "adjust"}
    assert not (isinstance(env["data"], dict) and "ok" in env["data"])   # 内层不能再有 ok


def test_get_pattern_rejects_exright_day(db):
    """除权日必须与 build_pool 同口径拒绝：否则模型会拿到池外的「合格形态」。"""
    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    seed_pool_stock(db, "sh600519", adj_factor=3.0358)
    build_pool(db, CFG, "cn_main", AS_OF)          # 先把 stock_stats 填好，否则会先撞 bars 门槛
    db.execute("INSERT INTO corporate_actions(code, ex_date, source) VALUES ('sh600519', ?, 'westock_dividend')",
               (AS_OF,))
    db.commit()
    reg = make_reg(db, clock=MutableClock(datetime(2026, 10, 30, 9, 40)))
    env = reg.invoke("get_pattern", {"code": "sh600519"}, kind="chat")
    assert env["ok"] is False and env["reason"].startswith("reject_exright")


def test_get_kline_schema_does_not_expose_adjust(db):
    """adj 只在 compare_pattern 内部用：schema 里暴露它 = 让模型把复权价与实时价比较。"""
    import json as _json
    reg = make_reg(db)
    tools = [t for t in reg.schema("chat") if t["function"]["name"] == "get_kline"]
    schema = _json.dumps(tools, ensure_ascii=False)
    assert '"adj"' not in schema


def test_cache_key_tolerates_list_args(db):
    """args 里带数组不能让缓存 key 抛 unhashable。"""
    env = make_reg(db).invoke("get_chip", {"code": "sh600519", "groups": ["A", "B"]}, kind="chat")
    assert "unhashable" not in (env["reason"] or "")


def test_invoke_returns_envelope_for_bad_args_shape(db):
    """args 传成数组也必须回信封（契约：绝不抛给调用方）。"""
    env = make_reg(db).invoke("get_quote", ["not", "a", "dict"], kind="chat")
    assert env["ok"] is False and env["reason"].startswith("tool_error")


def test_schema_mutation_does_not_leak_into_next_call(db):
    reg = make_reg(db)
    first = reg.schema("chat")
    first[0]["function"]["name"] = "PWNED"
    assert reg.schema("chat")[0]["function"]["name"] != "PWNED"
