"""网页访问口令（HTTP Basic）。

**为什么需要**：`/auth` 那套 session 登录只用在 `/api/auth/status` 自己身上，
所有业务接口（`/api/status`、`/api/pool`、`/api/chat`、`/api/hook/*`）此前
**完全裸奔**——外网任何人 `curl https://stock.<域名>/api/status` 就能读到
服务状态、池子、信号，甚至调写接口。

**为什么用 Basic 而不是自建登录页**：浏览器原生弹框即可，不用写前端代码。

**但手机浏览器不总记得 Basic 凭据**——从 ntfy 点开链接会一次次弹原生登录框。
所以加了一层 **会话票据 + Cookie**：

1. 推送里的每个链接都带 `?t=<签名>&exp=<到期>`，点开即自动登录（**不弹框**）；
2. 浏览器随后拿到 `HttpOnly; Secure; SameSite=Lax` 的会话 Cookie，
   之后刷新、换页面都不再问；
3. 直接输入域名（没有票据、也没有 Cookie）仍然走 Basic——这正是要的：
   **从通知点进来的免密，裸敲网址的照样要口令**；
4. Basic 登录成功后也发同一个 Cookie，所以手动输过一次之后就不再弹了。

票据是 `hmac_sha256(secret, "session:0:<exp>")`，密钥复用 `app_secret.hmac_key`。
`purpose` 与 `read`/`confirm` 分开，三类票据互不通用。
链接里带凭据的代价是它会出现在访问日志里——因此**过期时间有限、且只能读网页**；
要立刻让所有在飞的链接失效，删掉 `app_secret.hmac_key` 那一行即可。

**为什么口令放环境变量**：`config/` 是挂载进容器的、会进 Git；口令不该进版本库。
不配 `SS_WEB_PASS` 时**保持开放**（本机开发、内网自用），避免把自己锁在门外。
"""
from __future__ import annotations

import base64
import hmac
import logging
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse

_log = logging.getLogger("api.auth")

# ---- 会话票据（推送链接免密）--------------------------------------------
# Cookie 名。`__Host-` 前缀要求 Secure + Path=/ + 无 Domain，浏览器会强制检查；
# 这里不用它是因为自签/本地调试时没有 https 会让浏览器直接丢弃 Cookie。
SESSION_COOKIE = "ss_session"
# 有效期：票据与 Cookie 同一个到期时刻。短了手机上一天点几次就要重新登录，
# 长了等于把口令写进 URL 长期有效。30 天是折中，可用环境变量改。
SESSION_DAYS_ENV = "SS_SESSION_DAYS"
DEFAULT_SESSION_DAYS = 30

def _load_secret(conn_factory) -> bytes | None:
    """取签名密钥。**不关连接**：工厂可能返回共享连接。

    **故意不缓存**：`ensure_secret` 只是一次主键 SELECT（`app_secret` 几行），
    而缓存会让「删掉 `app_secret.hmac_key` 让在飞票据立刻失效」变成一个
    最长要等一分钟的操作——安全开关不该有延迟。缓存还曾让测试互相污染
    （单跑通过、合跑失败），这种隐蔽性不值得那点查询开销换。
    """
    try:
        from notify.hmac_sign import ensure_secret

        return ensure_secret(conn_factory())
    except Exception:                        # noqa: BLE001 —— 取不到就退化为仅 Basic
        _log.warning("取不到签名密钥，退化为仅 Basic 鉴权", exc_info=True)
        return None

USER_ENV = "SS_WEB_USER"
PASS_ENV = "SS_WEB_PASS"
DEFAULT_USER = "admin"

# **只放行不通外网的端点**：docker healthcheck 不带凭据，挡了会被判 unhealthy
# 并触发无限重启；`/auth` 本身是登录入口，挡了就没法登录。
OPEN_PATHS = ("/healthz", "/auth")

# ---- 失败限流 -----------------------------------------------------------
# 没有限流时，外网可以对 `/api/...` 无限次猜口令（实测连打 10 次全是 401、
# 毫无延迟）。口令又是人起的一串字，被慢慢穷举只是时间问题；一旦猜中，
# `/api/chat` 会花 LLM 预算、`/api/hook/*` 能改状态。
#
# 阈值故意宽松：正常用户输错一两次不该被锁。10 次 / 5 分钟已经远超手滑。
FAIL_WINDOW_S = 300          # 统计窗口
FAIL_MAX = 10                # 窗口内允许的失败次数
BLOCK_S = 900                # 触发后锁多久

# 跟踪的 IP 上限。**必须有上限**：`CF-Connecting-IP` 是客户端可设置的头，
# 每次换一个、各失败一次，就能让这个 dict 无限增长（100 万个 IP ≈ 100MB），
# 而每个只失败一次连封锁都不会触发——看起来「什么都没发生」。
MAX_TRACKED = 10_000

# ip -> (窗口起点, 失败次数, 封锁到何时)。**进程内内存**：
# 重启即清空——这是有意的，否则一次内存损坏/PID 复用会把正常用户永久挡在门外。
_fails: dict[str, tuple[float, int, float]] = {}
# 上次清扫时刻：批量清理不能每个请求都全表扫一遍（那是另一种拒绝服务）
_last_sweep = 0.0
SWEEP_EVERY_S = 60.0


