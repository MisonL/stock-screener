"""共享 fixture。

Task 1 版本只定义不依赖后续模块的 fixture（配置常量）。
凡是 import core.db / core.clock / core.http 的 fixture（db、make_http、client、
tmp_env、env、fake_env、det、disp 及各数据源 fixture）都在它们所依赖的模块建好之后
的那个 Task 的 Step 1 里追加到本文件 —— 提前 import 不存在的模块会让 pytest
在收集期 ModuleNotFoundError。
"""
from datetime import datetime as dt
from pathlib import Path

import pytest

from core.config import load_config
from core.db import connect, migrate
from core.models import Quote
from tests.helpers import FakeMono

# `api` fixture 的固定时钟：2026-10-30 10:00（盘中）
_API_T0 = __import__("datetime").datetime(2026, 10, 30, 10, 0,
                                          tzinfo=__import__("core.clock", fromlist=["SH"]).SH)


@pytest.fixture
def CFG():
    return load_config(Path("config"))


@pytest.fixture
def S(CFG):
    return CFG.strategy


@pytest.fixture
def MAIN(CFG):
    return CFG.market("cn_main")


@pytest.fixture
def P(CFG):
    return CFG.strategy.intraday


@pytest.fixture
def BOX(CFG):
    return CFG.strategy.box


@pytest.fixture
def CURVE(CFG):
    return CFG.strategy.intraday.curve


# Task 2（core.db 建成后）：db / tmp_db / conn 三个名字同一实现，测试里三种叫法都有，
# 不许各造各的（计划 Task 1 的规定）。

