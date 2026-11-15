"""westock-mcp 只读封装、自选股单向导入、续期脚本三键（先红后绿）。

覆盖 Step 1 清单里属于本文件的条目：
① 分组参数的键是 `group` 不是 `group_id`（传 `group_id` 会被上游静默忽略并返回全部）；
② `mcp_unavailable` 降级（token 缺失 / 401）；
③ `set_alert` 先读后合并、只增删 `SYS:` 前缀项；
④ `import_watchlist` 两次运行结果相同、不动 `source='manual'` 的行；
⑤ `WestockMcp.schema()` 原样返回 Task 3 的 `MCP_TOOL_SCHEMAS`（6 个，写工具标「确认」）；
⑥ `scripts/wb_auth_headless.mjs` 落 `WESTOCK_MCP_URL` / `WESTOCK_ACCESS_TOKEN` /
   `WESTOCK_EXPIRES_AT` 三键，成功 exit 0、失败 exit 1。

**不用真实网络**：HTTP 层注入假客户端；续期脚本用本地 stub server 顶替腾讯的
register / weixin-login / authorize / token 四个端点（`WB_AUTH_BASE` / `WB_PROXY_BASE`）。
"""
from __future__ import annotations

import json
import json as _json          # `post(..., json=…)` 的参数名会遮蔽模块名，函数内要用别名
from datetime import datetime
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from core.clock import FixedClock
from llm.tools import MCP_TOOL_SCHEMAS
from mcp.westock import WestockMcp, import_watchlist

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "wb_auth_headless.mjs"
ENV_BODY = ("WESTOCK_MCP_URL=https://mcp.example/mcp\n"
            "WESTOCK_ACCESS_TOKEN=tok-1\n"
            "WESTOCK_EXPIRES_AT=1893456000\n")


# ---------------------------------------------------------------- 假 HTTP 层


class Resp:
    """`httpx.Response` 的最小替身：只暴露实现里真正用到的三个属性。"""

    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload, ensure_ascii=False)
        self.headers = headers or {}


class FakeHttp:
    """按 `tools/call` 的底层工具名返回预置数据，并把每次调用记在 `calls` 里。"""

    def __init__(self, rows=None, status=200, envelope_text=None):
        self.rows = rows or {}
        self.status = status
        self.envelope_text = envelope_text
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None, **kw):        # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        if self.status != 200:
            return Resp(self.status, {"error": "unauthorized"}, text='{"error":"unauthorized"}')
        name = ((json or {}).get("params") or {}).get("name")
        payload = self.rows.get(name, [])
        text = self.envelope_text if self.envelope_text is not None else _json.dumps(
            payload, ensure_ascii=False)
        return Resp(200, {"jsonrpc": "2.0", "id": 1,
                          "result": {"content": [{"type": "text", "text": text}],
                                     "isError": False}})

    # 断言辅助
    def last_call(self) -> dict:
        assert self.calls, "没有发出任何 MCP 请求"
        return self.calls[-1]

    def last_args(self) -> dict:
        return ((self.last_call()["json"].get("params") or {}).get("arguments") or {})

    def names(self) -> list[str]:
        return [((c["json"].get("params") or {}).get("name")) for c in self.calls]


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / "westock.env"
    p.write_text(ENV_BODY, encoding="utf-8")
    return p


@pytest.fixture
def clock():
    from tests.helpers import SH

    return FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH))


def make_mcp(env_file, http, clock=None):
    return WestockMcp(env_path=env_file, http=http, clock=clock,
                      limiter=_NoWaitLimiter())


class _NoWaitLimiter:
    """测试里不真的睡 300ms：只记调用次数。"""

    def __init__(self):
        self.acquires = 0

    def acquire(self):
        self.acquires += 1


# ---------------------------------------------------------------- schema