def _credentials() -> tuple[str, str] | None:
    """返回 (user, pass)；未配置口令时返回 None（保持开放）。"""
    pw = (os.environ.get(PASS_ENV) or "").strip()
    if not pw:
        return None
    return (os.environ.get(USER_ENV) or DEFAULT_USER).strip(), pw


def _session_days() -> int:
    raw = (os.environ.get(SESSION_DAYS_ENV) or "").strip()
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_SESSION_DAYS
    return days if days > 0 else DEFAULT_SESSION_DAYS


def issue_session(secret: bytes, exp_ts: int) -> str:
    """签一张网页访问票据。`ref_id` 固定 0：本系统只有单一口令，没有多用户。"""
    from notify.hmac_sign import sign

    return sign(secret, "session", 0, exp_ts)


def session_ok(secret: bytes, token: str, exp_ts: int, now: float) -> bool:
    """票据有效且未过期。**过期判断在这里做**：`verify` 只看签名。"""
    from notify.hmac_sign import verify

    if not token or exp_ts <= 0 or now >= exp_ts:
        return False
    return verify(secret, "session", 0, exp_ts, token)


def _cookie_value(request: Request) -> str:
    return (request.cookies.get(SESSION_COOKIE) or "").strip()


def _set_session_cookie(response, token: str, days: int):
    """`HttpOnly` 防 XSS 读走；`Secure` 只在 https 下发送；`Lax` 兼顾跳转链接。"""
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=days * 24 * 3600, httponly=True, secure=True, samesite="lax", path="/",
    )
    # 链接里带过凭据：别让 Referer 把它泄给站外
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def _parse_basic(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(" ", 1)[1].strip(), validate=True)
        user, _, pw = raw.decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    return user, pw


def _challenge() -> JSONResponse:
    return JSONResponse(
        {"detail": "需要口令才能访问"},
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="stock-screener", charset="UTF-8"'},
    )


def _client_key(request: Request) -> str:
    """取客户端 IP。**必须优先用 Cloudflare 的头**。

    请求经过 cloudflared 才到 api，`request.client.host` 永远是隧道容器的地址。
    用它当键等于全世界的失败共用一个桶——攻击者随便试 10 次就能把正常用户锁死。
    """
    for header in ("cf-connecting-ip", "x-forwarded-for"):
        raw = request.headers.get(header)
        if raw:
            # X-Forwarded-For 可能是 "client, proxy1, proxy2"，第一跳才是客户端
            return raw.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _blocked_until(key: str, now: float) -> float:
    """返回封锁截止时刻；未封锁返回 0，并顺手清掉过期记录。"""
    entry = _fails.get(key)
    if entry is None:
        return 0.0
    window_start, count, blocked = entry
    if blocked > now:
        return blocked
    if now - window_start > FAIL_WINDOW_S:
        _fails.pop(key, None)          # 窗口已过，清零（也防内存无限增长）
        return 0.0
    return 0.0


def _sweep(now: float) -> None:
    """批量清掉过期条目。

    只靠「该 key 再次被访问时清理」是不够的：只出现一次的 key 永不被再次访问，
    会一直留着。所以按 `SWEEP_EVERY_S` 定期整体扫一遍，并在超上限时强制扫。
    仍在窗口内（或仍在封锁中）的条目必须保留——否则清扫等于帮攻击者重置计数。
    """
    global _last_sweep
    if now - _last_sweep < SWEEP_EVERY_S and len(_fails) <= MAX_TRACKED:
        return
    _last_sweep = now
    for k, (window_start, _count, blocked) in list(_fails.items()):
        if blocked <= now and now - window_start > FAIL_WINDOW_S:
            _fails.pop(k, None)
    # 扫完仍超上限（说明是短时间内的海量不同 IP）：按窗口起点丢最旧的，
    # 宁可放弃对老条目的跟踪，也不能让内存无界增长。
    #
    # **正在封锁中的条目一律不淘汰**：否则攻击者先把自己打上封锁（它的
    # `window_start` 最早），再用海量其它 IP 撑爆 dict，自己那条就会作为
    # 「最旧」被丢掉，封锁立即解除——等于给他一个解锁按钮。
    # 封锁最多持续 BLOCK_S 秒，所以它们最终都会被窗口清理掉，不会累积。
    if len(_fails) > MAX_TRACKED:
        over = len(_fails) - MAX_TRACKED
        evictable = sorted(
            (k for k, (_ws, _c, blocked) in _fails.items() if blocked <= now),
            key=lambda k: _fails[k][0],
        )[:over]
        for k in evictable:
            _fails.pop(k, None)
        # 极端情形：全部都在封锁中（只有海量 IP 同时各自打满 10 次才可能）。
        # 这时宁可暂时超出上限，也不能解除任何一条封锁——上限是防内存，
        # 而解除封锁是直接的安全失效，后者不可接受。
        if len(_fails) > MAX_TRACKED:
            _log.warning("口令失败跟踪条目 %d 条超过上限 %d，但都在封锁中，暂不淘汰",
                        len(_fails), MAX_TRACKED)


