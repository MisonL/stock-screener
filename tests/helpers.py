"""测试公共件：常量与纯 Python 小工具。

这是普通模块，不是 fixture —— pytest 只把 conftest 的 fixture 注入测试命名空间，
常量/类必须由测试文件显式 `from tests.helpers import ...`。
随任务推进向本文件追加 helper。
"""
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from core.clock import traded_minutes

SH = ZoneInfo("Asia/Shanghai")

SECRET = b"test-secret-32-bytes-deterministic!!"
# 与 config/notify.yaml 的 public_host 同域（同源校验测试要拿它拼 Origin）。
PUBLIC_HOST = "stock.example.com"


class FakeMono:
    """可推进的假单调时钟（纯 Python，无外部依赖）。"""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def advance(self, s):
        self.t += s


def iso(dt):
    return dt.isoformat()


def read_source(path):
    return Path(path).read_text(encoding="utf-8")


def set_beat(conn, service, ago_s=0, detail=None):
    """写 service_heartbeat 一行（UPSERT，原地更新不累积）。detail 走 json.dumps。"""
    ts = (datetime.now(SH) - timedelta(seconds=ago_s)).isoformat()
    conn.execute(
        "INSERT INTO service_heartbeat(service, last_beat_at, detail) VALUES (?, ?, ?) "
        "ON CONFLICT(service) DO UPDATE SET last_beat_at = excluded.last_beat_at, detail = excluded.detail",
        (service, ts, None if detail is None else json.dumps(detail)),
    )
    conn.commit()


def minutes(hm: str) -> int:
    """`"09:45"` → 当日已开盘交易分钟数（core.clock.traded_minutes）。"""
    return traded_minutes(time.fromisoformat(hm))


def make_http(**kw):
    """带零延迟限速器的 Http，测试不真的 sleep；transport 可注入 MockTransport。"""
    from core.http import Http, RateLimiter

    return Http({"tencent": RateLimiter(0.0), "sina": RateLimiter(0.0)}, **kw)


class BoomTransport(httpx.BaseTransport):
    """抛连接错误的 transport，供退避/异常测试用。"""

    def handle_request(self, request):
        raise httpx.ConnectError("boom")


boom_transport = BoomTransport()


# ---- Task 10：日线种子（普通函数，不是 fixture） ----

from datetime import date as _date, timedelta as _td