@pytest.fixture
def db(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


@pytest.fixture
def tmp_db(db):
    return db


@pytest.fixture
def conn(db):
    return db


@pytest.fixture
def q1():
    # 裸 Connection 没有 `.one`；需要单行结果时用 `q1(conn, "SELECT …")`。
    return lambda conn, sql, params=(): conn.execute(sql, params).fetchone()


# Task 4（core.clock）：只有 20260930 / 20261009 两天有 sh000300 的 bar。
# 定义一次，Task 4 之后全部复用。

@pytest.fixture
def db_with_index_days(db):
    for d in (20260930, 20261009):
        db.execute("INSERT INTO daily_bars VALUES ('sh000300', ?, 1, 1, 1, 1, 0, 0, 'tencent')", (d,))
    db.commit()
    return db


# Task 7（core.datasource.tencent）：数据源 fixture 全部走 MockTransport，单测不碰网。

def _tencent_source(body: bytes):
    import httpx

    from core.datasource.tencent import TencentSource
    from core.http import Http, RateLimiter

    def handler(request):
        return httpx.Response(200, content=body)

    return TencentSource(Http({"tencent": RateLimiter(0.0)}, transport=httpx.MockTransport(handler)))


@pytest.fixture
def tencent_with_fixture():
    """日线样本（真实响应，2026-10-26..28 的 sh600519）。

    响应按请求里的 `count` 切片（假上游）：`count=1` 时只回第一根，
    这样跨源比对的用例能稳定拿到同一根 bar 的字段顺序。
    """
    import json

    import httpx

    from core.datasource.tencent import TencentSource
    from core.http import Http, RateLimiter
    from tests.fixtures import load_fixture

    rows = load_fixture("tencent_kline_day.json")["data"]["sh600519"]["day"]

    def handler(request):
        count = int(request.url.params["param"].split(",")[-2])
        body = {"code": 0, "msg": "", "data": {"sh600519": {"day": rows[:count], "qt": {}}}}
        return httpx.Response(200, content=json.dumps(body).encode())

    return TencentSource(Http({"tencent": RateLimiter(0.0)}, transport=httpx.MockTransport(handler)))


@pytest.fixture
def tencent_with_snapshot():
    """快照样本：真实响应格式，`sz999999` 那行上游直接不返回。"""
    from tests.fixtures import load_fixture

    return _tencent_source(load_fixture("tencent_snapshot.txt").encode("gbk"))


@pytest.fixture
def tencent_empty():
    """`code==0` 但 bar 数组为空——异常，不是「没数据」。"""
    import json

    body = json.dumps({"code": 0, "msg": "", "data": {"sh600519": {"day": [], "qt": {}}}}).encode()
    return _tencent_source(body)


@pytest.fixture
def sina_fixture():
    """新浪样本：K 线 / 快照 / 两页 universe（第 2 页空数组）。

    限速器接 `FakeMono` —— `Http.calls[].ts_mono` 必须取自同一个 mono，
    否则分页间隔的断言恒为 0。
    """
    import json

    import httpx

    from core.datasource.sina import SinaSource
    from core.http import Http, RateLimiter
    from tests.fixtures import load_fixture

    kline = json.dumps(load_fixture("sina_kline.json")).encode()
    snapshot = load_fixture("sina_snapshot.txt").encode("gbk")
    page1 = json.dumps(load_fixture("sina_universe_page1.json"), ensure_ascii=False).encode()
    page2 = json.dumps(load_fixture("sina_universe_page2.json")).encode()

    def handler(request):
        url = str(request.url)
        if "CN_MarketDataService" in url:
            return httpx.Response(200, content=kline)
        if "getHQNodeData" in url:
            page = request.url.params.get("page")
            return httpx.Response(200, content=page1 if page == "1" else page2)
        return httpx.Response(200, content=snapshot)

    mono = FakeMono()
    http = Http({"sina": RateLimiter(0.3, mono=mono.now, sleep=mono.sleep)}, transport=httpx.MockTransport(handler))
    return SinaSource(http)


# Task 9（core.datasource.registry）：降级链的假源。

class _FakeSource:
    """可注入失败/延迟的假数据源；`name` 就是 registry 返回的源名。"""

    def __init__(self, name, mono=None, fail=None, delay=0.0):
        import httpx

        from core.models import Bar

        self.name = name
        self.calls = 0
        self._mono = mono
        self._fail = fail
        self._delay = delay
        self._Bar = Bar
        self._httpx = httpx

    def _maybe_fail(self):
        self.calls += 1
        if self._mono is not None and self._delay:
            self._mono.advance(self._delay)
        if self._fail == "timeout":
            from core.http import UpstreamTimeout

            raise UpstreamTimeout("slow")
        if self._fail == "blocked":
            from core.http import UpstreamBlocked

            raise UpstreamBlocked("403")
        if self._fail == "parse":
            from core.http import UpstreamError

            raise UpstreamError('{"code":0,"data":[]}')

    def fetch_kline_daily(self, code, count=1700, end=None, scope="backfill"):
        self._maybe_fail()
        return [self._Bar(code, 20261028, 1.0, 1.0, 1.0, 1.0, 100, 100, self.name)]

    def fetch_snapshot(self, codes, scope="pool"):
        self._maybe_fail()
        from core.models import Quote

        return {
            code: Quote(code, "X", 1.0, 1.0, 1.0, 1.0, 1.0, 100, 100, None, None, None, None, [])
            for code in codes
        }

    def fetch_universe(self, scope="backfill"):
        self._maybe_fail()
        return [("sh600519", "贵州茅台")]


@pytest.fixture
def fake_sources():
    from types import SimpleNamespace

    mono = FakeMono()
    return SimpleNamespace(
        mono=mono,
        boom=_FakeSource("tencent", mono, fail="parse"),
        blocked=_FakeSource("tencent", mono, fail="blocked"),
        slow=_FakeSource("tencent", mono, delay=4.0),
        ok=_FakeSource("sina", mono),
        other_ok=_FakeSource("tencent", mono),
        upstream_fail=_FakeSource("tencent", mono, fail="timeout"),
    )


# Task 11（jobs.backfill）：假上游 + 环境对象。

class FakeTencentTransport(__import__("httpx").BaseTransport):
    """同时应答腾讯日线、新浪 universe 的假上游；`failing` 里的代码一律回空数组。"""

    INDICES = ("sh000300", "sh000001", "sz399001", "sz399006", "sh000688")

    def __init__(self):
        import httpx

        self.failing = set()
        self.httpx = httpx
        self.universe = [("sh600519", "贵州茅台"), ("sz000001", "平安银行"), ("sz300750", "宁德时代")]

    def _days(self, n):
        from tests.helpers import _trading_days_rev

        return _trading_days_rev(20261030, n)

    def _bars(self, code, count):
        from core.models import Bar

        if count == 20:
            head = self._days(19)[:-1]                      # 18 根
            days = sorted(head + [20260812, 20261030])
            bars = [Bar(code, d, 10 + i * 0.1, 10 + i * 0.1, 10 + i * 0.1, 10 + i * 0.1, 1000, 10000, "tencent")
                    for i, d in enumerate(days[:-2])]
            bars.append(Bar(code, 20260812, 9.5, 9.5, 9.5, 9.5, 1000, 10000, "tencent"))   # 历史一字跌停
            bars.append(Bar(code, 20261030, 9.5, 9.5, 9.5, 9.5, 1000, 10000, "tencent"))    # 占位：close == 前收
            return bars
        return [
            Bar(code, d, 10 + i * 0.01, 10 + i * 0.01, 10 + i * 0.01, 10 + i * 0.01, 1000, 10000, "tencent")
            for i, d in enumerate(self._days(count))
        ]

    def _json(self, payload):
        import json

        return self.httpx.Response(200, content=json.dumps(payload, ensure_ascii=False).encode())

    def handle_request(self, request):
        import json

        url = str(request.url)
        if "getHQNodeData" in url:
            page = request.url.params.get("page")
            if page == "1":
                rows = [{"symbol": c, "code": c[2:], "name": n} for c, n in self.universe]
                return self._json(rows)
            return self._json([])
        param = request.url.params["param"]
        code, _, count = param.split(",")[0], None, int(param.split(",")[-2])
        if code in self.failing:
            return self._json({"code": 0, "msg": "", "data": {code: {"day": [], "qt": {}}}})
        return self._json({"code": 0, "msg": "", "data": {code: {"day": [
            [f"{b.date // 10000}-{b.date // 100 % 100:02d}-{b.date % 100:02d}",
             str(b.open), str(b.close), str(b.high), str(b.low), str(b.volume / 100), {}, "0", str(b.amount / 10000), "0", "0"]
            for b in self._bars(code, count)], "qt": {}}}})


@pytest.fixture
def fake_tencent_transport():
    return FakeTencentTransport()


@pytest.fixture
def tmp_env(tmp_path, fake_tencent_transport):
    from pathlib import Path
    from types import SimpleNamespace

    from core.config import load_config
    from core.db import connect, migrate
    from core.http import Http, RateLimiter

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    http = Http({"tencent": RateLimiter(0.0), "sina": RateLimiter(0.0)}, transport=fake_tencent_transport)

    def count(table, **eq):
        where = " AND ".join(f"{k}=:{k}" for k in eq) or "1"
        return conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", eq).fetchone()[0]

    def one(table, **eq):
        where = " AND ".join(f"{k}=:{k}" for k in eq) or "1"
        return conn.execute(f"SELECT * FROM {table} WHERE {where}", eq).fetchone()

    http.call_count_for = lambda code: sum(1 for c in http.calls if code in c.url)
    return SimpleNamespace(cfg=cfg, http=http, conn=conn, count=count, one=one, transport=fake_tencent_transport)


# Task 12（jobs.eod）：快照时间戳与占位列的假源 + `env`。
#
# 六个假源是模块级类/工厂，`env` 只通过参数注入它们——在 fixture 里裸引用只存在于
# 某个测试模块的名字会 `NameError`（这些名字 pytest 看不到）。

class FakeSnapshotSource:
    """快照假源：预置每只票的报价 + 一个统一快照时间戳。

    `ts` 是普通属性：除权用例要把「今天」挪到别的交易日，直接改 `source.ts`；
    `rows[code]` 是 dict，`set(code, **kw)` 就地改字段（ST 用例改 name）。
    缺口补拉落在 `kline_calls` 上——假源不回 bar（补拉是否写入不是本任务的判定点）。
    """

    def __init__(self, name="tencent", ts=None, rows=None):
        self.name = name
        self.ts = ts
        self.rows = dict(rows or {})
        self.kline_calls = []
        self.last_missing = set()
        self.gap_bars: list[tuple] = []      # 缺口补拉要回的 bar：`(code,date,o,h,l,c,vol,amt)`

    def set(self, code, **kw):
        self.rows.setdefault(code, {}).update(kw)
        return self

    def pop(self, code):
        self.rows.pop(code, None)
        return self

    def fetch_snapshot(self, codes, scope="snapshot_eod"):
        wanted = set(codes)
        self.last_missing = wanted - set(self.rows)
        return {code: self._quote(code, self.rows[code]) for code in codes if code in self.rows}

    def fetch_kline_daily(self, code, count=1700, end=None, scope="backfill"):
        self.kline_calls.append((code, count))
        from core.models import Bar

        return [Bar(c, d, o, h, l, cl, v, a, self.name)
                for (c, d, o, h, l, cl, v, a) in self.gap_bars if c == code]

    def _quote(self, code, row):
        return Quote(
            code=code,
            name=row.get("name", code),
            price=row.get("price"),
            prev_close=row.get("prev_close"),
            open=row.get("open"),
            high=row.get("high"),
            low=row.get("low"),
            volume=row.get("volume"),
            amount=row.get("amount"),
            volume_ratio=None,
            limit_up=None,
            limit_down=None,
            ts=row.get("ts", self.ts),
            raw=[],
        )


def quote_row(name="X", price=10.0, prev_close=10.0, volume=100000, **kw):
    """一条正常交易日的快照行：开=收，高/低各偏 1%，额随价量。"""
    row = {
        "name": name,
        "price": price,
        "prev_close": prev_close,
        "open": kw.pop("open", price),
        "high": kw.pop("high", round(price * 1.01, 2)),
        "low": kw.pop("low", round(price * 0.99, 2)),
        "volume": volume,
        "amount": kw.pop("amount", price * volume),
    }
    row.update(kw)
    return row


def _env_rows():
    """`env` 的三只票：茅台 / 平安 / 一字跌停过的 sh600080（前收必须等于库里 9.5）。

    时间戳由 `FakeSnapshotSource.ts` 统一给（逐行设 ts 会挡住用例改整批快照日期）。
    """
    return {
        "sh600519": quote_row("贵州茅台", 1700.0, 1690.0, 1000),
        "sz000001": quote_row("中国平安", 12.0, 11.9, 2000),
        "sh600080": quote_row("ST永泰", 9.6, 9.5, 3000),
    }


@pytest.fixture
def tencent_today():
    """快照时间戳 = 今天（20261030），三只票都是正常收盘数据。"""
    return FakeSnapshotSource("tencent", ts=dt(2026, 10, 30, 15, 0), rows=_env_rows())


@pytest.fixture
def sina_today():
    return FakeSnapshotSource("sina", ts=dt(2026, 10, 30, 15, 0), rows=_env_rows())


@pytest.fixture
def tencent_holiday():
    """快照时间戳 = 20260930（国庆前一天），今天 20261005 是节假日 → 整体跳过。"""
    from datetime import datetime as _dt

    return FakeSnapshotSource("tencent", ts=_dt(2026, 9, 30, 15, 0), rows=_env_rows())


@pytest.fixture
def sina_holiday():
    from datetime import datetime as _dt

    return FakeSnapshotSource("sina", ts=_dt(2026, 9, 30, 15, 0), rows=_env_rows())


@pytest.fixture
def tencent_placeholder():
    """盘前占位：sz000001 四价相等且收在昨收（量非 0，不能当成停牌）。"""
    rows = _env_rows()
    rows["sz000001"] = quote_row("中国平安", 11.9, 11.9, 500, open=11.9, high=11.9, low=11.9)
    return FakeSnapshotSource("tencent", ts=dt(2026, 10, 30, 15, 0), rows=rows)


@pytest.fixture
def sina_placeholder():
    rows = _env_rows()
    rows["sz000001"] = quote_row("中国平安", 11.9, 11.9, 500, open=11.9, high=11.9, low=11.9)
    return FakeSnapshotSource("sina", ts=dt(2026, 10, 30, 15, 0), rows=rows)


@pytest.fixture
def env(tmp_env, tencent_holiday, sina_holiday, tencent_placeholder, sina_placeholder,
        tencent_today, sina_today):
    from types import SimpleNamespace

    from core.datasource.registry import SourceRegistry
    from tests.helpers import FakeWestockCli, _trading_days_rev

    conn = tmp_env.conn
    # 交易日历（§6.4：sh000300 的日期集合）。到 20261029 为止，20261030 由工作日兜底判定。
    for day in _trading_days_rev(20261029, 40):
        conn.execute(
            "INSERT INTO daily_bars VALUES ('sh000300', ?, 3000, 3000, 3000, 3000, 0, 0, 'backfill')",
            (day,),
        )
    conn.executemany(
        "INSERT INTO stock_meta(code, name, market, updated_at) VALUES (?, ?, 'cn_main', '2026-10-30T00:00:00+08:00')",
        [("sh600519", "贵州茅台"), ("sz000001", "中国平安"), ("sh600080", "ST永泰")],
    )
    # Review Focus #5：历史一字跌停（四价相等但 volume>0）必须保留，不能被占位门误杀。
    conn.execute(
        "INSERT INTO daily_bars VALUES ('sh600080', 20260812, 9.5, 9.5, 9.5, 9.5, 1000, 10000, 'backfill')"
    )
    conn.commit()

    return SimpleNamespace(
        cfg=tmp_env.cfg, conn=conn, count=tmp_env.count, one=tmp_env.one,
        reg_holiday=SourceRegistry(tencent_holiday, sina_holiday),
        reg_placeholder=SourceRegistry(tencent_placeholder, sina_placeholder),
        reg_today=SourceRegistry(tencent_today, sina_today),
        westock=FakeWestockCli(suspensions={"sh600519": (20260901, 20260903)}),
    )


# Task 12：新浪板块接口的假上游（行业/概念快照 + 成分股分页）。

@pytest.fixture
def sina_sector():
    """`newSinaHy.php` / `newFLJK.php` / `getHQNodeData?node=…` 三个接口的离线应答。"""
    import json

    import httpx

    from core.datasource.sina import SinaSource
    from core.http import Http, RateLimiter
    from tests.fixtures import load_fixture

    industry = (
        'var S_Finance_bankuai_sinaindustry = {"new_blhy":"new_blhy,玻璃行业,19,19.89,0.12,0.61,'
        '12345678,98765432.000,sh600519,3.45,1330.000,44.350,贵州茅台"};'
    ).encode("gbk")
    concept = (
        'var S_Finance_bankuai_sinaconcept = {"gn_xny":"gn_xny,新能源,88,12.34,-0.10,-0.80,'
        '87654321,123456789.000,sz300750,1.20,200.000,2.000,宁德时代"};'
    ).encode("gbk")
    page1 = json.dumps(load_fixture("sina_universe_page1.json"), ensure_ascii=False).encode()
    page2 = json.dumps(load_fixture("sina_universe_page2.json")).encode()

    def handler(request):
        url = str(request.url)
        if "newSinaHy" in url:
            return httpx.Response(200, content=industry)
        if "newFLJK" in url:
            return httpx.Response(200, content=concept)
        page = request.url.params.get("page")
        return httpx.Response(200, content=page1 if page == "1" else page2)

    http = Http({"sina": RateLimiter(0.0)}, transport=httpx.MockTransport(handler))
    return SinaSource(http)


# Task 18：观察池夹具——预置了能过完全部门槛的行情（两只/十二只「跌深→横盘」票）。
# `build_pool` 在这些库上产出的池子行就是被测对象；夹具只负责造行情，不写 pool 表。

@pytest.fixture
def db_two_equal_scores(db):
    """两只行情完全相同的票：分数相等，排序只能由 `code ASC` 决定。"""
    from core.strategy.pool import refresh_stock_stats
    from tests.helpers import AS_OF, seed_index_bars, seed_pool_stock, _trading_days_rev

    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    for code in ("sh600001", "sh600002"):
        seed_pool_stock(db, code)
    refresh_stock_stats(db, AS_OF)
    return db


@pytest.fixture
def db_with_12_hits(db):
    """12 只合格票：`candidates < pool_alert_min(30)` → 池子告警 `pool_low`。"""
    from core.strategy.pool import refresh_stock_stats
    from tests.helpers import AS_OF, seed_index_bars, seed_pool_stock, _trading_days_rev

    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    for i in range(12):
        seed_pool_stock(db, f"sh6001{i:02d}")
    refresh_stock_stats(db, AS_OF)
    return db


class OfflineChatLlm:
    """测试用的离线 `ChatSession.llm`：**任何出站请求都直接失败**。

    原来 api fixture 不注入 llm，`ChatSession` 会自建 `LlmClient(cfg)` 去连
    `config/llm.yaml` 里的地址——测试里就是真实的网络尝试（本机无网络时表现为
    DNS 失败，只是恰好没让用例红，却拖慢且污染边界）。
    行为与"没配 key"一致：抛异常由 `ChatSession` 自己的降级路径处理。
    """

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, **kw):
        self.calls += 1
        from llm.client import RetriesExhausted

        raise RetriesExhausted("测试环境不发起真实 LLM 请求", 1)