def _note_failure(key: str, now: float) -> None:
    _sweep(now)
    window_start, count, _blocked = _fails.get(key, (now, 0, 0.0))
    if now - window_start > FAIL_WINDOW_S:
        window_start, count = now, 0
    count += 1
    blocked = now + BLOCK_S if count >= FAIL_MAX else 0.0
    _fails[key] = (window_start, count, blocked)
    if blocked:
        _log.error("口令连续失败 %d 次，封锁 %s 至 %.0f（%d 秒）",
                   count, key, blocked, BLOCK_S)


def _note_success(key: str) -> None:
    _fails.pop(key, None)


def _too_many(until: float) -> JSONResponse:
    retry = max(1, int(until - time.time()))
    return JSONResponse(
        {"detail": f"口令失败次数过多，{retry} 秒后再试"},
        status_code=429,
        headers={"Retry-After": str(retry)},
    )


def make_middleware(conn_factory=None):
    """造一个挂了签名密钥来源的中间件。

    `conn_factory` 用来取 `app_secret.hmac_key`（票据验签）。测试可以不传，
    此时**只认 Basic**——没有密钥就没法验票，退化成原来的行为最安全。
    """

    async def middleware(request: Request, call_next):
        """口令校验。挂在整个 app 上，**逐请求读环境变量**（改了不用重启构造）。"""
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in OPEN_PATHS):
            return await call_next(request)

        creds = _credentials()
        if creds is None:
            return await call_next(request)

        key = _client_key(request)
        now = time.time()
        until = _blocked_until(key, now)
        if until > now:
            return _too_many(until)

        days = _session_days()
        secret = _load_secret(conn_factory) if conn_factory is not None else None

        # ① 已有会话 Cookie：最省事的一条路，手机刷新/换页都不会再弹框
        if secret is not None:
            token = _cookie_value(request)
            exp_raw = request.cookies.get(SESSION_COOKIE + "_exp") or ""
            try:
                exp = int(exp_raw)
            except ValueError:
                exp = 0
            if token and session_ok(secret, token, exp, now):
                _note_success(key)
                return await call_next(request)

            # ② 推送链接里带的票据：验过就发 Cookie，然后把 URL 上的凭据抹掉
            q_token = (request.query_params.get("t") or "").strip()
            q_exp = (request.query_params.get("exp") or "").strip()
            # **带了口令就先校验口令**：否则「有效票据 + 错误口令」会被票据直接放行，
            # 错误口令既不失败也不计数——限流形同虚设。
            if request.headers.get("authorization"):
                bad = _parse_basic(request.headers.get("authorization"))
                want_user, want_pw = creds
                if bad is None or not (
                        hmac.compare_digest(bad[0].encode(), want_user.encode())
                        and hmac.compare_digest(bad[1].encode(), want_pw.encode())):
                    _log.warning("口令错误（票据请求）：path=%s", path)
                    _note_failure(key, now)
                    return _challenge()
            if q_token and q_exp.isdigit() and session_ok(secret, q_token, int(q_exp), now):
                _note_success(key)
                # 去掉 t/exp 后 302，避免票据留在地址栏、历史和 Referer 里
                rest = [(k, v) for k, v in request.query_params.multi_items()
                        if k not in ("t", "exp")]
                qs = "&".join(f"{k}={v}" for k, v in rest)
                target = path + (f"?{qs}" if qs else "")
                from fastapi.responses import RedirectResponse

                resp = RedirectResponse(target, status_code=302)
                return _set_session_cookie(resp, q_token, days)

        got = _parse_basic(request.headers.get("authorization"))
        if got is None:
            # **过期票据不该计入口令失败**：否则手机点几条旧通知就被锁 15 分钟，
            # 而用户根本没输过口令。它也不是猜口令的尝试，不占用限流额度。
            # 但**只要带了口令就照常计数**——否则「票据 + 错误口令」就能绕过限流。
            if not request.query_params.get("t"):
                _note_failure(key, now)
            return _challenge()
        want_user, want_pw = creds
        # **常量时间比较**：普通 == 会因为提前返回泄露前缀长度
        ok_user = hmac.compare_digest(got[0].encode(), want_user.encode())
        ok_pass = hmac.compare_digest(got[1].encode(), want_pw.encode())
        if not (ok_user and ok_pass):
            _log.warning("口令错误：user=%r path=%s", got[0][:40], path)
            _note_failure(key, now)
            return _challenge()
        _note_success(key)
        response = await call_next(request)
        # ③ Basic 成功后也发 Cookie：手动输过一次之后就不再弹原生框
        if secret is not None:
            exp_ts = int(now + days * 24 * 3600)
            token = issue_session(secret, exp_ts)
            _set_session_cookie(response, token, days)
            response.set_cookie(SESSION_COOKIE + "_exp", str(exp_ts),
                                max_age=days * 24 * 3600, httponly=True,
                                secure=True, samesite="lax", path="/")
        return response

    return middleware


# 兼容旧装配：没有 conn_factory 时只认 Basic（不会突然变宽松）
middleware = make_middleware()
