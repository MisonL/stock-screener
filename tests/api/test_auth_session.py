"""推送链接免密登录（会话票据 + Cookie）。

**要解决的问题**：手机浏览器不总记得 HTTP Basic 凭据，从 ntfy 点开一条推送
就会被原生登录框拦一次，每次都要重新输口令。

**设计边界（这是需求本身，不是妥协）**：
  - 从通知点进来的链接带签名票据 → 直接进，**不弹框**；
  - 裸敲域名、没有票据也没有 Cookie → 仍然要口令。

票据是 `hmac_sha256(secret, "session:0:<exp>")`，与 `read` / `confirm` 三类
票据互不通用（purpose 进了签名原文）。
"""
from __future__ import annotations

import asyncio
import base64
import sqlite3
import time

import httpx

import pytest

from api import auth
from notify.hmac_sign import sign


@pytest.fixture
def passwd(monkeypatch):
    monkeypatch.setenv("SS_WEB_USER", "u")
    monkeypatch.setenv("SS_WEB_PASS", "p")


@pytest.fixture
def secret(tmp_db):
    from notify.hmac_sign import ensure_secret

    return ensure_secret(tmp_db)


def _cred(user: str, pw: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def _post(api, path: str, headers=None, json=None):
    async def run():
        async with api() as client:
            return await client.post(path, headers=headers or {},
                                     json=json, follow_redirects=False)

    return asyncio.run(run())


def _get(api, path: str = "/api/status", headers=None):
    async def run():
        async with api() as client:
            return await client.get(path, headers=headers or {}, follow_redirects=False)

    return asyncio.run(run())


def _ticket(secret, days: int = 30) -> tuple[str, int]:
    exp = int(time.time() + days * 86400)
    return sign(secret, "session", 0, exp), exp


def _raw_get(api, path: str) -> int:
    """发**未经规范化的**原始路径，返回状态码。

    httpx 会在客户端做 `..` 归一，所以这类"前缀放行是否被路径穿越绕过"的问题
    只能用 ASGI 直接调，否则测试永远绿。
    """
    app = api()
    # fixture 返回的是 AsyncClient 工厂；真正的 app 挂在 transport 上
    transport = getattr(app, "_transport", None)
    asgi_app = getattr(transport, "app", None)
    assert asgi_app is not None, "拿不到 ASGI app，无法发原始请求"

    captured = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        captured.update(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": [(b"host", b"test")],
        "scheme": "https", "client": ("192.0.2.1", 1234), "server": ("test", 443),
    }
    asyncio.run(asgi_app(scope, receive, send))
    return captured.get("status", 0)


def test_unsigned_request_still_asks_for_password(api, tmp_db, passwd):
    """裸敲域名必须仍然要口令——否则等于把鉴权去掉了。"""
    assert _get(api).status_code == 401


def test_link_with_ticket_logs_in_without_prompt(api, tmp_db, passwd, secret):
    """从通知点开的链接（带票据）直接进，不弹框。"""
    token, exp = _ticket(secret)
    r = _get(api, f"/api/status?t={token}&exp={exp}")
    assert r.status_code == 302, f"带票据的链接没放行：{r.status_code}"
    # 会把票据换成 Cookie，并把 t/exp 从地址栏抹掉
    assert auth.SESSION_COOKIE in r.headers.get("set-cookie", "")


def test_ticket_is_stripped_from_the_redirect_target(api, tmp_db, passwd, secret):
    """票据不能留在地址栏/历史/Referer 里。"""
    token, exp = _ticket(secret)
    r = _get(api, f"/signals?t={token}&exp={exp}&tab=k")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "t=" not in loc and "exp=" not in loc, f"票据还在跳转目标里：{loc}"
    assert "tab=k" in loc, "业务查询参数被一起抹掉了"


def test_expired_ticket_is_rejected(api, tmp_db, passwd, secret):
    """过期票据不放行（`verify` 只看签名，过期必须单独判）。"""
    exp = int(time.time() - 10)
    token = sign(secret, "session", 0, exp)
    assert _get(api, f"/api/status?t={token}&exp={exp}").status_code == 401


def test_ticket_from_another_purpose_does_not_work(api, tmp_db, passwd, secret):
    """`read` 票据不能当访问票据用——同一 id 签不出同一张票。"""
    exp = int(time.time() + 3600)
    read_token = sign(secret, "read", 0, exp)
    assert _get(api, f"/api/status?t={read_token}&exp={exp}").status_code == 401


def test_forged_ticket_is_rejected(api, tmp_db, passwd):
    """拿别的密钥签的票必须拒。"""
    exp = int(time.time() + 3600)
    forged = sign(b"y" * 32, "session", 0, exp)
    assert _get(api, f"/api/status?t={forged}&exp={exp}").status_code == 401


def test_basic_login_sets_cookie_so_it_asks_only_once(api, tmp_db, passwd, secret):
    """手动输过一次口令后发 Cookie，之后不再弹原生框。

    这正是用户抱怨的场景：认证一次、下次还弹。发了 Cookie 就不会了。
    """
    r = _get(api, headers=_cred("u", "p"))
    assert r.status_code == 200
    cookies = r.headers.get("set-cookie", "")
    assert auth.SESSION_COOKIE in cookies, "Basic 登录成功后没有发会话 Cookie"
    assert "HttpOnly" in cookies, "Cookie 少了 HttpOnly，XSS 能读走"


def test_session_ok_helpers():
    """纯函数层：过期、空票、错误 purpose 都判否。"""
    sec = b"k" * 32
    exp = int(time.time() + 100)
    assert auth.session_ok(sec, auth.issue_session(sec, exp), exp, time.time()) is True
    assert auth.session_ok(sec, "x", exp, time.time()) is False
    assert auth.session_ok(sec, "", exp, time.time()) is False
    assert auth.session_ok(sec, auth.issue_session(sec, exp), exp, exp + 1) is False


def test_expired_ticket_does_not_count_as_password_failure(api, tmp_db, passwd, secret):
    """过期票据点几次不能把人锁在门外。

    原先过期票据落到 Basic 分支会被 `_note_failure` 计数，点 10 条旧通知
    就触发 15 分钟封锁——用户一次口令都没输过。而且真正的口令限流不需要
    靠这个来保护（猜口令必须带 Authorization）。
    """
    exp = int(time.time() - 10)
    stale = sign(secret, "session", 0, exp)
    for _ in range(auth.FAIL_MAX):
        assert _get(api, f"/api/status?t={stale}&exp={exp}").status_code == 401
    # 仍然能用正确口令登录（没被锁）
    assert _get(api, headers=_cred("u", "p")).status_code == 200


def test_ticket_plus_wrong_password_still_counts(api, tmp_db, passwd, secret):
    """带票据同时带错误口令：必须照常计数，否则限流能被绕过。"""
    exp = int(time.time() + 3600)
    good = sign(secret, "session", 0, exp)
    for i in range(auth.FAIL_MAX):
        # 前几次 401；打满阈值后立即 429——说明错误口令确实在计数
        assert _get(api, f"/api/status?t={good}&exp={exp}",
                    headers=_cred("u", f"bad{i}")).status_code in (401, 429)
    blocked = _get(api, headers=_cred("u", "p"))
    assert blocked.status_code == 429, "带票据的错误口令没有计入限流"


def _browser_flow(api, path: str):
    """**同一个 client** 连发两次，**中间不重定向**，手动跟随 `Location`。

    浏览器收到 302 会自己带上刚拿到的 Cookie。测试里必须模拟这一点——
    直接让 httpx 自动跟随会把 Set-Cookie 丢掉（`ASGITransport` 加默认
    client 时 cookie jar 行为不一致），于是"免密没生效"这种真缺陷会被
    "浏览器会处理"的假设掩盖掉。

    `base_url` 必须是 https：Cookie 带 `Secure`，http 下 httpx 不会回传。
    """
    async def run():
        # 用 `https://` 的 client：Cookie 带 `Secure`，http 下 httpx 不会回传，
        # 测试就会得到"免密没生效"的假失败（或反过来掩盖真问题）。
        client = api()
        client.base_url = httpx.URL("https://test")
        async with client:
            first = await client.get(path)
            if first.headers.get("location"):
                second = await client.get(first.headers["location"])
            else:
                second = first
            return first, second

    return asyncio.run(run())


def test_ticket_login_then_next_request_succeeds(api, tmp_db, passwd, secret):
    """**端到端**：点链接 → 302 → 跟着跳转的那次请求必须 200。

    上一版就在这里断了：票据分支只写 `ss_session`，而 Cookie 校验还要
    `ss_session_exp`；跳转后的请求找不到到期时刻 → 又弹 Basic。
    """
    token, exp = _ticket(secret)
    first, second = _browser_flow(api, f"/api/status?t={token}&exp={exp}")
    assert first.status_code == 302
    assert second.status_code == 200, (
        f"跟随 302 之后仍然要口令（{second.status_code}）：免密链路没生效")


def test_ticket_sets_both_cookies(api, tmp_db, passwd, secret):
    """两枚 Cookie 必须一起写，且有效期为**票据自己的** exp。"""
    token, exp = _ticket(secret)
    r = _get(api, f"/api/status?t={token}&exp={exp}")
    cookies = r.headers.get_list("set-cookie")
    joined = "; ".join(cookies)
    assert auth.SESSION_COOKIE in joined
    assert auth.SESSION_COOKIE + "_exp" in joined, "没有写 ss_session_exp，下次请求会弹框"
    for c in cookies:
        assert "HttpOnly" in c, f"Cookie 少了 HttpOnly：{c}"
        assert "Secure" in c, f"Cookie 少了 Secure：{c}"
        assert "SameSite=lax" in c.replace("samesite=lax", "SameSite=lax")


def test_hook_buttons_work_without_browser_credentials(api, tmp_db, passwd):
    """ntfy 的「标记已读」「确认执行」不带浏览器凭据，必须能过中间件。

    这两个按钮是 ntfy **服务端直接 POST**，没有浏览器、没有 Cookie、也不会
    弹口令框。全局中间件按 Basic 拦它 → 401，路由自己的 HMAC 根本没机会跑，
    于是"标记已读"从来没能生效（生产库证据：94 条信号 `read_at` 全为空）。
    放行是安全的：两个路由都强制 `verify()` + 过期判断。
    """
    # **必须用 POST**：这两条路由都是 POST。用 GET 会打到 SPA 兜底返回 404
    # `{"detail":"不是前端路由"}`，根本没走到 hook 路由——旧版本断言
    # `in (403, 404)` 恰好放过了它，是假绿。
    for path in ("/api/hook/signals/1/read", "/api/hook/agent/1/confirm"):
        r = _post(api, path)
        # 403 = 路由验签失败（对象存在）；404 = 路由先查对象、查不到（测试库里
        # 没有这一行）。两者都证明**请求到达了路由**。关键是不能是 401。
        assert r.status_code in (403, 404), (
            f"{path} 期望 403/404（路由自己处理），实际 {r.status_code}："
            "401 = 被中间件拦死")
        assert r.json().get("detail") != "需要口令才能访问", (
            f"{path} 返回了中间件的口令提示，说明被拦在中间件")


def test_hook_prefix_is_not_a_general_bypass(api, tmp_db, passwd):
    """放行 `/api/hook/` 不能顺带放行别的路径。"""
    assert _get(api, "/api/status").status_code == 401
    assert _get(api, "/api/hookx/status").status_code == 401
    # 带 `..` 的路径不能借前缀放行（不把安全性建立在"路由器不规范化路径"上）。
    # **必须发原始 ASGI 请求**：httpx 会在客户端把 `..` 规范化掉，
    # 用普通 client 测这条等于测了个不存在的路径（变异不会变红）。
    assert _raw_get(api, "/api/hook/../api/status") == 401
    assert _raw_get(api, "/healthz/../api/status") == 401


def test_every_hook_route_enforces_its_own_signature(api, tmp_db, passwd, secret):
    """`/api/hook/` 前缀在中间件里是放行的——**每条路由都必须自己验签**。

    这条守卫**在运行时真发请求**，不用字符串/AST 检查。AST 版本只看函数体里
    有没有出现 `verify(`，三种写法都会漏：校验挪进辅助函数、被 `try/except` 包住、
    或者用 FastAPI 惯用的 `Depends(require_hook_token)`——最后一种最现实，
    而 AST 检查会逼着开发者绕开它。

    正反两面都要验：
      - **负面**：无票请求不能返回 401（那说明中间件拦死了，按钮点不动）；
        也不能返回 200（那说明路由根本没验签，成了公开接口）。
      - **正面**：带**合法**票据时不能是 403/401（说明验签真的在工作）。
    """
    from api.routes import create_app  # noqa: F401  —— 只要它的路由表

    # **先把数据备好**：路由是先查对象再验签的，库是空的就会直接 404，
    # 请求根本走不到 `verify()`——负面断言会变成空转（变异也不会红）。
    trade_date = 20261113
    tmp_db.execute(
        "INSERT INTO signals(ts, trade_date, market, code, kind, level, event_id, seq, payload) "
        "VALUES ('2026-11-13T09:35:00', ?, 'cn_main', 'sh600519', 'breakout', 'P1', 0, 1, '{}')",
        (trade_date,))
    tmp_db.commit()
    sid = tmp_db.execute("SELECT max(id) FROM signals").fetchone()[0]

    app = api()._transport.app
    hook_routes = [r for r in app.routes
                   if getattr(r, "path", "").startswith("/api/hook/")]
    assert hook_routes, "没有 /api/hook/ 路由，守卫失效"

    for route in hook_routes:
        # 把 {param} 换成 1；无票 POST
        concrete = route.path.replace("{signal_id}", "1").replace("{turn_id}", "1")
        codes = {m: _post(api, concrete) for m in (route.methods or {"POST"})}
        for method, r in codes.items():
            assert r.status_code != 200, (
                f"{route.path} [{method}] 无票请求返回 200：中间件对该前缀放行，"
                "而路由没有验签，等于公开接口")
            assert r.status_code != 401, (
                f"{route.path} [{method}] 被中间件拦成 401：ntfy 按钮（服务端直接 "
                "POST、无浏览器凭据）永远点不动")

    # 正面：read 路由带一张合法票据必须能通过验签
    from notify.hmac_sign import day_end_ts

    exp = day_end_ts(trade_date)
    token = sign(secret, "read", sid, exp)
    ok = _post(api, f"/api/hook/signals/{sid}/read?t={token}")
    assert ok.status_code == 200, (
        f"带合法票据仍被拒（{ok.status_code}）：验签或数据前置条件有问题")


def test_blocked_ip_with_valid_session_still_gets_in(api, tmp_db, passwd, secret):
    """被封的 IP 拿着有效会话必须能进。

    限流是为了挡**猜口令**。会话 Cookie / 票据本身已经是「认证通过」的证明，
    再拿它去挡等于：手机上口令手滑几次 → 被锁 15 分钟 → 连点通知链接也进不去。
    而用户当初要的就是「点一下就开」。
    """
    token, exp = _ticket(secret)
    # 先把本机 IP 打满失败次数
    for i in range(auth.FAIL_MAX):
        _get(api, headers=_cred("u", f"bad{i}"))
    assert _get(api, headers=_cred("u", "p")).status_code == 429, "前置条件：应该已被封锁"
    # 但有效会话仍然能进
    r = _get(api, "/api/status",
             headers={"Cookie": f"{auth.SESSION_COOKIE}={token}; "
                                f"{auth.SESSION_COOKIE}_exp={exp}"})
    assert r.status_code == 200, "有效会话被口令限流连累，锁在门外了"


def test_one_request_counts_exactly_one_failure(api, tmp_db, passwd):
    """**一次请求只记一次失败**，无论它带不带垃圾票据。

    ③-0（带口令先校验）与 ⑤（Basic 分支）是两段代码。如果两段都计数，
    每次请求记 2 次，10 次配额只够 5 次猜测——攻击者把候选口令塞进 `?t=`
    就能把窗口内的猜测次数翻倍。

    **必须直接量计数**：用状态码推断测不出来（密码正确时两段都不计数，
    双计数不改变状态码；密码错误时 ③-0 就返回了，根本到不了 ⑤）。

    **注意本测试的边界**：在当前结构下，"双计数"这个具体变异是测不出来的
    （③-0 返回后不会继续执行）。它守的是"每请求恰好记 1 次"这个可观测性质，
    而不是某段代码是否存在——后者在密码正确的路径上根本不可观测。
    """
    for suffix in ("?t=garbage&exp=1", ""):
        key = f"203.0.113.{80 + len(suffix)}"
        headers = {**_cred("u", "bad"), "CF-Connecting-IP": key}
        assert _get(api, "/api/status" + suffix, headers=headers).status_code == 401
        counted = auth._fails.get(key, (0, 0, 0))[1]
        assert counted == 1, (
            f"一次错误口令请求记了 {counted} 次失败（应为 1），suffix={suffix!r}")


def test_browser_first_paint_does_not_lock_the_user_out(api, tmp_db, passwd, secret):
    """新浏览器首屏的十几个 401 不能把人锁在外面。

    网页首屏并行请求十几个 `/api/*`。用户换手机、清 Cookie、或从通知点进一个
    没有票据的页面，就会连收十几次 401。如果这些匿名 401 用"猜口令"那档
    （10 次）计数，打开首页就被锁 15 分钟——正是免密功能要解决的问题本身。
    """
    for _ in range(15):
        assert _get(api).status_code == 401
    # 首屏之后，手动输正确口令必须能进
    assert _get(api, headers=_cred("u", "p")).status_code == 200, \
        "首屏的匿名 401 把用户锁在外面了"


def test_ticket_is_scrubbed_even_when_already_logged_in(api, tmp_db, passwd, secret):
    """已经登录时，URL 里的票据也必须抹掉。

    否则手机浏览器会把带票据的地址留在地址栏和历史里——票据还能用 30 天。
    """
    _first, _second = _browser_flow(api, f"/signals?t={_ticket(secret)[0]}&exp={_ticket(secret)[1]}")
    token, exp = _ticket(secret)

    async def run():
        async with api() as client:
            client.base_url = httpx.URL("https://test")
            # 先登录拿 Cookie
            await client.get(f"/signals?t={token}&exp={exp}")
            # 再点一条带票据的链接
            return await client.get(f"/signals?t={token}&exp={exp}")

    r = asyncio.run(run())
    assert r.status_code == 302, (
        f"已登录时带票据的请求返回 {r.status_code}，地址栏里的票据不会被抹掉")
    assert "t=" not in (r.headers.get("location") or "")


def test_secret_load_failure_is_logged_as_error(monkeypatch, caplog):
    """取不到签名密钥必须打 ERROR，不能只是 warning。

    密钥取不到时，所有在飞的免密链接和 ntfy 按钮会同时失效，用户看到的是
    "点了没反应"。`/healthz` 不碰鉴权、报不出这个错，所以日志/计数是唯一线索。
    """
    import logging

    def boom():
        raise sqlite3.OperationalError("database is locked")

    auth._secret_failures = 0
    with caplog.at_level(logging.ERROR):
        got = auth._load_secret(boom)
    assert got is None, "取不到密钥时不应该返回任何值（无法验签）"
    assert auth._secret_failures == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records), "没有打 ERROR"


def test_chat_requires_same_origin(api, tmp_db, passwd):
    """`/api/chat` 每调一次都花 LLM 预算，必须有同源校验。

    `/api/watch` 早就有了，`/api/chat` 原先没有。`SameSite=Lax` 确实挡住了
    跨站 POST，但那是浏览器行为——服务端自己判一次 Origin 才不依赖别人。
    """
    # **必须带口令**：不带就停在中间件 401，走不到同源校验（那样测的是鉴权）
    cred = _cred("u", "p")
    body = {"text": "hi"}
    bad = _post(api, "/api/chat", headers={**cred, "Origin": "https://evil.example"},
                json=body)
    assert bad.status_code == 400, f"跨源 chat 没被拒：{bad.status_code}"

    # 同源：Origin 必须等于 https://<public_host>。这里只验"通过校验"，
    # 不关心 LLM 回答（没配 key 时它自己会失败，那是另一层的事）
    from core.config import load_config
    from pathlib import Path

    host = str(load_config(Path("config")).notify.public_host).strip()
    same = _post(api, "/api/chat", headers={**cred, "Origin": f"https://{host}"},
                 json=body)
    assert same.status_code != 400, "同源请求被误拒"