@pytest.fixture
def api(tmp_db, CFG):
    """返回一个「造好数据 → 取 AsyncClient」的工厂，避免每个用例重复装配。"""
    import httpx

    from api.routes import create_app
    from core.clock import FixedClock

    app = create_app(CFG, lambda: tmp_db, FixedClock(_API_T0),
                     chat_llm=OfflineChatLlm())
    app.state.conn = tmp_db

    def make():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    make.app = app
    return make


@pytest.fixture(autouse=True)
def _reset_auth_failures():
    """每个用例前清空口令失败计数。

    `api.auth` 的失败计数是**进程内模块状态**，跨用例会累积：某个用例尝试了
    几次错误口令，后面的用例就可能撞上限流、拿到 429 而不是预期的 401。
    这类污染极难定位（单跑绿、整跑红），所以统一在入口清掉。
    """
    from api import auth

    auth._fails.clear()
    # 清扫节流也要重置：`_last_sweep` 是模块级、跨用例残留，
    # 前一个用例把它推到很后面，后一个用例的清扫就不会发生（单跑绿、整跑红）
    auth._last_sweep = 0.0
    yield
    auth._fails.clear()
    auth._last_sweep = 0.0


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """**默认禁止真实出站连接**：测试必须是离线可复现的。

    审查发现有些用例会真的发网络请求（api fixture 自建 `LlmClient`、自检用例
    直接 `httpx.post`），表现是本机没网时 DNS 失败却不红、有网时又拖慢整套。
    这类"边界"不会让用例失败，只会让结果依赖运行环境。

    需要真实出站的地方应显式注入 `httpx.MockTransport`；确有不走 httpx 的
    （比如 subprocess 跑 node），要么换成假 runner，要么用 `mock_transport` 挡掉。
    """
    import socket

    real_connect = socket.socket.connect

    def guard(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost", ""):
            raise RuntimeError(
                f"测试发起了真实网络连接：{host}:{address[1] if isinstance(address, tuple) else ''}。"
                "请注入 MockTransport / 假 runner，不要依赖外网。")
        return real_connect(self, address, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", guard, raising=False)


# ---- 临时目录隔离：**所有** `tempfile.mkdtemp()` 都落进 pytest 的 tmp_path ----
#
# 2026-11-21 事故：本机的长期测试容器 `ss-dev-run`（`sleep infinity`，从不删除）
# 可写层涨到 **230 GB**，撑爆 Docker Desktop 虚拟磁盘，连带 PostgreSQL 起不来
# （`could not write lock file "postmaster.pid": No space left on device`）。
#
# 直接原因有两条，这条 fixture 治的是第一条：
#   1. `tests/chat/test_session.py` 有 12 处、`tests/jobs/test_main_scheduler.py`
#      有 1 处直接 `tempfile.mkdtemp()`，**从不清理**——每次跑测试都在 /tmp
#      里留十几个目录和 SQLite 文件；
#   2. 容器本身长期不删（`docker run --rm` 会收拾，`sleep infinity` 的守护容器不会）。
#
# 用 autouse + 猴补 `tempfile.mkdtemp`，而不是逐个改调用点：以后新写的测试
# 也会自动被约束，不用靠人记得。pytest 的 `tmp_path` 会按用例清理，
# 整体再由 `--basetemp`（见 Makefile 的 `--tmpfs /tmp`）兜底。
@pytest.fixture(autouse=True)
def _isolate_mkdtemp(tmp_path, monkeypatch):
    """把 `tempfile.mkdtemp()` 重定向到本用例的 `tmp_path` 下。"""
    import itertools
    import os as _os
    import tempfile as _tf

    counter = itertools.count()

    def _mkdtemp(suffix: str | None = None, prefix: str | None = None,
                 dir: str | None = None) -> str:
        target = tmp_path / f"mk{next(counter)}"
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(_tf, "mkdtemp", _mkdtemp)

    # **同一类入口都要收**：评审指出只猴补 `mkdtemp` 的话，
    # 将来有人用 `mkstemp()` / `NamedTemporaryFile(delete=False)` 又会漏到 /tmp。
    # 这两个没有"自动清理"的语义，同样会跨用例堆积。
    real_mkstemp = _tf.mkstemp

    def _mkstemp(suffix: str | None = None, prefix: str | None = None,
                 dir: str | None = None, text: bool = False) -> tuple[int, str]:
        target = tmp_path / f"tmp{next(counter)}{suffix or ''}"
        fd = _os.open(target, _os.O_RDWR | _os.O_CREAT | _os.O_EXCL, 0o600)
        return fd, str(target)

    monkeypatch.setattr(_tf, "mkstemp", _mkstemp)

    _real_ntf = _tf.NamedTemporaryFile

    def _named(*args, **kw):
        # 落到 tmp_path 里；`delete=` 保持调用方语义（pytest 会整体删 tmp_path，
        # 所以即使 delete=False 也不会跨用例泄漏）
        kw.setdefault("dir", str(tmp_path))
        return _real_ntf(*args, **kw)

    monkeypatch.setattr(_tf, "NamedTemporaryFile", _named)
    yield
