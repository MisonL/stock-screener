"""只读接口、签名回调与状态（§7.5、D19/D20/D23/D25）。"""
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from api.routes import create_app
from core.clock import FixedClock
from notify.hmac_sign import day_end_ts, ensure_secret, sign
from tests.helpers import SH, seed_sig_context

T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)


@pytest.fixture
def read_at(tmp_db):
    return lambda sid: (tmp_db.execute("SELECT read_at FROM signals WHERE id=?", (sid,)).fetchone() or [None])[0]


def test_snapshot_parsed_once_per_as_of(api, tmp_db, CFG):
    import asyncio

    from core.snapcache import SnapshotCache

    calls = []
    orig = SnapshotCache.get

    def spy(self, scope):
        calls.append(scope)
        return orig(self, scope)

    SnapshotCache.get = spy
    try:
        tmp_db.execute("INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES"
                       " ('market', 'a1', ?)", (json.dumps({"as_of": "a1", "rows": [{"code": "sh600519"}]}),))
        tmp_db.commit()

        async def run():
            async with api() as client:
                r1 = await client.get("/api/quotes")
                r2 = await client.get("/api/quotes")
                return r1, r2

        r1, r2 = asyncio.run(run())
        assert r1.status_code == 200 and r2.status_code == 200
        assert len(calls) == 2                      # 两次请求各查一次 as_of
        # 但解析只做一次：as_of 没变就不重复 json.loads
        from core.snapcache import SnapshotCache as SC

        cache = SC(lambda: tmp_db)
        cache.get("market")
        before = cache._cache["market"][1]
        assert cache.get("market") is before
    finally:
        SnapshotCache.get = orig


def test_snapshot_cache_new_as_of_refreshes_and_parses_once(tmp_db, monkeypatch):
    """as_of 变了必须重新解析；同一个 as_of 只解析一次（`_loads` 计数可证伪）。"""
    import core.snapcache as sc

    parsed: list[str] = []
    orig = sc._loads

    def spy(payload):
        parsed.append(payload)
        return orig(payload)

    monkeypatch.setattr(sc, "_loads", spy)
    tmp_db.execute("INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES"
                   " ('market', 'a1', ?)",
                   (json.dumps({"as_of": "a1", "rows": [{"code": "sh600519", "price": 10.0}]}),))
    tmp_db.commit()

    cache = sc.SnapshotCache(lambda: tmp_db)
    first = cache.get("market")
    assert first["rows"][0]["price"] == 10.0
    assert cache.get("market") is first
    assert len(parsed) == 1                    # 同一 as_of：只解析一次

    tmp_db.execute("UPDATE snapshot_cache SET as_of = 'a2', payload = ? WHERE scope = 'market'",
                   (json.dumps({"as_of": "a2", "rows": [{"code": "sh600519", "price": 20.0}]}),))
    tmp_db.commit()

    second = cache.get("market")
    assert second["rows"][0]["price"] == 20.0  # 新 as_of：返回新 payload
    assert second is not first
    assert len(parsed) == 2                    # 新 as_of 只再解析一次
    cache.get("market")
    assert len(parsed) == 2


def test_quotes_market_filter_infers_market_from_code(api, tmp_db):
    """快照行没有 market 字段时按代码前缀推断，`?market=` 不能把全部行放过去。"""
    import asyncio

    rows = [
        {"code": "sz300750", "price": 1.0},
        {"code": "sh600519", "price": 2.0},
        {"code": "sh688981", "price": 3.0},
        {"code": "bj430047", "price": 4.0},
        {"code": "hk00700", "price": 5.0},
    ]
    tmp_db.execute("INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES ('market', 'a1', ?)",
                   (json.dumps({"as_of": "a1", "rows": rows}),))
    tmp_db.commit()

    async def run():
        async with api() as client:
            return {m: (await client.get(f"/api/quotes?market={m}")).json()["rows"]
                    for m in ("cn_gem", "cn_star", "cn_bse", "hk", "cn_main")}

    got = asyncio.run(run())
    assert [r["code"] for r in got["cn_gem"]] == ["sz300750"]
    assert [r["code"] for r in got["cn_star"]] == ["sh688981"]
    assert [r["code"] for r in got["cn_bse"]] == ["bj430047"]
    assert [r["code"] for r in got["hk"]] == ["hk00700"]
    assert [r["code"] for r in got["cn_main"]] == ["sh600519"]