def test_schema_returns_task3_schemas_as_is(env_file):
    """6 个工具的 schema 由 Task 3 声明，本任务**原样**返回（两处定义会漂移）。"""
    mcp = make_mcp(env_file, FakeHttp())
    assert mcp.schema() is MCP_TOOL_SCHEMAS
    assert len(mcp.schema()) == 6


def test_schema_write_tools_say_needs_confirm(env_file):
    mcp = make_mcp(env_file, FakeHttp())
    by_name = {t["function"]["name"]: t["function"] for t in mcp.schema()}
    for name in ("mcp_add_watch", "mcp_remove_watch", "mcp_set_alert", "mcp_cancel_alert"):
        assert "确认" in by_name[name]["description"], name
        assert by_name[name]["strict"] is True
    for name in ("mcp_read_watchlist", "mcp_read_alerts"):
        assert "确认" not in by_name[name]["description"], name


# ---------------------------------------------------------------- 读：参数键名


def test_read_watchlist_sends_group_key_not_group_id(env_file):
    """`group_id` 会被上游静默忽略并返回全部分组（spec §5.4.5），必须发 `group`。"""
    http = FakeHttp({"portfolio_watchlist": [{"code": "sh600519", "name": "贵州茅台"}]})
    mcp = make_mcp(env_file, http)
    env = mcp.read_watchlist(group="沪深", market="A")
    args = http.last_args()
    assert args["group"] == "沪深"
    assert "group_id" not in args
    assert args["market"] == "A"
    assert http.last_call()["json"]["params"]["name"] == "portfolio_watchlist"
    assert env["ok"] is True


def test_market_only_accepts_a_hk_us(env_file):
    """`market` 只认 `A`/`HK`/`US`：传中文被上游忽略，与其静默返回全部，不如本地不发。"""
    http = FakeHttp({"portfolio_watchlist": []})
    mcp = make_mcp(env_file, http)
    mcp.read_watchlist(market="沪深")
    assert "market" not in http.last_args()
    mcp.read_watchlist(market="HK")
    assert http.last_args()["market"] == "HK"


def test_read_watchlist_envelope_has_five_keys(env_file):
    http = FakeHttp({"portfolio_watchlist": [{"code": "sh600519"}]})
    mcp = make_mcp(env_file, http, clock=FixedClock(datetime(2026, 10, 30, 10, 0)))
    env = mcp.read_watchlist()
    assert set(env) == {"ok", "data", "as_of", "reason", "adjust"}
    assert env["ok"] is True and env["reason"] is None
    assert env["data"][0]["code"] == "sh600519"


def test_authorization_header_carries_bearer_token(env_file):
    http = FakeHttp({"portfolio_tips_query": []})
    mcp = make_mcp(env_file, http)
    mcp.read_alerts()
    assert http.last_call()["headers"]["Authorization"] == "Bearer tok-1"
    assert http.last_call()["json"]["params"]["name"] == "portfolio_tips_query"


def test_rereads_env_file_on_every_call(env_file):
    """续期成功后不重启也必须生效：每次调用重读 env 文件。"""
    http = FakeHttp({"portfolio_tips_query": []})
    mcp = make_mcp(env_file, http)
    mcp.read_alerts()
    env_file.write_text(ENV_BODY.replace("tok-1", "tok-2"), encoding="utf-8")
    mcp.read_alerts()
    assert http.calls[0]["headers"]["Authorization"] == "Bearer tok-1"
    assert http.calls[1]["headers"]["Authorization"] == "Bearer tok-2"


# ---------------------------------------------------------------- 降级


def test_missing_token_degrades_to_mcp_unavailable(tmp_path, clock):
    http = FakeHttp({"portfolio_watchlist": []})
    mcp = WestockMcp(env_path=tmp_path / "absent.env", http=http, clock=clock,
                     limiter=_NoWaitLimiter())
    env = mcp.read_watchlist()
    assert env["ok"] is False and env["reason"] == "mcp_unavailable"
    assert http.calls == []                    # 没有 token 就不该打上游