def _trading_days(start: int, n: int) -> list[int]:
    """从 start 起按「跳过周六周日」写 n 个交易日（测试不查节假日日历，够用）。"""
    out, d = [], _date(start // 10000, start // 100 % 100, start % 100)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(int(d.strftime("%Y%m%d")))
        d += _td(days=1)
    return out


def _trading_days_rev(last: int, n: int) -> list[int]:
    """从 last 往前取 n 个交易日，返回升序。"""
    out, d = [], _date(last // 10000, last // 100 % 100, last % 100)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(int(d.strftime("%Y%m%d")))
        d -= _td(days=1)
    return sorted(out)


def seed_daily_bars(conn, code, n, *, start_date=20250102, last_date=None, close=10.0, last_close=None):
    """写 n 根连续 raw bar。last_date 给定时第 n 根落在该日；last_close 只改最后一根收盘。"""
    days = _trading_days_rev(last_date, n) if last_date is not None else _trading_days(start_date, n)
    for i, d in enumerate(days):
        px = last_close if (last_close is not None and i == len(days) - 1) else close
        conn.execute(
            "INSERT INTO daily_bars(code,date,open,high,low,close,volume,amount,source) "
            "VALUES (?,?,?,?,?,?,?,?,'backfill')",
            (code, d, px, px, px, px, 100000, 1_000_000),
        )
    conn.commit()
    return days


def append_daily_bar(conn, code, *, date, close=10.0):
    conn.execute(
        "INSERT INTO daily_bars(code,date,open,high,low,close,volume,amount,source) "
        "VALUES (?,?,?,?,?,?,?,?,'backfill')",
        (code, date, close, close, close, close, 100000, 1_000_000),
    )
    conn.commit()


# ---- Task 12：westock CLI 的假体（除权 / 停牌） ----


class FakeWestockCli:
    """`WestockCli` 的假体：dividend / batch_dividend / suspension 全部离线返回预置数据。

    `suspensions` 是 `{code: (start, end)}`，`suspension()` 忽略查询区间原样返回
    （真 CLI 是区间查询，这里只需要「调一次拿回一批」这个形状）。
    """

    def __init__(self, dividends=None, suspensions=None):
        self.dividends = dict(dividends or {})        # code -> list[CorpAction]
        self.suspensions = dict(suspensions or {})    # code -> (start, end)
        self.calls = []

    def dividend(self, code, years=10):
        self.calls.append(("dividend", code, years))
        return list(self.dividends.get(code, []))

    def batch_dividend(self, codes, years=10, chunk=300, max_requests=None):
        # `max_requests` 是真实客户端的单轮配额（上游限频），假体只需要接受它
        self.calls.append(("batch_dividend", tuple(codes), years))
        return {code: list(self.dividends.get(code, [])) for code in codes}

    def suspension(self, start, end):
        self.calls.append(("suspension", start, end))
        return [(code, begin, finish) for code, (begin, finish) in self.suspensions.items()]


def seed_fetch_log(conn, code, n, status, purpose="backfill"):
    from datetime import datetime

    conn.execute(
        "INSERT INTO fetch_log(code, purpose, n, status, ts) VALUES (?,?,?,?,?) "
        "ON CONFLICT(code, purpose) DO UPDATE SET n=excluded.n, status=excluded.status, ts=excluded.ts",
        (code, purpose, n, status, datetime.now(SH).isoformat()),
    )
    conn.commit()


# ---- Task 18：观察池种子（普通函数，不是 fixture） ----

AS_OF = 20261030          # Task 18 的基准交易日（与普查基线同日）


def seed_index_bars(conn, days, close=4000.0):
    """写沪深300 的日线（交易日历的唯一来源，`TradingCalendar` 只认它）。"""
    for d in days:
        conn.execute(
            "INSERT OR REPLACE INTO daily_bars(code,date,open,high,low,close,volume,amount,source) "
            "VALUES ('sh000300',?,?,?,?,?,?,?,'backfill')",
            (d, close, close + 5.0, close - 5.0, close, 1_000_000, close * 1_000_000),
        )
    conn.commit()


def seed_pool_stock(conn, code, *, n=300, box_n=60, last_date=AS_OF, high=100.0,
                    box_price=61.0, box_half=0.3, decline_vol=3.0e6, box_vol=1.0e6,
                    adj_factor=1.0, name=None, market=None):
    """写一只「跌深 → 横盘」形态的票，返回 `(days, closes)`。

    形态：前 `n − box_n` 根从 `high` 线性跌到 `box_price`（回撤 ≈ 39%），后 `box_n` 根
    平在 `box_price`。量：下跌段 `decline_vol`、横盘段 `box_vol`（b4 ≈ 0.42，过 0.75 硬门槛）；
    `amount = 收盘 × 量`，横盘段 ≈ 6.1e7 > `min_amount_20d` 5e7。

    `adj_factor` 给定时 `daily_bars_adj` 整体乘该因子（`vol_factor=1.0`）——除权桥接用。
    """
    from core.market import classify_market

    days = _trading_days_rev(last_date, n)
    n_decl = n - box_n
    closes = [high - (high - box_price) * i / (n_decl - 1) for i in range(n_decl)]
    closes += [box_price] * box_n
    market = market or classify_market(code) or "cn_main"
    for i, d in enumerate(days):
        c = closes[i]
        hi, lo = round(c + box_half, 4), round(c - box_half, 4)
        vol = decline_vol if i < n_decl else box_vol
        conn.execute(
            "INSERT OR REPLACE INTO daily_bars(code,date,open,high,low,close,volume,amount,source) "
            "VALUES (?,?,?,?,?,?,?,?,'backfill')",
            (code, d, c, hi, lo, c, vol, round(c * vol, 4)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO daily_bars_adj"
            "(code,date,open,high,low,close,volume,factor,vol_factor) VALUES (?,?,?,?,?,?,?,?,1.0)",
            (code, d, c * adj_factor, hi * adj_factor, lo * adj_factor,
             c * adj_factor, vol, adj_factor),
        )
    conn.execute(
        "INSERT OR REPLACE INTO stock_meta(code,name,market,industry,list_date,updated_at) "
        "VALUES (?,?,?,NULL,20000101,'2026-11-06T08:00:00+08:00')",
        (code, name or code, market),
    )
    conn.commit()
    return days, closes


def set_bar_volume(conn, code, date, volume):
    """改一根 bar 的量（raw 与 adj 同步，`vol_factor=1` 的种子上两者相等）。"""
    conn.execute("UPDATE daily_bars SET volume = ?, amount = close * ? WHERE code = ? AND date = ?",
                 (volume, volume, code, date))
    conn.execute("UPDATE daily_bars_adj SET volume = ? WHERE code = ? AND date = ?",
                 (volume, code, date))
    conn.commit()


# ---------------------------------------------------------------- Task 23 推送层

class FakeNtfy:
    """`NtfyClient` 的替身：记录发出去的消息，可切换成「每次都失败」。"""

    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    def publish(self, m):
        if self.fail:
            raise RuntimeError("ntfy 不通")
        self.published.append(m)
        return f"fake-{len(self.published)}"


def seed_sig_context(conn, code="sh600519", trade_date=20261030, price=32.90, prev_close=31.87,
                     kind="breakout", score=86.0, bvm=1.82, industry="白酒", name="贵州茅台",
                     a_group_discounted=0, extended=0, panic=0, sealed=0, seq=1):
    """造出让 `load_sig` 命中六处来源的最小数据，返回 `event_id`。

    六处：stock_meta / stock_status_history / pool(T−1) / snapshot_cache('pool') /
    event_log / signals。
    """
    import json as _json

    # 池子在 T−1（盘中只有昨天那份）；这里按「前一自然日」直接落库，不依赖交易日历
    prev_trade_date = 20261029
    for d in (20261028, 20261029, 20261030):
        conn.execute("INSERT OR IGNORE INTO daily_bars VALUES ('sh000300', ?, 3000,3000,3000,3000,0,0,'x')", (d,))
    conn.execute("INSERT OR REPLACE INTO stock_meta(code,name,market,industry,list_date,updated_at)"
                 " VALUES (?,?,?,?,?,datetime('now'))", (code, name, "cn_main", industry, 20010827))
    conn.execute("INSERT OR REPLACE INTO stock_status_history VALUES (?,?,?,?)",
                 (code, 19900101, None, "normal"))
    conn.execute(
        "INSERT OR REPLACE INTO pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (prev_trade_date, "cn_main", code, 1, score, _json.dumps({"b4": 0.41}), 23,
         32.74, 30.21, 32.74, 30.21, 31.87, 1_000_000.0, 0.6, a_group_discounted, 0))
    # snapshot_cache 每个 scope 只有一行：多次 seed 必须**合并** rows，否则后一只票会顶掉前一只
    cached = conn.execute("SELECT payload FROM snapshot_cache WHERE scope='pool'").fetchone()
    rows = _json.loads(cached[0])["rows"] if cached else []
    rows = [r for r in rows if r.get("code") != code]
    rows.append({"code": code, "price": price, "volume": 1000, "prev_close": prev_close,
                 "up": None, "down": None})
    conn.execute(
        "INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES ('pool', ?, ?)",
        ("2026-10-30T10:00:00+08:00",
         _json.dumps({"as_of": "2026-10-30T10:00:00+08:00", "rows": rows})))
    payload = {"score": score, "box_vol_multiple": bvm, "limit_up": None,
               "extended": bool(extended), "panic": bool(panic), "sealed": bool(sealed),
               "watch": False, "a_group_discounted": a_group_discounted}
    cur = conn.execute(
        "INSERT INTO event_log(ts, trade_date, market, kind, scope, target, payload, salience,"
        " escalated, agent_status, dedupe_key) VALUES (?,?,?,?,?,?,?,?,0,'new',?)",
        ("2026-10-30T10:00:00+08:00", trade_date, "cn_main", kind, "stock", code,
         _json.dumps(payload), 0.40, f"{kind}:{code}:{trade_date}:{seq}"))
    event_id = cur.lastrowid
    conn.execute(
        "INSERT INTO signals(ts, trade_date, market, code, kind, level, event_id, seq, payload, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        ("2026-10-30T10:00:00+08:00", trade_date, "cn_main", code, kind, "P0", event_id, seq, "{}"))
    conn.commit()
    return event_id