def test_quote_falls_back_to_market_scope(api, tmp_db):
    import asyncio

    tmp_db.execute("INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES"
                   " ('market','a1',?), ('pool','a1',?)",
                   (json.dumps({"rows": [{"code": "sh600519", "price": 10.0}]}),
                    json.dumps({"rows": []})))
    tmp_db.commit()

    async def run():
        async with api() as client:
            return await client.get("/api/quote/sh600519"), await client.get("/api/quote/sh600000")

    ok, missing = asyncio.run(run())
    assert ok.json()["price"] == 10.0 and missing.status_code == 404


def test_kline_default_raw_and_adj_switch(api, tmp_db):
    import asyncio

    tmp_db.executemany(
        "INSERT OR REPLACE INTO daily_bars(code,date,open,high,low,close,volume,amount,source)"
        " VALUES ('sh600519',?,10,11,9,10.5,1e6,1e7,'x')", [(20261028,), (20261029,)])
    tmp_db.executemany(
        "INSERT OR REPLACE INTO daily_bars_adj(code,date,open,high,low,close,volume,factor,vol_factor)"
        " VALUES ('sh600519',?,30,33,27,31.5,1e6,3.0,1.0)", [(20261028,), (20261029,)])
    tmp_db.commit()

    async def run():
        async with api() as client:
            a = await client.get("/api/kline/sh600519?days=5")
            b = await client.get("/api/kline/sh600519?days=5&adjust=adj")
            return a, b

    raw, adj = asyncio.run(run())
    assert raw.json()["adjust"] == "raw" and raw.json()["bars"][-1]["close"] == 10.5
    assert adj.json()["adjust"] == "adj" and adj.json()["bars"][-1]["close"] == 31.5


def test_pool_defaults_to_latest_date_and_sorts(api, tmp_db):
    import asyncio

    for day, rank, code in ((20261029, 2, "sh600000"), (20261029, 1, "sh600519"), (20261028, 1, "sz000001")):
        tmp_db.execute(
            "INSERT OR REPLACE INTO pool(trade_date, market, code, rank, score, factors, box_days,"
            " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean, pos_in_box)"
            " VALUES (?,?,?,?,80,'{}',40,10,9,10,9,9.5,1e6,0.5)", (day, "cn_main", code, rank))
    tmp_db.commit()

    async def run():
        async with api() as client:
            return (await client.get("/api/pool"), await client.get("/api/pool?market=cn_gem"),
                    await client.get("/authors/1"), await client.get("/auth-log"))

    res, empty, authors, auth_log = asyncio.run(run())
    # 前端自己的路由不能被服务端前缀吞掉：/authors/1 与 /auth-log 都不是 /auth
    assert authors.status_code in (200, 404) and auth_log.status_code in (200, 404)
    assert "application/json" not in (authors.headers.get("content-type") or "") or authors.status_code == 404
    body = res.json()
    assert body["trade_date"] == 20261029
    assert [r["code"] for r in body["rows"]] == ["sh600519", "sh600000"]   # 按 rank
    assert empty.json()["rows"] == []


def test_signals_filters(api, tmp_db):
    import asyncio

    seed_sig_context(tmp_db, code="sh600519")           # breakout P0
    seed_sig_context(tmp_db, code="sh600000", kind="breakdown")

    async def run():
        async with api() as client:
            return (await client.get("/api/signals")).json(), \
                   (await client.get("/api/signals?kind=breakdown")).json()

    all_rows, breakdown = asyncio.run(run())
    assert len(all_rows["rows"]) == 2 and len(breakdown["rows"]) == 1
    assert breakdown["rows"][0]["kind"] == "breakdown"