def test_401_degrades_to_mcp_unavailable(env_file, clock):
    http = FakeHttp(status=401)
    mcp = make_mcp(env_file, http, clock=clock)
    env = mcp.read_watchlist()
    assert env["ok"] is False and env["reason"] == "mcp_unavailable"
    assert env["as_of"]


def test_transport_error_degrades_without_raising(env_file, clock):
    class Boom(FakeHttp):
        def post(self, *a, **kw):
            raise RuntimeError("connect timed out")

    mcp = make_mcp(env_file, Boom(), clock=clock)
    env = mcp.read_alerts()
    assert env["ok"] is False and env["reason"].startswith("mcp_error")


# ---------------------------------------------------------------- 写：先读后合并


def alerts_server(rows):
    return FakeHttp({"portfolio_tips_query": rows, "portfolio_tips_set": {"ok": True}})


def test_set_alert_keeps_user_manual_alert(env_file):
    """`portfolio_tips_set` 是全量覆盖：必须先 query 回填，只增删 `SYS:` 项。"""
    manual = {"id": 7, "code": "sz000001", "text": "我自己的提醒 5%", "kind": "change",
              "threshold": 5}
    old_sys = {"id": 9, "code": "sh600519", "text": "SYS:up 100", "kind": "up", "threshold": 100}
    http = alerts_server([manual, old_sys])
    mcp = make_mcp(env_file, http)

    env = mcp.set_alert(code="sh600519", kind="up", threshold=1200)
    assert env["ok"] is True
    assert http.names() == ["portfolio_tips_query", "portfolio_tips_set"]
    sent = http.last_args()["tips"]
    texts = [json.dumps(row, ensure_ascii=False) for row in sent]
    assert any("我自己的提醒 5%" in t for t in texts)        # 手设的那条保留
    assert not any("SYS:up 100" in t for t in texts)         # 同 code 同 kind 的旧 SYS 被替换
    assert any("SYS:" in t and "1200" in t for t in texts)   # 新 SYS 项进去了


def test_set_alert_does_not_write_when_read_fails(env_file):
    """读不到现值就不能做全量覆盖——那会把用户的提醒全部抹掉。"""
    http = FakeHttp(status=500)
    mcp = make_mcp(env_file, http)
    env = mcp.set_alert(code="sh600519", kind="up", threshold=1200)
    assert env["ok"] is False
    assert http.names() == ["portfolio_tips_query"]          # 没有发 set


def test_cancel_alert_only_removes_matching_sys_item(env_file):
    manual = {"id": 7, "code": "sh600519", "text": "我的成本提醒", "kind": "up", "threshold": 1}
    keep_sys = {"id": 8, "code": "sz000001", "text": "SYS:up 60", "kind": "up", "threshold": 60}
    drop_sys = {"id": 9, "code": "sh600519", "text": "SYS:up 1200", "kind": "up", "threshold": 1200}
    http = alerts_server([manual, keep_sys, drop_sys])
    mcp = make_mcp(env_file, http)
    env = mcp.cancel_alert(code="sh600519", kind="up")
    assert env["ok"] is True
    texts = [json.dumps(row, ensure_ascii=False) for row in http.last_args()["tips"]]
    assert any("我的成本提醒" in t for t in texts)
    assert any("SYS:up 60" in t for t in texts)
    assert not any("SYS:up 1200" in t for t in texts)


def test_add_and_remove_watch_pass_code(env_file):
    http = FakeHttp({"portfolio_watchlist_add": {"ok": True},
                     "portfolio_watchlist_remove": {"ok": True}})
    mcp = make_mcp(env_file, http)
    assert mcp.add_watch(code="sh600519", note="突破箱体")["ok"] is True
    assert http.last_call()["json"]["params"]["name"] == "portfolio_watchlist_add"
    assert http.last_args()["code"] == "sh600519"
    assert mcp.remove_watch(code="sh600519")["ok"] is True
    assert http.last_call()["json"]["params"]["name"] == "portfolio_watchlist_remove"


# ---------------------------------------------------------------- 单向导入


