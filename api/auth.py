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
# 有效期常量在 `notify/hmac_sign.py`（api 与 notify 共用同一个来源）

def _load_secret(conn_factory) -> bytes | None:
    """取签名密钥。**不关连接**：工厂可能返回共享连接。

    **故意不缓存**：`ensure_secret` 只是一次主键 SELECT（`app_secret` 几行），
    而缓存会让「删掉 `app_secret.hmac_key` 让在飞票据立刻失效」变成一个
    最长要等一分钟的操作——安全开关不该有延迟。缓存还曾让测试互相污染
    （单跑通过、合跑失败），这种隐蔽性不值得那点查询开销换。
    """
    global _secret_failures
    try:
        from notify.hmac_sign import ensure_secret

        return ensure_secret(conn_factory())
    except Exception:                        # noqa: BLE001 —— 取不到就退化为仅 Basic
        # 这里**不受"只记一次"限制地打 ERROR**：一旦密钥取不到，所有在飞的
        # 免密链接和 ntfy 按钮会同时失效，用户看到的是"点了没反应"。
        # 只在日志里留一条 warning 等于没人知道——提级成 error 并计数，
        # `/healthz` 也不报这个错（它不碰鉴权），所以必须靠日志/指标发现。
        _secret_failures += 1
        if _secret_failures == 1 or _secret_failures % 100 == 0:
            _log.error("取不到签名密钥，退化为仅 Basic 鉴权（累计 %d 次）",
                       _secret_failures, exc_info=True)
        return None

USER_ENV = "SS_WEB_USER"
PASS_ENV = "SS_WEB_PASS"
DEFAULT_USER = "admin"

# **只放行不通外网的端点**：docker healthcheck 不带凭据，挡了会被判 unhealthy
# 并触发无限重启；`/auth` 本身是登录入口，挡了就没法登录。
OPEN_PATHS = ("/healthz", "/auth")

#: 自带签名保护的路径**前缀**：它们的鉴权由路由自己用 `verify()` 完成，
#: 全局口令中间件必须放行，否则 ntfy 的按钮请求（不携带任何浏览器凭据）会被 401。
#:
#: **为什么必须在中间件放行**：ntfy 的「标记已读」「确认执行」是服务端直接 POST，
#: 根本没有浏览器、没有 Cookie、也不会弹口令框。放行是安全的——两个路由都
#: 强制 `verify(secret, purpose, id, exp, token)` + 过期判断，purpose 写进签名原文，
#: read / confirm / session 三类票据互不通用。
#:
#: **新增 `/api/hook/` 下的路由时必须自己验签**，守卫测试会检查这一点。
SELF_SIGNED_PREFIXES = ("/api/hook/",)


def _has_dotdot_segment(path: str) -> bool:
    """路径里有没有 `..` 段。

    有的话**一律不放行**（当成需要口令的普通路径），即使它看起来落在
    `/healthz/` 或 `/api/hook/` 前缀下。现在的路由不会解析 `..`（实测返回 404），
    所以这条不是修补某个已知漏洞，而是不把「前缀放行」建立在
    「路由器恰好不规范化路径」这个假设上。
    """
    return ".." in path.split("/")

# ---- 失败限流 -----------------------------------------------------------
# 没有限流时，外网可以对 `/api/...` 无限次猜口令（实测连打 10 次全是 401、
# 毫无延迟）。口令又是人起的一串字，被慢慢穷举只是时间问题；一旦猜中，
# `/api/chat` 会花 LLM 预算、`/api/hook/*` 能改状态。
#
# 阈值故意宽松：正常用户输错一两次不该被锁。10 次 / 5 分钟已经远超手滑。
FAIL_WINDOW_S = 300          # 统计窗口
FAIL_MAX = 10                # 窗口内允许的**带口令**失败次数（真的是在猜）
BLOCK_S = 900                # 触发后锁多久

# **匿名请求单独一档**，阈值宽松得多。
# 网页首屏会并行打十几个 /api/*，用户换手机、清 Cookie、或从通知点进一个没有
# 票据的页面，一个全新的浏览器就会连着收到十几次 401。用 10 次那档等于
# 「打开首页就把自己锁 15 分钟」——而这正是免密功能要解决的问题本身。
# 匿名请求猜不出口令（它们不带口令），所以放宽它不会削弱防护。
ANON_MAX = 200                # 窗口内允许的匿名 401 次数
ANON_BLOCK_S = 120            # 触发后锁多久（短：误判的代价要小）

