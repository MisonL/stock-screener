"""网页访问口令（HTTP Basic）。

**为什么需要**：`/auth` 那套 session 登录只用在 `/api/auth/status` 自己身上，
所有业务接口（`/api/status`、`/api/pool`、`/api/chat`、`/api/hook/*`）此前
**完全裸奔**——外网任何人 `curl https://stock.<域名>/api/status` 就能读到
服务状态、池子、信号，甚至调写接口。

**为什么用 Basic 而不是自建登录页**：浏览器原生弹框即可，手机上输一次由系统
记住，不用写前端代码，也不会因为刷新丢 session。

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

    got = _parse_basic(request.headers.get("authorization"))
    if got is None:
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
    return await call_next(request)