def test_sector_and_settings_endpoints(api, tmp_db):
    import asyncio

    tmp_db.execute("INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES ('sector','a1',?)",
                   (json.dumps({"as_of": "a1", "rows": [
                       {"code": "new_001", "name": "银行", "chg": 1.2, "members": 12},
                       {"code": "new_002", "name": "白酒", "chg": 3.4, "members": 8}]}),))
    tmp_db.execute("INSERT OR REPLACE INTO sector_member(sector, code, updated_at)"
                   " VALUES ('new_001','sh600519','x')")
    tmp_db.commit()

    async def run():
        async with api() as client:
            return (await client.get("/api/sector")).json(), \
                   (await client.get("/api/sector/new_001/members")).json(), \
                   (await client.get("/api/settings")).json()

    sector, members, settings = asyncio.run(run())
    assert sector["rows"][0]["chg"] == 3.4                  # 按涨幅倒序
    assert members["rows"][0]["code"] == "sh600519"
    assert isinstance(settings["strategy"], dict) and settings["strategy"]
    assert isinstance(settings["markets"], dict) and settings["markets"]


def test_hook_bad_token_403_no_change(api, tmp_db, read_at):
    import asyncio

    seed_sig_context(tmp_db)
    sid = tmp_db.execute("SELECT id FROM signals").fetchone()[0]

    async def run():
        async with api() as client:
            return await client.post(f"/api/hook/signals/{sid}/read?t=bad")

    res = asyncio.run(run())
    assert res.status_code == 403 and read_at(sid) is None


def test_hook_ok_sets_read_at(api, tmp_db, read_at):
    import asyncio

    seed_sig_context(tmp_db)
    sid, day = tmp_db.execute("SELECT id, trade_date FROM signals").fetchone()
    secret = ensure_secret(tmp_db)
    token = sign(secret, "read", sid, day_end_ts(day))

    async def run():
        async with api() as client:
            return await client.post(f"/api/hook/signals/{sid}/read?t={token}")

    assert asyncio.run(run()).status_code == 200
    assert read_at(sid) is not None


def test_hook_token_bound_to_signal_id(api, tmp_db, read_at):
    import asyncio

    seed_sig_context(tmp_db, code="sh600519")
    seed_sig_context(tmp_db, code="sh600000")
    a, b = [r[0] for r in tmp_db.execute("SELECT id FROM signals ORDER BY id")]
    secret = ensure_secret(tmp_db)
    token = sign(secret, "read", b, day_end_ts(20261030))

    async def run():
        async with api() as client:
            return await client.post(f"/api/hook/signals/{a}/read?t={token}")

    assert asyncio.run(run()).status_code == 403 and read_at(a) is None


def test_hook_yesterday_token_403(api, tmp_db, read_at):
    import asyncio

    seed_sig_context(tmp_db, trade_date=20261029)
    sid, day = tmp_db.execute("SELECT id, trade_date FROM signals").fetchone()
    secret = ensure_secret(tmp_db)
    token = sign(secret, "read", sid, day_end_ts(day))          # 昨日 23:59:59 已过期

    async def run():
        async with api() as client:
            return await client.post(f"/api/hook/signals/{sid}/read?t={token}")

    assert asyncio.run(run()).status_code == 403 and read_at(sid) is None


def test_watch_write_rejects_foreign_origin(api, CFG):
    import asyncio

    async def run():
        async with api() as client:
            bad = await client.post("/api/watch", json={"code": "sh600519"},
                                    headers={"Origin": "https://evil.example"})
            good = await client.post("/api/watch", json={"code": "sh600519"},
                                     headers={"Origin": f"https://{CFG.notify.public_host}"})
            listed = await client.get("/api/watch")
            deleted = await client.delete("/api/watch/sh600519",
                                          headers={"Origin": f"https://{CFG.notify.public_host}"})
            return bad, good, listed, deleted

    bad, good, listed, deleted = asyncio.run(run())
    assert bad.status_code == 400
    assert good.status_code == 200 and listed.json()["rows"][0]["code"] == "sh600519"
    assert deleted.json()["removed"] == 1