# 跟踪的 IP 上限。**必须有上限**：`CF-Connecting-IP` 是客户端可设置的头，
# 每次换一个、各失败一次，就能让这个 dict 无限增长（100 万个 IP ≈ 100MB），
# 而每个只失败一次连封锁都不会触发——看起来「什么都没发生」。
MAX_TRACKED = 10_000

# ip -> (窗口起点, 次数, 封锁到何时)。**进程内内存**：
# 重启即清空——这是有意的，否则一次内存损坏/PID 复用会把正常用户永久挡在门外。
#
# **两个桶必须分开**：匿名 401（首屏几个请求要不到凭据）和带口令的失败
# （真的在猜）是两种完全不同的行为。共用一个桶会互相污染——网页首屏的十几个
# 匿名 401 把 IP 顶到封锁，用户随后就算输对也进不来；反过来，攻击者猜到第 9 次
# 时用户只是刷新了一次页面，就直接被锁死。
_fails: dict[str, tuple[float, int, float]] = {}    # 带口令的失败（在猜）
_anon: dict[str, tuple[float, int, float]] = {}     # 匿名 401（只是要不到凭据）
#: 累计取不到签名密钥的次数（DB 故障时票据全失效，靠它被发现）
_secret_failures = 0
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
    from notify.hmac_sign import session_days

    return session_days()


def issue_session(secret: bytes, exp_ts: int) -> str:
    """签一张网页访问票据。`ref_id` 固定 0：本系统只有单一口令，没有多用户。"""
    from notify.hmac_sign import sign

    return sign(secret, "session", 0, exp_ts)


def session_signature_valid(secret: bytes, token: str, exp_ts: int) -> bool:
    """**只看签名**，不管是否过期。

    用来区分「合法签发但已过期」与「伪造/垃圾」：前者不该计入口令失败
    （用户点了一条旧通知），后者应该计入（有人在试探）。
    """
    from notify.hmac_sign import verify

    if not token or exp_ts <= 0:
        return False
    return verify(secret, "session", 0, exp_ts, token)


def session_ok(secret: bytes, token: str, exp_ts: int, now: float) -> bool:
    """票据有效且未过期。**过期判断在这里做**：`verify` 只看签名。"""
    from notify.hmac_sign import verify

    if not token or exp_ts <= 0 or now >= exp_ts:
        return False
    return verify(secret, "session", 0, exp_ts, token)


def _as_int(raw) -> int:
    """把 Cookie / 查询参数转成 int；不是数字就是 0（当"没有"处理）。"""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _is_stale_ticket(request: Request, secret: bytes, now: float) -> bool:
    """票据签名有效但已过期——这种不该计入失败（用户点了一条旧通知）。"""
    token = (request.query_params.get("t") or "").strip()
    exp = _as_int(request.query_params.get("exp"))
    return bool(token and exp) and session_signature_valid(secret, token, exp) and exp <= now


def _cookie_value(request: Request) -> str:
    return (request.cookies.get(SESSION_COOKIE) or "").strip()


