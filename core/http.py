"""串行限速的上游 HTTP 客户端。

三条硬约束（§6.1.1、§6.2、§6.3）：

1. **相邻请求 ≥ 300ms、禁止并发**：`jobs` 进程只构造一个 `Http`，所有循环共用它。
   「串行」不是靠调用方自觉，而是靠这里的 `RateLimiter` 按主机族节流。
2. **绕过代理**：`trust_env=False`，永远不读 `HTTPS_PROXY`（VM 的全局代理指向台北）。
3. **固定浏览器 UA**：`python-httpx/x.y` 会被上游直接挡掉。

`get()` 的 `scope` 是必填参数（`pool/sector/market/eod/auction/members/backfill`）：
`jobs`/`backfill` 用它把每次请求写进 `upstream_calls`，而 `/api/status` 与验收的
「合计 ≤ 3000」按这个枚举统计——忘记传就得报错，不能静默落进一个没人统计的桶。

计数在 `finally` 里做：**失败的请求同样消耗上游配额**，只在成功路径计数会系统性
低估预算（回填的失败重试尤其明显）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

import httpx

# 固定浏览器 UA（§6.3 要求必填）。调用方可以覆盖，但不能清空。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 已废弃/会触发 WAF 的域名：请求根本不该发出去（§6.1.1）。
FORBIDDEN_HOSTS = frozenset({"web.ifzq.gtimg.cn"})

# WAF 拦截页的少量特征串。宁可少判：误判会把一次正常响应当故障。
_WAF_MARKERS = ("web应用防火墙", "访问被拦截", "请求被拦截", "blocked by waf")

_BLOCKED_STATUS = (403, 501)


class ForbiddenHost(Exception):
    """目标域名在禁用清单里（例如 `web.ifzq.gtimg.cn`）。"""


class UpstreamBlocked(Exception):
    """上游返回 403/501，或返回了 WAF 拦截页。"""


class UpstreamTimeout(Exception):
    """连接或读取超时。"""


class UpstreamError(Exception):
    """其它传输层/解析前的错误。"""


class RateLimiter:
    """串行限速器：保证同一家族相邻两次 `acquire()` 间隔 ≥ `min_interval_s`。

    `mono` / `sleep` 可注入（测试用假时钟，不真的睡）。
    """

    def __init__(
        self,
        min_interval_s: float,
        mono: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.min_interval_s = min_interval_s
        self.mono = mono
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self.mono()
            if self._last is not None:
                wait = self.min_interval_s - (now - self._last)
                if wait > 0:
                    # 多睡 1µs：浮点累加会让「刚好 0.3」算成 0.29999999999999993，
                    # 下游的 ≥0.3 断言与上游的节流口径都按「不少于」理解。
                    self._sleep(wait + 1e-6)
            self._last = self.mono()


@dataclass
class HttpCall:
    """一次上游请求的记录（测试用它算间隔、断言 Referer 与 scope）。"""

    url: str
    family: str
    scope: str
    ts_mono: float
    headers: dict = field(default_factory=dict)

    @property
    def ts(self) -> float:
        """兼容 `calls[-1].ts` 的读法；口径是单调时钟秒。"""
        return self.ts_mono


def _family_of(host: str) -> str:
    if host.endswith("gtimg.cn") or host.endswith("qq.com"):
        return "tencent"
    if host.endswith("sina.com.cn") or host.endswith("sinajs.cn"):
        return "sina"
    return host


def _looks_like_waf(text: str) -> bool:
    # 去掉空白再比：拦截页里的「Web 应用防火墙」常带空格或换行。
    normalized = "".join(text.lower().split())
    return any(marker in normalized for marker in _WAF_MARKERS)


class Http:
    """所有上游请求的唯一出口。"""

    def __init__(
        self,
        limiters: dict[str, RateLimiter],
        timeout_s: float = 10,
        transport: httpx.BaseTransport | None = None,
        ua: str = BROWSER_UA,
        on_request: Callable[[str], None] | None = None,
    ):
        self._limiters = limiters
        self._on_request = on_request
        self.calls: list[HttpCall] = []
        self.client = httpx.Client(
            trust_env=False,               # §6.2：永不读代理环境变量
            timeout=timeout_s,
            transport=transport,
            headers={"User-Agent": ua},
        )

    def get(self, url: str, headers: dict | None = None, encoding: str = "utf-8", scope: str | None = None) -> str:
        host = urlparse(url).hostname or ""
        if host in FORBIDDEN_HOSTS:
            raise ForbiddenHost(f"禁用域名，请求不该发出: {url}")
        if scope is None:
            # 签名上留 None 默认值只为让上面的禁用域名检查先报错；实际调用必须传。
            raise TypeError("Http.get() 缺必填参数 scope（pool/sector/market/eod/auction/members/backfill）")

        family = _family_of(host)
        limiter = self._limiters.get(family)
        try:
            if limiter is not None:
                limiter.acquire()
            ts_mono = limiter.mono() if limiter is not None else time.monotonic()
            self.calls.append(
                HttpCall(url=url, family=family, scope=scope, ts_mono=ts_mono, headers=dict(headers or {}))
            )
            try:
                resp = self.client.get(url, headers=headers)
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout(f"上游超时: {url}") from exc
            except httpx.HTTPError as exc:
                raise UpstreamError(f"上游请求失败: {url}: {exc}") from exc

            if resp.status_code in _BLOCKED_STATUS:
                raise UpstreamBlocked(f"上游拒绝服务（{resp.status_code}）: {url}")
            text = resp.content.decode(encoding, errors="replace")
            if _looks_like_waf(text):
                raise UpstreamBlocked(f"上游返回 WAF 拦截页: {url}")
            return text
        finally:
            if self._on_request is not None:
                self._on_request(scope)

    def gaps(self, family: str) -> list[float]:
        """同一主机族相邻两次请求的时间差（测试断言没有 < 300ms 的间隔）。"""
        stamps = [c.ts_mono for c in self.calls if c.family == family]
        return [b - a for a, b in zip(stamps, stamps[1:])]
