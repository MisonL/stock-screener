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
        assert _get(api, f"/api/status?t={good}&exp={exp}",
                    headers=_cred("u", f"bad{i}")).status_code in (401, 302)
    blocked = _get(api, headers=_cred("u", "p"))
    assert blocked.status_code == 429, "带票据的错误口令没有计入限流"