def _set_session_cookie(response, token: str, exp_ts: int):
    """写入 `ss_session` **与** `ss_session_exp` 两枚 Cookie。

    **必须同时写**：Cookie 分支验签要用到期时刻，而这个时刻只存在于 URL 的
    `exp` 参数里。上一版票据分支只写了 `ss_session`，于是 302 之后的下一次请求
    找不到 `exp` → `session_ok` 判否 → 又落回 Basic 弹框，**免密链路完全没生效**。
    测试当时只断言"302 + Set-Cookie 里有 ss_session"，所以全绿。

    有效期取票据自己的 `exp`，不另算：两者不一致会让 Cookie 比票据活得久或短。
    """
    max_age = max(0, int(exp_ts - time.time()))
    for name, value in ((SESSION_COOKIE, token),
                        (SESSION_COOKIE + "_exp", str(exp_ts))):
        response.set_cookie(
            name, value,
            max_age=max_age, httponly=True, secure=True, samesite="lax", path="/",
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


def _blocked_until(key: str, now: float, anonymous: bool = False) -> float:
    """返回封锁截止时刻；未封锁返回 0，并顺手清掉过期记录。"""
    entry = (_anon if anonymous else _fails).get(key)
    if entry is None:
        return 0.0
    window_start, count, blocked = entry
    if blocked > now:
        return blocked
    if now - window_start > FAIL_WINDOW_S:
        (_anon if anonymous else _fails).pop(key, None)   # 窗口已过，清零
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
    for bucket in (_fails, _anon):
        for k, (window_start, _count, blocked) in list(bucket.items()):
            if blocked <= now and now - window_start > FAIL_WINDOW_S:
                bucket.pop(k, None)
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


def _note_failure(key: str, now: float, anonymous: bool = False) -> None:
    """记一次失败。`anonymous=True` 表示请求**没带口令**（只是要不到凭据）。

    两档阈值：带口令的（在猜）10 次；匿名的 200 次。见 `ANON_MAX` 的注释。
    """
    _sweep(now)
    bucket = _anon if anonymous else _fails
    limit = ANON_MAX if anonymous else FAIL_MAX
    block_s = ANON_BLOCK_S if anonymous else BLOCK_S
    window_start, count, _blocked = bucket.get(key, (now, 0, 0.0))
    if now - window_start > FAIL_WINDOW_S:
        window_start, count = now, 0
    count += 1
    blocked = now + block_s if count >= limit else 0.0
    bucket[key] = (window_start, count, blocked)
    if blocked:
        _log.error("%s %d 次，封锁 %s 至 %.0f（%d 秒）",
                   "匿名请求" if anonymous else "口令连续失败",
                   count, key, blocked, block_s)


def _note_success(key: str) -> None:
    """认证成功：**两个桶都清**。真的登进来了就不该再背任何计数。"""
    _fails.pop(key, None)
    _anon.pop(key, None)


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
        """口令校验。挂在整个 app 上，**逐请求读环境变量**（改了不用重启构造）。

        判定顺序（**顺序本身就是语义**，改之前先读这段）：

        1. 免鉴权路径 / 自带签名的回调 → 放行
        2. 没配口令 → 放行（本机开发）
        3. **已认证凭据优先**，且**不看封锁**：
           a. 会话 Cookie —— 已是"认证通过"的证据。
              否则手机上口令手滑几次被锁 15 分钟、连点通知链接都进不来，
              而那正是这个功能要解决的问题。
           b. 带了口令就先校验口令 —— `有效票据 + 错误口令`不能因为票据就放过，
              否则错误口令既不失败也不计数，限流形同虚设。
           c. 推送票据 → 302 + 发 Cookie。
        4. **封锁判断**：到这里还没有有效凭据，才是"猜口令"，限流只挡这个。
        5. Basic 校验；成功后也发 Cookie，手动输过一次就不再弹框。

        过期票据不计失败、垃圾票据照常计数——见 `_is_stale_ticket`。
        """
        path = request.url.path
        if not _has_dotdot_segment(path):
            if any(path == p or path.startswith(p + "/") for p in OPEN_PATHS):
                return await call_next(request)
            if any(path.startswith(prefix) for prefix in SELF_SIGNED_PREFIXES):
                return await call_next(request)

        creds = _credentials()
        if creds is None:
            return await call_next(request)

        key = _client_key(request)
        now = time.time()
        until = _blocked_until(key, now)
        secret = _load_secret(conn_factory) if conn_factory is not None else None
        days = _session_days()
        # 口令是否已在 ③-2 校验通过（已验证过的请求不再走 ⑤ 的校验与计数）
        password_ok = False

        if secret is not None:
            # ③-0 **带了口令就先校验口令**：`有效票据 + 错误口令`不能因为票据
            # 就放过，否则错误口令既不失败也不计数，限流形同虚设。
            # 放在票据之前是因为口令是"显式声明要认证"的最强意图。
            if request.headers.get("authorization"):
                got = _parse_basic(request.headers.get("authorization"))
                want_user, want_pw = creds
                if got is None or not (
                        hmac.compare_digest(got[0].encode(), want_user.encode())
                        and hmac.compare_digest(got[1].encode(), want_pw.encode())):
                    _log.warning("口令错误：path=%s", path)
                    _note_failure(key, now)
                    until = _blocked_until(key, now)
                    return _too_many(until) if until > now else _challenge()
                password_ok = True

        if secret is not None:
            # ③-1 **URL 里带有效票据就抹掉**，不管当前是否已登录。
            # 先判 Cookie 会让"已登录 + 带票据的链接"直接 200，
            # 票据就留在地址栏和历史里（30 天可用）。
            q_token = (request.query_params.get("t") or "").strip()
            q_exp = _as_int(request.query_params.get("exp"))
            if q_token and q_exp and session_ok(secret, q_token, q_exp, now):
                _note_success(key)
                rest = [(k, v) for k, v in request.query_params.multi_items()
                        if k not in ("t", "exp")]
                qs = "&".join(f"{k}={v}" for k, v in rest)
                from fastapi.responses import RedirectResponse

                resp = RedirectResponse(path + (f"?{qs}" if qs else ""), status_code=302)
                resp.headers.setdefault("Cache-Control", "no-store")
                return _set_session_cookie(resp, q_token, q_exp)

            # ③-2 会话 Cookie：最省事的一条路，手机刷新/换页都不会再弹框
            token = _cookie_value(request)
            exp = _as_int(request.cookies.get(SESSION_COOKIE + "_exp"))
            if token and session_ok(secret, token, exp, now):
                _note_success(key)
                return await call_next(request)


        # ④ 到这里还没有有效凭据：只对"猜口令"限流
        if until > now:
            # **带口令的（在猜）给 429**；**匿名的给 401 + challenge**。
            # 429 不带 `WWW-Authenticate`，iOS Safari / Chrome 不会弹原生登录框，
            # 用户只看到一坨 JSON——登录入口事实上消失。匿名请求本来就没在猜，
            # 给它 401 既保留背压（不继续跑业务），又让人还能登进来。
            if request.headers.get("authorization"):
                return _too_many(until)
            return _challenge()

        if password_ok:
            # 口令在 ③-2 已验过、也已计过（如果错的话早返回了）。
            # 这里直接放行并补发 Cookie，**绝不重复计数**。
            _note_success(key)
            response = await call_next(request)
            if secret is not None:
                exp_ts = int(now + days * 24 * 3600)
                _set_session_cookie(response, issue_session(secret, exp_ts), exp_ts)
            return response

        got = _parse_basic(request.headers.get("authorization"))
        if got is None:
            # **只有「合法签发但已过期」的票据才不计失败**：用户点了一条旧通知，
            # 不该被锁。伪造/垃圾票据照常计数——否则 `?t=garbage` 可无限试探。
            anon_blocked = _blocked_until(key, now, anonymous=True)
            if anon_blocked > now:
                return _challenge()          # 已限流：401 + challenge，不再计数
            if not (secret is not None and _is_stale_ticket(request, secret, now)):
                _note_failure(key, now, anonymous=True)
                # 刚触发限流也**不返回 429**：匿名请求没有 `WWW-Authenticate`
                # 就弹不出登录框（见 ④ 的注释），统一 401 + challenge。
                # 背压照样生效——请求止步于此，不会进业务。
            return _challenge()
        want_user, want_pw = creds
        # **常量时间比较**：普通 == 会因为提前返回泄露前缀长度
        ok_user = hmac.compare_digest(got[0].encode(), want_user.encode())
        ok_pass = hmac.compare_digest(got[1].encode(), want_pw.encode())
        if not (ok_user and ok_pass):
            _log.warning("口令错误：user=%r path=%s", got[0][:40], path)
            _note_failure(key, now)
            until = _blocked_until(key, now)
            return _too_many(until) if until > now else _challenge()
        _note_success(key)
        response = await call_next(request)
        # ⑤ Basic 成功后也发 Cookie：手动输过一次之后就不再弹原生框
        if secret is not None:
            exp_ts = int(now + days * 24 * 3600)
            _set_session_cookie(response, issue_session(secret, exp_ts), exp_ts)
        return response

    return middleware


# **故意不导出**模块级的 `middleware = make_middleware()`：它是"只认 Basic、
# 不发票"的降级入口，一旦有人 `app.middleware("http")(auth.middleware)`，
# 免密登录会**静默失效**且不报错。要装配就显式传 conn_factory：
#     app.middleware("http")(make_middleware(conn_factory))
# `make_middleware()` 不传参仍然可用（测试要只认 Basic 时用），但不留全局别名。
