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
    for path in ("/api/hook/signals/1/read", "/api/hook/agent/1/confirm"):
        r = _get(api, path)
        # **不能只断言"不是 401"**：500 也能过。这里要的是"中间件放行了，
        # 由路由自己拒绝"——403（验签失败）或 404（对象不存在）都证明请求到达了路由。
        # 401 = 被中间件拦死；5xx = 路由炸了。
        assert r.status_code in (403, 404), (
            f"{path} 期望 403/404（路由自己处理），实际 {r.status_code}："
            "401 说明被中间件拦死，5xx 说明路由崩了")


def test_hook_prefix_is_not_a_general_bypass(api, tmp_db, passwd):
    """放行 `/api/hook/` 不能顺带放行别的路径。"""
    assert _get(api, "/api/status").status_code == 401
    assert _get(api, "/api/hookx/status").status_code == 401
    # 带 `..` 的路径不能借前缀放行（不把安全性建立在"路由器不规范化路径"上）。
    # **必须发原始 ASGI 请求**：httpx 会在客户端把 `..` 规范化掉，
    # 用普通 client 测这条等于测了个不存在的路径（变异不会变红）。
    assert _raw_get(api, "/api/hook/../api/status") == 401
    assert _raw_get(api, "/healthz/../api/status") == 401


def test_every_hook_route_verifies_its_own_signature():
    """`/api/hook/` 前缀在中间件里是放行的——**每条路由都必须自己验签**。

    放行靠的是「路由内部 `verify()`」这个前提。将来有人在这里加一条不验签的
    路由，它就静默变成公开接口——中间件不会再挡。这条守卫按 AST 检查
    `verify(` 调用是否出现在每个 hook 路由的函数体里。
    """
    import ast
    from pathlib import Path

    src = Path("api/routes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    checked = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not dec.args:
                continue
            arg = dec.args[0]
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                continue
            if not arg.value.startswith("/api/hook/"):
                continue
            calls = {n.func.id for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            attrs = {n.func.attr for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            assert "verify" in calls | attrs, (
                f"{arg.value} 在 /api/hook/ 下但没有调用 verify()："
                "中间件对该前缀放行，它就变成公开接口了")
            checked.append(arg.value)

    assert checked, "没找到任何 /api/hook/ 路由，守卫失效"


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
