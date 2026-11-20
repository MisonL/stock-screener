"""串行限速 HTTP 客户端。"""
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


def test_direct_connection_config_is_actually_read(monkeypatch):
    """**`strategy.direct_connection` 必须真的生效**（v0.2.105）。

    审计发现这个键全仓零读取：`httpx.Client(trust_env=False)` 是写死的字面量。
    这是本项目专门写进 §6.2 的"容器绝不能走代理"开关，改 YAML 却完全没用——
    出代理事故时排查方向会被直接带偏。

    接线后 `trust_env = not direct_connection`，默认配置 `true` 仍是直连
    （与旧行为一致）。
    """
    import core.http as H

    monkeypatch.setattr(H, "_DIRECT_CACHE", None)
    assert H._direct() is True, "默认配置应当直连（trust_env=False）"
    # **必须验到 `Http` 真的用了它**：只测 `_direct()` 本身是假绿——
    # 变异「trust_env 改回字面量 False」照样绿（实测过，第一版就是）。
    assert H.Http(limiters=None).client.trust_env is False, (
        "Http 没把 direct_connection 传给 httpx：接线等于没做")

    # 改成 false → 必须真的读到（此时才允许读代理变量）
    monkeypatch.setattr(H, "_DIRECT_CACHE", False)
    assert H._direct() is False
    assert H.Http(limiters=None).client.trust_env is True, (
        "把 direct_connection 设成 false 后 Http 仍是直连：配置没生效")

    # 读配置失败时回退到安全侧（直连）
    monkeypatch.setattr(H, "_DIRECT_CACHE", None)
    import core.config as C
    monkeypatch.setattr(C, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert H._direct() is True, "配置读不到时必须回退直连，不能抛"


def test_all_four_http_exits_read_direct_connection(monkeypatch):
    """**四个出口都必须读 `strategy.direct_connection`**（v0.2.105，评审 S6）。

    只接 `core/http.py` 一处是半接线：配置说"走代理"却只有行情层听，
    LLM / 推送 / MCP 仍直连——那**比完全没有这个开关更误导**，
    出代理事故时排查方向会被直接带偏。

    把 `direct_connection` 设成 False，四个出口新建的 client 都必须
    `trust_env is True`；设成 True（默认）则都必须 `trust_env is False`。
    """
    import core.http as H

    def _check_all(expect_trust_env: bool):
        # ① 行情 Http
        assert H.Http(limiters=None).client.trust_env is expect_trust_env, "行情 Http"
        # ② 推送 ntfy
        from notify.ntfy import NtfyClient
        n = NtfyClient("http://x", "t")
        assert n.http.trust_env is expect_trust_env, "推送 ntfy"
        # ③ MCP westock
        from mcp.westock import WestockMcp
        w = WestockMcp()
        assert w.http.trust_env is expect_trust_env, "MCP westock"

    monkeypatch.setattr(H, "_DIRECT_CACHE", False)
    _check_all(True)

    monkeypatch.setattr(H, "_DIRECT_CACHE", True)
    _check_all(False)