def test_same_origin_requires_exact_https_origin(api, CFG):
    """只认 `https://{public_host}` 精确 Origin：裸 host、http、带尾斜杠一律 400。"""
    import asyncio

    host = CFG.notify.public_host

    async def run():
        async with api() as client:
            return [await client.post("/api/watch", json={"code": "sh600519"},
                                      headers={"Origin": origin})
                    for origin in (f"http://{host}", host, f"https://{host}/", f"https://{host}")]

    statuses = [r.status_code for r in asyncio.run(run())]
    assert statuses == [400, 400, 400, 200]


def test_auth_localhost_only(api):
    """远程来源必须被拒；本机（SSH 端口转发）才给登录页。"""
    import asyncio

    app = api.app

    async def remote():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 443)),
                base_url="http://test") as client:
            return await client.get("/auth"), await client.post("/api/auth/start")

    blocked, started = asyncio.run(remote())
    assert blocked.status_code == 403 and started.status_code == 403

    async def local():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5555)),
                base_url="http://test") as client:
            page = await client.get("/auth")
            started = await client.post("/api/auth/start")
            return page, started

    page, started = asyncio.run(local())
    assert page.status_code == 200 and "扫码登录" in page.text
    token = started.json()["token"]

    async def with_token():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 443)),
                base_url="http://test") as client:
            return await client.get(f"/auth?token={token}")

    assert asyncio.run(with_token()).status_code == 200       # 一次性 token 可经隧道进来


def test_auth_status_requires_valid_token(api):
    """无 token / token 不对 → 403（轮询接口不再向任何人泄露登录状态）。"""
    import asyncio

    async def run():
        async with api() as client:
            return await client.get("/api/auth/status"), await client.get("/api/auth/status?token=bad")

    no_token, bad = asyncio.run(run())
    assert no_token.status_code == 403 and bad.status_code == 403


def test_auth_token_one_time_and_mints_short_session(api):
    """一次性 token 第二次 /auth → 403；首次消费换成 ≤5 分钟的 session token。"""
    import asyncio
    import re
    import time

    app = api.app

    async def start_local():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5555)),
                base_url="http://test") as client:
            return (await client.post("/api/auth/start")).json()["token"]

    token = asyncio.run(start_local())
    assert 0 < app.state.auth_tokens[token] - time.time() <= 300

    async def open_twice():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 443)),
                base_url="http://test") as client:
            return (await client.get(f"/auth?token={token}"),
                    await client.get(f"/auth?token={token}"))

    first, second = asyncio.run(open_twice())
    assert first.status_code == 200 and second.status_code == 403

    m = re.search(r'const TOKEN = "([^"]+)"', first.text)
    assert m is not None, "登录页必须把 session token 注入给轮询脚本"
    session_token = m.group(1)
    assert 0 < app.state.auth_sessions[session_token] - time.time() <= 300

    async def status(tok: str):
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 443)),
                base_url="http://test") as client:
            return await client.get(f"/api/auth/status?token={tok}")

    assert asyncio.run(status(session_token)).status_code == 200
    assert asyncio.run(status(token)).status_code == 403      # 一次性 token 已被消费