class FakeMcp:
    """`import_watchlist` 只需要 read_watchlist。"""

    def __init__(self, rows, ok=True):
        self.rows = rows
        self.ok = ok
        self.calls = 0

    def read_watchlist(self, group=None, market=None):
        self.calls += 1
        if not self.ok:
            return {"ok": False, "data": None, "as_of": "2026-10-30T10:00:00+08:00",
                    "reason": "mcp_unavailable", "adjust": None}
        return {"ok": True, "data": self.rows, "as_of": "2026-10-30T10:00:00+08:00",
                "reason": None, "adjust": None}


def _watch_rows(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT code, source FROM watch_stock")}


def test_import_watchlist_is_idempotent_and_keeps_manual(conn):
    conn.execute("INSERT INTO watch_stock(code, note, pct_threshold, source, added_at)"
                 " VALUES ('sh600036','手设的', 3.0, 'manual', '2026-10-01T09:00:00+08:00')")
    conn.execute("INSERT INTO watch_stock(code, note, pct_threshold, source, added_at)"
                 " VALUES ('sz000001', NULL, NULL, 'manual', '2026-10-01T09:00:00+08:00')")
    conn.commit()
    mcp = FakeMcp([{"code": "sh600519", "name": "贵州茅台"},
                   {"code": "sh600036", "name": "招商银行"}])       # sh600036 已是 manual

    assert import_watchlist(conn, mcp) == 1                       # 只新增 sh600519
    after_first = _watch_rows(conn)
    assert after_first == {"sh600036": "manual", "sz000001": "manual", "sh600519": "westock"}

    assert import_watchlist(conn, mcp) == 0                       # 第二次什么都不做
    assert _watch_rows(conn) == after_first                        # 结果相同
    # `manual` 行的 note/pct_threshold 一个字都没被改
    row = conn.execute("SELECT note, pct_threshold, source FROM watch_stock WHERE code='sh600036'"
                       ).fetchone()
    assert tuple(row) == ("手设的", 3.0, "manual")
    # 不在手机自选股里的本地行**不删**（单向导入，不回写）
    assert "sz000001" in _watch_rows(conn)


def test_import_watchlist_unavailable_returns_zero(conn):
    assert import_watchlist(conn, FakeMcp([], ok=False)) == 0
    assert _watch_rows(conn) == {}


def test_import_watchlist_skips_rows_without_usable_code(conn):
    """裸六位代码没有市场前缀，`classify_market` 明确「不要猜」（core/market.py）。"""
    mcp = FakeMcp([{"code": "600519"}, {"symbol": "sz000001"}])
    assert import_watchlist(conn, mcp) == 1
    assert _watch_rows(conn) == {"sz000001": "westock"}


# ---------------------------------------------------------------- 续期脚本（真跑 node）


class _Stub:
    """腾讯四个端点的本地替身：register / weixin-login / authorize / token。"""

    def __init__(self, fail_token=False):
        self.fail_token = fail_token
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):                      # 静音
                pass

            def _send(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):                               # noqa: N802
                url = urlparse(self.path)
                outer.requests.append("GET " + url.path)
                q = parse_qs(url.query)
                if url.path.endswith("/login/weixin/login"):
                    return self._send({"code": 0, "msg": "ok", "data": {
                        "openid": "o-openid", "fskey": "f-fskey", "check": "c-check"}})
                if url.path.endswith("/authorize"):
                    return self._send({"code": "auth-code-1",
                                       "state": (q.get("state") or [""])[0]})
                return self._send({"error": "not found"}, 404)

            def do_POST(self):                              # noqa: N802
                url = urlparse(self.path)
                outer.requests.append("POST " + url.path)
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                if url.path.endswith("/register"):
                    return self._send({"client_id": "stub-client"})
                if url.path.endswith("/token"):
                    if outer.fail_token:
                        return self._send({"error": "invalid_grant"}, 500)
                    return self._send({"access_token": "tok-from-stub", "expires_in": 3600,
                                       "refresh_token": "tok-from-stub", "token_type": "Bearer"})
                return self._send({"error": "not found"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}/cgi/cgi-bin/openai/mcp/oauth"

    @property
    def proxy(self):
        return f"http://127.0.0.1:{self.port}"


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 18+")


def _run_script(tmp_path, stub):
    out = tmp_path / "out" / "westock.env"
    cmd = ["node", str(SCRIPT), "--out", str(out), "--wx-code", "wx-code-1",
           "--client-cache", str(tmp_path / "client.json"),
           "--session-cache", str(tmp_path / "session.json"),
           "--stamp", str(tmp_path / "stamp"), "--min-interval-ms", "0"]
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "WB_AUTH_BASE": stub.base,
        "WB_PROXY_BASE": stub.proxy,
        "WESTOCK_MCP_URL": "https://mcp.example/mcp",
    }
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env, cwd=ROOT), out


