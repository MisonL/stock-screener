"""Task 6：串行限速 HTTP 客户端。"""
import time

import httpx
import pytest

from core.http import (
    BROWSER_UA,
    ForbiddenHost,
    Http,
    RateLimiter,
    UpstreamBlocked,
    UpstreamError,
    UpstreamTimeout,
)
from tests.helpers import FakeMono, boom_transport, make_http


def test_limiter_spacing():
    t = FakeMono()
    rl = RateLimiter(0.3, mono=t.now, sleep=t.sleep)
    stamps = [rl.acquire() or t.now() for _ in range(5)]
    assert all(b - a >= 0.3 for a, b in zip(stamps, stamps[1:]))


def test_forbidden_host_refused_before_network():
    with pytest.raises(ForbiddenHost):
        make_http(transport=boom_transport).get("https://web.ifzq.gtimg.cn/x")


def test_scope_is_required():
    # 忘了 scope 不能静默记进一个统计枚举之外的桶
    with pytest.raises(TypeError):
        make_http(transport=boom_transport).get("https://qt.gtimg.cn/x")


def test_no_proxy_from_env(monkeypatch):
    # 用 RFC 5737 保留地址：不要从真实网络里抄代理地址，那会暴露内网布局
    monkeypatch.setenv("HTTPS_PROXY", "http://192.0.2.10:7890")
    assert make_http().client.trust_env is False


def test_default_ua_is_browser_like():
    h = make_http()
    ua = h.client.headers["User-Agent"]      # 只读默认 header，不发真实请求（单测不碰网）
    assert "Mozilla/5.0" in ua and "python" not in ua.lower()
    assert ua == BROWSER_UA


def _transport(status=200, body=b"ok", headers=None):
    def handler(request):
        return httpx.Response(status, content=body, headers=headers)

    return httpx.MockTransport(handler)


def test_501_is_blocked():
    with pytest.raises(UpstreamBlocked):
        make_http(transport=_transport(501)).get("https://qt.gtimg.cn/x", scope="pool")


def test_403_is_blocked():
    with pytest.raises(UpstreamBlocked):
        make_http(transport=_transport(403)).get("https://hq.sinajs.cn/x", scope="pool")


def test_waf_page_is_blocked():
    waf = "您的请求被 Web 应用防火墙 拦截".encode("utf-8")
    with pytest.raises(UpstreamBlocked):
        make_http(transport=_transport(200, waf)).get("https://qt.gtimg.cn/x", scope="pool")


def test_timeout_maps_to_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(UpstreamTimeout):
        make_http(transport=httpx.MockTransport(handler)).get("https://qt.gtimg.cn/x", scope="pool")


def test_connect_error_maps_to_upstream_error():
    with pytest.raises(UpstreamError):
        make_http(transport=boom_transport).get("https://qt.gtimg.cn/x", scope="pool")


@pytest.mark.parametrize("fail", [False, True])
def test_on_request_counts_even_failed_calls(fail):
    seen = []
    transport = boom_transport if fail else _transport()
    h = make_http(transport=transport, on_request=seen.append)
    if fail:
        with pytest.raises(UpstreamError):
            h.get("https://qt.gtimg.cn/x", scope="pool")
    else:
        h.get("https://qt.gtimg.cn/x", scope="pool")
    assert seen == ["pool"]              # 失败请求同样消耗上游配额


def test_calls_and_gaps_recorded_by_family():
    h = make_http(transport=_transport())
    h.get("https://qt.gtimg.cn/a", scope="pool")
    h.get("https://hq.sinajs.cn/b", scope="market")
    h.get("https://qt.gtimg.cn/c", scope="eod")
    assert [(c.family, c.scope) for c in h.calls] == [("tencent", "pool"), ("sina", "market"), ("tencent", "eod")]
    assert h.calls[-1].url == "https://qt.gtimg.cn/c"
    assert len(h.gaps("tencent")) == 1 and h.gaps("sina") == []


def test_get_decodes_with_requested_encoding():
    body = "贵州茅台".encode("gbk")
    assert make_http(transport=_transport(200, body)).get("https://hq.sinajs.cn/x", scope="market", encoding="gbk") == "贵州茅台"


def test_limiter_actually_used_by_family():
    t = FakeMono()
    h = Http({"tencent": RateLimiter(0.3, mono=t.now, sleep=t.sleep)}, transport=_transport())
    for _ in range(3):
        h.get("https://qt.gtimg.cn/x", scope="pool")
    assert all(g >= 0.3 for g in h.gaps("tencent"))
    # 没挂限速器的主机族不受节流（time.monotonic 不是 fake）
    h.get("https://example.com/x", scope="market")
    assert h.calls[-1].family == "example.com"
    assert time.monotonic() >= h.calls[-1].ts_mono