def test_status_shape(api, tmp_db):
    import asyncio

    async def run():
        async with api() as client:
            return (await client.get("/api/status")).json()

    body = asyncio.run(run())
    assert set(body) == {"version", "services", "counts", "pools", "upstream_1h", "notify_ok",
                         "notify_last_error", "disk_used_pct", "maintenance", "budget_exhausted",
                         # 首页「agent 最后活动 N 分钟前」：口径与 watchdog 规则 3 同一份
                         "agent_last_turn_at", "agent_turn_stale_min",
                         # 收盘后没有新回合是预期，前端不能只按分钟数标红
                         "in_session"}
    assert set(body["services"]) == {"api", "jobs", "agent"}
    assert body["services"]["jobs"]["last_beat_at"] is None
    assert set(body["upstream_1h"]) == {"pool", "sector", "market", "eod", "auction",
                                        "members", "backfill"}
    assert body["upstream_1h"]["pool"] == 0                  # 缺键补 0，不 500


def test_status_reads_calls_by_scope(api, tmp_db):
    import asyncio

    tmp_db.execute("INSERT OR REPLACE INTO service_heartbeat(service, last_beat_at, detail)"
                   " VALUES ('jobs', ?, ?)", (T0.isoformat(), json.dumps({"calls_by_scope": {"pool": 7}})))
    tmp_db.commit()

    async def run():
        async with api() as client:
            return (await client.get("/api/status")).json()

    body = asyncio.run(run())
    assert body["upstream_1h"]["pool"] == 7 and body["upstream_1h"]["market"] == 0
    assert body["services"]["jobs"]["stale_s"] == 0


def test_status_notify_down_then_recovers(api, tmp_db):
    import asyncio

    def insert_push(ts, kind, status, body=None, level="P2"):
        tmp_db.execute(
            "INSERT INTO push_log(ts, kind, level, seq_id, segment, status, trade_date, body)"
            " VALUES (?,?,?,?,1,?,20261030,?)",
            (ts, kind, level, f"x-{ts}-{kind}", status, body))
        tmp_db.commit()

    insert_push("2026-10-30T09:00:00+08:00", "push_channel_down", "failed", "ntfy 500")

    async def run():
        async with api() as client:
            down = (await client.get("/api/status")).json()
            return down

    down = asyncio.run(run())
    assert down["notify_ok"] is False and down["notify_last_error"] == "ntfy 500"

    insert_push("2026-10-30T09:30:00+08:00", "agent_note", "accepted")
    assert asyncio.run(run())["notify_ok"] is True


def test_confirm_result_does_not_count_as_success(api, tmp_db):
    import asyncio

    tmp_db.execute("INSERT INTO push_log(ts, kind, level, seq_id, segment, status, trade_date, body)"
                   " VALUES ('2026-10-30T09:00:00+08:00','push_channel_down','P2','pcd',1,'failed',20261030,'down')")
    tmp_db.execute("INSERT INTO push_log(ts, kind, level, seq_id, segment, status, trade_date)"
                   " VALUES ('2026-10-30T09:30:00+08:00','confirm_result','P2','cr',1,'accepted',20261030)")
    tmp_db.commit()

    async def run():
        async with api() as client:
            return (await client.get("/api/status")).json()

    assert asyncio.run(run())["notify_ok"] is False       # 确认回调从没经过 ntfy


def test_maintenance_flag(api, tmp_db):
    import asyncio

    tmp_db.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES ('maintenance.flag','1')")
    tmp_db.commit()

    async def run():
        async with api() as client:
            return (await client.get("/api/status")).json()

    assert asyncio.run(run())["maintenance"] is True


def test_healthz(api):
    """`/healthz` 免鉴权，且**带出版本号**：排障第一个问题就是「线上是哪一版」。

    版本号来自 `core/version.py` 这唯一来源，不是镜像 tag 也不是环境变量。
    """
    import asyncio

    from core.version import VERSION

    async def run():
        async with api() as client:
            return await client.get("/healthz")

    body = asyncio.run(run()).json()
    assert body["ok"] is True
    assert body["version"] == VERSION