@needs_node
def test_wb_auth_headless_writes_the_three_renewal_keys(tmp_path):
    """`--out` 的键名与读取方逐字一致；只落 `zxg_*` 等于续期成功后仍 `mcp_unavailable`。"""
    with _Stub() as stub:
        proc, out = _run_script(tmp_path, stub)
    assert proc.returncode == 0, proc.stderr[-2000:]
    kv = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    assert set(kv) == {"WESTOCK_MCP_URL", "WESTOCK_ACCESS_TOKEN", "WESTOCK_EXPIRES_AT"}
    assert kv["WESTOCK_MCP_URL"] == "https://mcp.example/mcp"
    assert kv["WESTOCK_ACCESS_TOKEN"] == "tok-from-stub"
    assert kv["WESTOCK_EXPIRES_AT"].isdigit()          # epoch 秒字符串，与 renew 的比较口径一致
    assert abs(int(kv["WESTOCK_EXPIRES_AT"]) - (time.time() + 3600)) < 300


@needs_node
def test_wb_auth_headless_exits_1_on_failure(tmp_path):
    with _Stub(fail_token=True) as stub:
        proc, out = _run_script(tmp_path, stub)
    assert proc.returncode == 1
    assert not out.exists()                            # 失败不留半成品凭据


# ---------------------------------------------------------------- 除权 / 停牌取数
#
# 下面的 fixture 数据是**从真实接口抓回来的原文**（2026-11-15），不是编的。
# 字段名、单位、"多只票返回以 code 为键的字典"这些坑，全是实测撞出来的。


#: `data_dividend` 单只票的真实返回（sh603259，含转增）
_DIV_SINGLE = {
    "code": "sh603259",
    "end": "2026-11-15",
    "plans": [
        {"bonusShareRatio": "", "cashDiviRMB": "5.80", "dividendFlag": "是",
         "dividendPlan": "10派5.800元转4.000股", "dividendType": "有分红",
         "exDiviDate": "20260710", "procedure": "方案实施", "proposalSn": 1,
         "reportEndDate": "20251231", "rightRegDate": "20260709",
         "totalCashDiviComRMB": "1.0", "tranAddShareRatio": "4.00"},
        # 未实施的预案且**没有**除权日
        {"bonusShareRatio": "", "cashDiviRMB": "1.00", "dividendFlag": "否",
         "dividendPlan": "10派1.000元", "dividendType": "有分红",
         "exDiviDate": "", "procedure": "董事会预案", "proposalSn": 1,
         "reportEndDate": "20260930", "rightRegDate": "",
         "totalCashDiviComRMB": "0", "tranAddShareRatio": ""},
        # 未实施的预案但**填了**除权日（预案里排的日期）。只靠"有没有除权日"
        # 过滤是漏不掉的——必须按 `procedure` 判。不判就会把还没实施的方案
        # 当成已除权，复权因子提前偏移。
        {"bonusShareRatio": "", "cashDiviRMB": "2.00", "dividendFlag": "否",
         "dividendPlan": "10派2.000元", "dividendType": "有分红",
         "exDiviDate": "20270415", "procedure": "股东大会决议", "proposalSn": 1,
         "reportEndDate": "20260331", "rightRegDate": "",
         "totalCashDiviComRMB": "0", "tranAddShareRatio": ""},
    ],
}