def test_spa_fallback_excludes_server_routes(api):
    import asyncio

    async def run():
        async with api() as client:
            return (
                await client.get("/api/nope"),
                await client.get("/healthz"),
                await client.get("/auth"),
                await client.get("/api/auth/status"),
            )

    api_404, health, auth, auth_status = asyncio.run(run())
    # /api/nope 绝不能被 SPA 回落吞掉（前端拿到 200 + HTML 会把 JSON 解析炸掉）
    assert api_404.status_code == 404 and "text/html" not in api_404.headers.get("content-type", "")
    # /healthz 免鉴权即可，正文必须带版本（见 test_healthz）
    assert health.json()["ok"] is True and health.json()["version"]
    # /auth 是服务端登录页（不是 SPA 回落）：正文必须是「扫码登录」而不是 index.html
    assert auth.status_code == 200 and "扫码登录" in auth.text
    assert auth_status.status_code == 403      # status 受保护：无 token 不再泄露登录状态


def test_spa_prefixes_do_not_shadow_frontend_routes(api):
    """/authors/1、/auth-log、/healthzzz 是前端自己的路由，回落 index.html 而不是 404 JSON。

    **`web/dist` 是构建产物、不入库**（见 .gitignore）。没构建过时 `api/routes.py`
    的 SPA 回落按设计返回 404「前端产物不存在」——那是正确行为，不是要靠这条测试
    抓的缺陷。所以这里显式跳过，而不是断言 200：否则测试会依赖「本地碰巧构建过」，
    CI 上必红（第一次跑 CI 就是这么红的）。
    """
    import asyncio

    if not (Path("web/dist/index.html")).exists():
        pytest.skip("web/dist 未构建（make test-web 或镜像构建后才有）")

    async def run():
        async with api() as client:
            return {p: await client.get(p) for p in ("/authors/1", "/auth-log", "/healthzzz")}

    for path, resp in asyncio.run(run()).items():
        assert resp.status_code == 200 and "text/html" in resp.headers["content-type"], path
        assert "选股盯盘" in resp.text, path


def test_pool_factors_returned_as_object(api):
    """库里存 JSON 文本；原样返回会让前端 factorsOf 拿到字符串。"""
    import asyncio

    async def run():
        async with api() as client:
            return await client.get("/api/pool")

    body = asyncio.run(run()).json()
    for row in body["rows"]:
        assert row["factors"] is None or isinstance(row["factors"], dict)


def test_watch_pct_threshold_rejects_bool(api, CFG):
    """`True` 是 int 的子类，会被存成 1.0 当阈值。"""
    import asyncio

    async def run():
        async with api() as client:
            headers = {"Origin": f"https://{CFG.notify.public_host}"}
            return [await client.post("/api/watch", json=body, headers=headers)
                    for body in ({"code": "sh600519", "pct_threshold": True},
                                 {"code": "sh600519", "pct_threshold": "Infinity"},  # JSON 允许

                                 {"code": "sh600519", "pct_threshold": 3.5})]

    bad_bool, bad_inf, ok = asyncio.run(run())
    assert bad_bool.status_code == 422 and bad_inf.status_code == 422
    assert ok.status_code == 200


def test_status_in_session_excludes_lunch_break(api, tmp_db):
    """午休期间 `in_session` 必须是 False——与 agent 的唤醒规则同一口径。

    接口算午休的话，12:00 返回 true，前端就会按「交易时段内超 15 分钟没回合」
    把正常午休标红（实机验证过：11:29 最后一回合 → 12:00 显示红色）。
    """
    import asyncio

    async def run():
        async with api() as client:
            return (await client.get("/api/status")).json()

    body = asyncio.run(run())
    assert body["in_session"] is False, "午休（或非交易时段）被判成交易中"


# ---- 访问口令（D25 的鉴权终于接上了）----------------------------------


def test_protected_route_requires_password(api, tmp_db, monkeypatch):
    """设置口令后，**未带凭据**访问业务接口必须 401。

    之前 `/auth` 那套 session 只用在 `/api/auth/status` 自己身上，
    所有业务接口（含 `/api/status`、`/api/pool`、`/api/chat`）**完全裸奔**，
    外网任何人可读。这里把口令真正挂到路由上。
    """
    import asyncio

    monkeypatch.setenv("SS_WEB_USER", "u")
    monkeypatch.setenv("SS_WEB_PASS", "p")

    async def run():
        async with api() as client:
            return await client.get("/api/status")

    r = asyncio.run(run())
    assert r.status_code == 401, f"未带凭据竟返回 {r.status_code}"
    assert r.headers.get("www-authenticate", "").lower().startswith("basic")