#: `data_dividend` 多只票的真实返回：**以 code 为键的字典**，顶层没有 plans
_DIV_MULTI = {
    "sh600036": {"code": "sh600036", "end": "2026-11-15", "plans": [
        {"cashDiviRMB": "10.03", "dividendPlan": "10派10.030元",
         "exDiviDate": "20260710", "procedure": "方案实施",
         "bonusShareRatio": "", "tranAddShareRatio": ""}]},
    "sz300059": {"code": "sz300059", "end": "2026-11-15", "plans": [
        {"cashDiviRMB": "0.70", "dividendPlan": "10派0.700元转2.000股",
         "exDiviDate": "20260520", "procedure": "方案实施",
         "bonusShareRatio": "", "tranAddShareRatio": "2.00"}]},
}

#: `data_suspension` 的真实返回：`resumptionDate=19000101` 表示"还没定复牌日"
_SUSPENSION = {"market": "hs", "stocks": [
    {"code": "sz000635", "name": "英 力 特", "resumptionDate": "19000101",
     "status": "", "statusDesc": "重大事项", "suspendDate": "20261113"},
    {"code": "sz000831", "name": "中国稀土", "resumptionDate": "20261116",
     "status": "", "statusDesc": "重大事项", "suspendDate": "20261102"},
]}


def _mcp_with(env_file, rows):
    return WestockMcp(env_path=env_file, http=FakeHttp(rows), clock=FixedClock(
        datetime(2026, 11, 15, 10, 0)))


def _wrapped(payload):
    """按**真实接口**的形状包一层：`content[].text` 是 `{"ok":true,"data":…}`。

    这一点最初就是因为 fixture 用了平铺数据而漏掉的：假数据平铺、真数据双层，
    于是单测全绿、线上一条都取不到。fixture 必须照真实形状写。
    """
    return {"ok": True, "data": payload}


def test_batch_dividend_maps_real_payload(env_file):
    """字段映射与**单位**必须对：接口是「每 10 股」，落了库要换成每股。

    实测对照：`cashDiviRMB='5.80'` 对应 `dividendPlan='10派5.800元转4.000股'`，
    `tranAddShareRatio='4.00'` 对应「转 4.000 股」。不除 10 会让现金虚高 10 倍，
    复权因子整个算错——这类错误在图上不会报错，只会让价格看起来不对。
    """
    mcp = _mcp_with(env_file, {"data_dividend": _wrapped(_DIV_SINGLE)})
    got = mcp.batch_dividend(["sh603259"], years=10)

    assert list(got) == ["sh603259"]
    actions = got["sh603259"]
    assert len(actions) == 1, "未实施的预案（无除权日）必须被丢掉"
    a = actions[0]
    assert a.code == "sh603259" and a.ex_date == 20260710
    assert a.cash_per_share == pytest.approx(0.58)        # 5.80 / 10
    assert a.tran_ratio == pytest.approx(0.40)            # 4.00 / 10
    assert a.bonus_ratio == 0.0
    assert a.source == "westock_dividend"


def test_batch_dividend_reads_dict_shaped_multi_code_reply(env_file):
    """多只票的返回是**以 code 为键的字典**，顶层没有 `plans`。

    单只票是 `{code, plans}`、多只票是 `{sh600036: {...}, ...}`——两种都出现过。
    只按列表处理会让多只票**一条都取不到**，而 silently 返回空 dict 看起来像
    "这段时间没有分红"，最难发现。
    """
    mcp = _mcp_with(env_file, {"data_dividend": _wrapped(_DIV_MULTI)})
    got = mcp.batch_dividend(["sh600036", "sz300059"], years=10)

    assert set(got) == {"sh600036", "sz300059"}
    assert got["sh600036"][0].cash_per_share == pytest.approx(1.003)
    assert got["sz300059"][0].cash_per_share == pytest.approx(0.07)
    assert got["sz300059"][0].tran_ratio == pytest.approx(0.2)


def test_batch_dividend_sends_comma_joined_codes(env_file):
    """入参契约：`codes` 是**逗号分隔的字符串**，不是数组。"""
    mcp = _mcp_with(env_file, {"data_dividend": _wrapped(_DIV_SINGLE)})
    mcp.batch_dividend(["sh603259", "sz300059"], years=8)

    assert mcp.http.calls, "没有发请求"
    sent = mcp.http.last_args()
    assert sent["codes"] == "sh603259,sz300059"
    assert sent["years"] == 8


def test_batch_dividend_returns_empty_on_failure_instead_of_crashing(env_file):
    """取数失败返回空 dict：周刷新不该把 jobs 主链路带崩。"""
    mcp = WestockMcp(env_path=env_file, http=FakeHttp({}, status=401),
                     clock=FixedClock(datetime(2026, 11, 15, 10, 0)))
    assert mcp.batch_dividend(["sh600519"]) == {}


def test_suspension_parses_real_payload_and_handles_undecided_resumption(env_file):
    """`resumptionDate=19000101` 是"还没定"的哨兵值，不能原样落成 1900 年。

    接口用它表示"至今未复牌"。原样落库的话 `resume_dates()`（按
    `end_date == 上一交易日` 找复牌股）永远命中不了，但这条记录也永远清不掉。
    这里换成查询区间的右端（今天），语义是"仍在停牌"。
    """
    mcp = _mcp_with(env_file, {"data_suspension": _wrapped(_SUSPENSION)})
    rows = mcp.suspension("2026-11-01", "2026-11-15")

    assert (("sz000831", 20261102, 20261116) in rows), rows
    stuck = [r for r in rows if r[0] == "sz000635"][0]
    assert stuck[1] == 20261113
    assert stuck[2] == 20261115, f"19000101 应换成区间右端，实际 {stuck[2]}"


def test_suspension_uses_hs_market_argument(env_file):
    """`data_suspension` 的 market 只认 hs/hk/us，与 `MARKETS`（A/HK/US）不是一套。"""
    mcp = _mcp_with(env_file, {"data_suspension": _wrapped(_SUSPENSION)})
    mcp.suspension("2026-11-01", "2026-11-15")
    assert mcp.http.last_args()["market"] == "hs"


def test_refresh_hooks_are_wired_when_client_supports_it(env_file):
    """客户端具备两个方法时，`make_refresh_hooks` 必须返回真的回调而不是 (None, None)。

    这是这一整块的关键：原先 `WestockMcp` 没有这两个方法，回调恒为 None，
    `corporate_actions` 只有快照反推、`suspension_calendar` 永远是空表，
    而启动日志只会打一条 warning。
    """
    from jobs.main import make_refresh_hooks

    mcp = _mcp_with(env_file, {"data_dividend": _wrapped(_DIV_SINGLE),
                              "data_suspension": _wrapped(_SUSPENSION)})
    assert hasattr(mcp, "batch_dividend") and hasattr(mcp, "suspension")
    actions, suspensions = make_refresh_hooks(mcp, cfg=None, conn=_Conn([]))
    assert actions is not None, "有 batch_dividend 却仍返回 None：回调没接线"
    assert suspensions is not None, "有 suspension 却仍返回 None：回调没接线"


class _Conn:
    """`make_refresh_hooks` 只用到 `conn.execute(...)` 取 code 列表。"""

    def __init__(self, codes):
        self._codes = codes

    def execute(self, sql, params=()):
        return _Rows(self._codes)


class _Rows:
    def __init__(self, rows):
        self._rows = [(c,) for c in rows]

    def __iter__(self):
        return iter(self._rows)