def test_protected_route_accepts_correct_password(api, tmp_db, monkeypatch):
    import asyncio
    import base64

    monkeypatch.setenv("SS_WEB_USER", "u")
    monkeypatch.setenv("SS_WEB_PASS", "p")
    cred = base64.b64encode(b"u:p").decode()

    async def run():
        async with api() as client:
            return await client.get("/api/status", headers={"Authorization": f"Basic {cred}"})

    assert asyncio.run(run()).status_code == 200


def test_wrong_password_rejected(api, tmp_db, monkeypatch):
    import asyncio
    import base64

    monkeypatch.setenv("SS_WEB_USER", "u")
    monkeypatch.setenv("SS_WEB_PASS", "p")
    cred = base64.b64encode(b"u:wrong").decode()

    async def run():
        async with api() as client:
            return await client.get("/api/status", headers={"Authorization": f"Basic {cred}"})

    assert asyncio.run(run()).status_code == 401


def test_healthz_stays_open_for_docker(api, tmp_db, monkeypatch):
    """`/healthz` 必须免鉴权：docker healthcheck 不带凭据，挡了会被判 unhealthy。"""
    import asyncio

    monkeypatch.setenv("SS_WEB_USER", "u")
    monkeypatch.setenv("SS_WEB_PASS", "p")

    async def run():
        async with api() as client:
            return await client.get("/healthz")

    assert asyncio.run(run()).status_code == 200


def test_no_password_configured_keeps_open(api, tmp_db, monkeypatch):
    """没配口令时保持开放（本机开发/内网），不把自己锁在门外。"""
    import asyncio

    monkeypatch.delenv("SS_WEB_PASS", raising=False)

    async def run():
        async with api() as client:
            return await client.get("/api/status")

    assert asyncio.run(run()).status_code == 200


def test_api_disk_used_pct_matches_df_on_ext4(monkeypatch):
    """**分子分母都要对**，且必须与 `df` 同口径。

    ext4 给 root 保留 5%。踩过两次，都在**真实 ext4 才暴露**：
     ① 分子写成 `blocks-bavail` → 保留块被算成"已用"，报 90.1%（df 84.6%）
     ② 分母写成 `blocks` → 保留块进分母，报 85.5%
    macOS 的 HFS/APFS 没有保留块（`f_bfree == f_bavail`），
    三种写法在本机结果相同——所以必须**造出保留块**来测。
    """
    import os as _os

    class FakeStat:
        # 100G 盘：已用 80G、非 root 可用 10G、保留 10G
        f_frsize = 1
        f_blocks = 100 * 2**30
        f_bfree = 20 * 2**30          # 含保留块
        f_bavail = 10 * 2**30         # 不含保留块
    monkeypatch.setattr(_os, "statvfs", lambda p: FakeStat())

    from api.routes import _disk_used_pct
    got = _disk_used_pct("data")
    assert abs(got - 88.9) < 0.2, f"应该是 used/(used+free)=88.9%，实际 {got:.1f}%"
    # 该 fixture 下错误的写法只有两种，各自对应一个真会出现的错法：
    assert abs(got - 90.0) > 1, f"分子用了 blocks-bavail（保留块算成已用）：{got:.1f}%"
    assert abs(got - 80.0) > 5, f"分母用了 blocks（保留块进分母）：{got:.1f}%"
    # 读不到时必须是 None，不能是 0（0 会让所有告警阈值失效）
    monkeypatch.setattr(_os, "statvfs", lambda p: (_ for _ in ()).throw(OSError("boom")))
    assert _disk_used_pct("data") is None
