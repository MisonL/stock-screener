"""LLM 客户端（重试分类 / thinking 注入 / 抖动）。"""
import json
from pathlib import Path

import httpx
import pytest

from core.config import load_config
from llm.client import LlmClient, NonRetryable, RetriesExhausted, classify

CFG = load_config(Path("config"))


def ok_body(content="好的", tool_calls=None, finish="stop", tokens=(10, 5)):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "x", "object": "chat.completion", "created": 0, "model": CFG.llm.model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1],
                  "total_tokens": tokens[0] + tokens[1]},
    }


def tool_call(args='{"speak": "x"}', finish="tool_calls"):
    return {
        "id": "c1", "type": "function",
        "function": {"name": "speak", "arguments": args},
    }, finish


class Mock:
    """按脚本依次回放响应（或状态码），并记下每次请求体。"""

    def __init__(self, *script, delay_s=0.0):
        self.script = list(script)
        self.requests = []
        self.delay_s = delay_s

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content or b"{}"))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, int):
            return httpx.Response(item, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=item)

    def client(self) -> LlmClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler), trust_env=False)
        return LlmClient(CFG, http_client=http)

    @property
    def sent_max_tokens(self):
        return [r.get("max_tokens") for r in self.requests]


async def test_request_body_injects_thinking_disabled():
    mock = Mock(ok_body(tool_calls=[tool_call()[0]]))
    await mock.client().chat([{"role": "user", "content": "hi"}])
    assert mock.requests[0]["thinking"] == {"type": "disabled"}


async def test_502_three_attempts_then_exhausted():
    mock = Mock(502, 502, 502)
    with pytest.raises(RetriesExhausted) as exc:
        await mock.client().chat([{"role": "user", "content": "hi"}])
    assert exc.value.attempts == 3 and len(mock.requests) == 3


async def test_400_not_retried():
    mock = Mock(400, ok_body())
    with pytest.raises(NonRetryable) as exc:
        await mock.client().chat([{"role": "user", "content": "hi"}])
    assert exc.value.status == 400 and len(mock.requests) == 1


async def test_empty_content_with_tool_calls_is_success():
    mock = Mock(ok_body(content="", tool_calls=[tool_call()[0]]))
    result = await mock.client().chat([{"role": "user", "content": "hi"}])
    assert result.attempts == 1 and len(mock.requests) == 1


async def test_truncated_tool_args_retried_with_double_tokens():
    bad = ok_body(content="", tool_calls=[tool_call('{"speak": tr')[0]], finish="length")
    mock = Mock(bad, ok_body(tool_calls=[tool_call()[0]]))
    result = await mock.client().chat([{"role": "user", "content": "hi"}], max_tokens=2000)
    assert result.attempts == 2
    assert mock.sent_max_tokens == [2000, 4000]


async def test_empty_body_retried():
    empty = ok_body(content="   ")
    mock = Mock(empty, ok_body(content="好"))
    result = await mock.client().chat([{"role": "user", "content": "hi"}])
    assert result.attempts == 2


def test_backoff_jitter_bounds():
    """退避 = 配置基值 ±30% 抖动。**基值从配置读**，不写死数字。

    退避值会随上游表现调整（限流严重时拉长），写死会让每次调参都来改这条测试。
    """
    import random

    client = Mock(ok_body()).client()
    table = list(client.cfg.retry.backoff)
    assert len(table) >= 2, "退避表至少要有两档（首次重试 + 后续重试）"
    for attempt, base in enumerate(table[:2], start=1):
        got = [client.backoff_delay(attempt, random.Random(i)) for i in range(200)]
        assert min(got) >= base * 0.7 - 1e-9, f"第 {attempt} 档下界越界"
        assert max(got) <= base * 1.3 + 1e-9, f"第 {attempt} 档上界越界"


def test_classify_prefers_tool_calls_over_empty_content():
    from llm.client import _Attempt

    msg = type("M", (), {"tool_calls": [object()], "content": ""})()
    assert classify(_Attempt(message=msg, finish_reason="tool_calls")) == "ok"


def test_env_override_base_url(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://blackhole:1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    client = LlmClient(CFG)
    assert str(client.client.base_url).startswith("http://blackhole:1")
    assert client.client.max_retries == 0


async def test_thinking_injected_on_every_request():
    """只在首个请求注入 thinking 是错的：重试那次同样会烧推理 token。"""
    mock = Mock(502, 502, ok_body(content="好"))
    await mock.client().chat([{"role": "user", "content": "hi"}])
    assert all(r.get("thinking") == {"type": "disabled"} for r in mock.requests)
    assert len(mock.requests) == 3


async def test_tools_default_max_tokens_is_tools_budget():
    """带工具时不传 max_tokens 必须用 tools 预算（2000），否则稳定截断 emit_decision 的 JSON。"""
    mock = Mock(ok_body(tool_calls=[tool_call()[0]]))
    await mock.client().chat([{"role": "user", "content": "hi"}],
                             tools=[{"type": "function", "function": {"name": "x"}}])
    assert mock.sent_max_tokens == [2000]


async def test_plain_default_max_tokens_is_plain_budget():
    mock = Mock(ok_body(content="好"))
    await mock.client().chat([{"role": "user", "content": "hi"}])
    assert mock.sent_max_tokens == [800]


async def test_doubling_happens_at_most_once():
    """连续截断只允许翻倍一次；第二次仍截断应当降级（抛 RetriesExhausted），不能翻到 8000。"""
    bad = ok_body(content="", tool_calls=[tool_call('{"speak": tr')[0]], finish="length")
    mock = Mock(bad, bad, bad)
    with pytest.raises(RetriesExhausted):
        await mock.client().chat([{"role": "user", "content": "hi"}], max_tokens=2000)
    assert mock.sent_max_tokens == [2000, 4000]


async def test_invalid_args_without_length_is_not_doubled():
    """finish_reason=stop 却带回非法 JSON：那是模型的错，翻倍 token 救不回来。"""
    mock = Mock(ok_body(content="", tool_calls=[tool_call('{"speak": tr')[0]], finish="stop"))
    result = await mock.client().chat([{"role": "user", "content": "hi"}], max_tokens=2000)
    assert result.attempts == 1 and mock.sent_max_tokens == [2000]


async def test_empty_tool_args_with_length_is_truncation():
    """arguments="" 是缺参数，不是「无需参数」。"""
    bad = ok_body(content="", tool_calls=[tool_call("")[0]], finish="length")
    mock = Mock(bad, ok_body(tool_calls=[tool_call()[0]]))
    result = await mock.client().chat([{"role": "user", "content": "hi"}], max_tokens=2000)
    assert result.attempts == 2 and mock.sent_max_tokens == [2000, 4000]


async def test_usage_accumulates_across_retries():
    """失败/重试的尝试也花了钱：累计 usage 必须包含它们，否则日预算低估。"""
    mock = Mock(ok_body(content="  ", tokens=(100, 5)), ok_body(content="好", tokens=(10, 5)))
    result = await mock.client().chat([{"role": "user", "content": "hi"}])
    assert result.attempts == 2
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (110, 10)


async def test_exhausted_carries_accumulated_usage():
    mock = Mock(ok_body(content="  ", tokens=(100, 5)), ok_body(content="  ", tokens=(100, 5)))
    with pytest.raises(RetriesExhausted) as exc:
        await mock.client().chat([{"role": "user", "content": "hi"}])
    assert exc.value.usage.prompt_tokens == 300


async def test_backoff_sequence_follows_config():
    import random

    client = Mock(ok_body()).client()
    first = [client.backoff_delay(1, random.Random(i)) for i in range(300)]
    third = [client.backoff_delay(3, random.Random(i)) for i in range(300)]
    assert max(first) < min(third)                 # 0.5 段严格低于 1.0 段
    # 抖动必须真的有离散度：恒返回基值的实现应当被抓出来
    assert len({round(x, 6) for x in first}) > 50


async def test_no_sleep_after_last_attempt(monkeypatch):
    import asyncio as _asyncio

    calls = []

    async def fake_sleep(s):
        calls.append(s)

    mock = Mock(502, 502, 502)
    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    with pytest.raises(RetriesExhausted):
        await mock.client().chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 2                        # 3 次尝试之间只有 2 次等待


def test_nonretryable_from_once_propagates(monkeypatch):
    """`_once` 内部抛的 NonRetryable（如 403）必须原样冒泡，不能被降级成 status=0。"""
    import asyncio

    client = Mock(ok_body()).client()

    async def boom(*a, **k):
        raise NonRetryable(403, "forbidden")

    monkeypatch.setattr(client, "_once", boom)
    with pytest.raises(NonRetryable) as exc:
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert exc.value.status == 403


async def test_chat_timeout_uses_chat_budget():
    """对话回合用 `chat_s`，带工具的盯盘回合用 `agent_call_s`。

    这里**从配置里取期望值**，不写死数字：`agent_call_s` 会因为上游网关
    延迟变化而调整（2026-11-19 的 100% 超时故障就把它从 20 提到 40），
    写死的话每次调超时都要来改这条测试，而且会掩盖"配置没生效"。
    """
    from core.config import load_config
    from pathlib import Path as _P

    cfg = load_config(_P("config"))
    client = Mock(ok_body()).client()

    from llm.client import SAFETY_TIMEOUT_S

    def expected(configured: float):
        # **0 = 不设业务上限**，但仍套兜底硬上限；正数 = 那个数
        return SAFETY_TIMEOUT_S if float(configured) <= 0 else float(configured)

    # 关系本身就是语义：**不传 tools = 对话**（用 chat_s），**传 tools = 盯盘回合**
    # （用 agent_call_s）。写死数字会把"配置改了但代码没读到"一起放过。
    assert client.default_timeout(None, None) == expected(cfg.llm.timeouts.chat_s)
    assert client.default_timeout(None, [{"type": "function"}]) == \
        expected(cfg.llm.timeouts.agent_call_s)
    assert client.default_timeout(None, []) == expected(cfg.llm.timeouts.chat_s), \
        "空 tools 列表应当按对话处理（falsy），不能误用盯盘超时"
    # 显式传入的 timeout_s 仍然生效（可以被单次调用覆盖）
    assert client.default_timeout(7.5, None) == 7.5


def test_zero_timeout_means_no_limit():
    """**配置 `0` = 不设超时**，而不是"立刻超时"。

    这是拿一次生产故障换来的默认值：上游网关排队时单次调用要 23–31 秒，
    任何固定的超时值迟早会被正常调用超过去，然后每一次 attempt 都超时、
    整个交易日所有回合报废（实盘发生过：一天 21/21 个回合 timeout，
    44 次 attempt 没有一次拿到回包）。

    不设上限后靠两件事兜住：错误自动重试 + 回合轮数上限。
    """
    from core.config import load_config
    from pathlib import Path

    cfg = load_config(Path("config"))
    client = Mock(ok_body()).client()

    from llm.client import SAFETY_TIMEOUT_S

    # 配置写 0 → 不设**业务**上限，但落回兜底硬上限
    assert client.default_timeout(0, None) == SAFETY_TIMEOUT_S
    assert client.default_timeout(-1, None) == SAFETY_TIMEOUT_S
    # 正数仍然生效（业务上限优先）
    assert client.default_timeout(12, None) == 12
    # **绝不能返回 0、负数或 None**：
    #   0/负数 → SDK 当成立刻超时（每次 attempt 瞬间失败）
    #   None   → SDK 当成"永远等"（上游不回包就永久卡住）
    for configured in (0, 0.0, -1):
        got = client.default_timeout(configured, None)
        assert got is not None and got > 0, f"timeout={got} 会让调用失效或永久挂住"
    # 兜底值必须显著大于实测最慢的单次调用（31 秒），否则会误杀慢但正常的调用
    assert SAFETY_TIMEOUT_S >= 31 * 5, "兜底上限太小，会误杀慢但正常的调用"
    # 但也不能大到卡住后久久不恢复
    assert SAFETY_TIMEOUT_S <= 900, "兜底上限过大，卡住时恢复太慢"


def test_429_is_retried_not_fatal():
    """**429 限流必须重试**。它是"稍后再来"，不是"你错了"。

    以前 `classify()` 用的是写死的 `_FATAL_STATUS`，配置里的
    `retry_on`/`never_retry` 两个键根本没人读——改 YAML 不会有任何效果。
    """
    from llm.client import _Attempt, classify

    def fatal_of(cfg):
        return tuple(cfg.retry.never_retry)

    from core.config import load_config
    from pathlib import Path

    cfg = load_config(Path("config"))
    fatal = fatal_of(cfg)

    retry_on = tuple(cfg.retry.retry_on)

    assert classify(_Attempt(error="限流", status=429),
                    fatal_status=fatal, retry_on=retry_on) == "retry"
    assert "429" not in [str(x) for x in fatal], "429 不能被列为不可重试"
    assert "429" in [str(x) for x in retry_on], "429 必须在 retry_on 里"

    # **两个键都必须真的接线**，否则改 YAML 不会有任何效果。
    # 没有这两条断言，"参数没接线"和"接线了"的表现完全一样。
    assert classify(_Attempt(error="限流", status=429),
                    fatal_status=(429,), retry_on=retry_on) == "fatal", \
        "classify 没有使用传入的 never_retry"
    assert classify(_Attempt(error="x", status=418),
                    fatal_status=fatal, retry_on=(418,)) == "retry", \
        "classify 没有使用传入的 retry_on"
    # 4xx 里真正该放弃的仍然放弃
    for status in (400, 401, 403, 404):
        assert classify(_Attempt(error="x", status=status),
                        fatal_status=fatal, retry_on=retry_on) == "fatal"
    # 5xx 一律重试
    for status in (500, 502, 503, 504):
        assert classify(_Attempt(error="x", status=status),
                        fatal_status=fatal, retry_on=retry_on) == "retry"


async def test_chat_uses_configured_never_retry(monkeypatch):
    """**端到端**：`chat()` 必须真的把 `retry.never_retry` 传给 `classify()`。

    只测 `classify(...)` 的参数是不够的：`chat()` 里漏传时，默认集合恰好和
    配置一致，表现完全相同——"参数没接线"和"接线了"分不出来。

    这里把 503（默认**要**重试的）塞进 `never_retry`：接线了就该立刻放弃。
    """
    from llm.client import NonRetryable, RetriesExhausted

    # 直接构造一个只改了 never_retry 的配置对象：`RetryCfg` 是 _NS，
    # 用 `replace` 太重，这里用 SimpleNamespace 覆盖那一层即可。
    from types import SimpleNamespace

    real = CFG
    cfg = SimpleNamespace(llm=real.llm,
                          retry=SimpleNamespace(max_attempts=real.retry.max_attempts,
                                                backoff=(0.0, 0.0),
                                                retry_on=real.retry.retry_on,
                                                never_retry=(503,)))
    m = Mock(503, ok_body("不该走到这里"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(m.handler), trust_env=False)
    client = LlmClient(cfg, http_client=http)
    with pytest.raises((RetriesExhausted, NonRetryable)):
        await client.chat([{"role": "user", "content": "hi"}], max_tokens=50)
    assert len(m.requests) == 1, (
        f"配置里 503 是 never_retry，却重试了 {len(m.requests)} 次——参数没接线")


async def test_chat_retries_after_429(monkeypatch):
    """**端到端**：第一次 429，第二次 200 —— 必须重试并成功返回。

    只看 `classify()` 的返回值不够：真正的行为在 `chat()` 的重试循环里。
    """
    cfg = CFG
    monkeypatch.setattr(type(cfg.retry), "backoff", property(lambda self: (0.0, 0.0)),
                        raising=False)
    m = Mock(429, ok_body("重试后成功"))
    client = m.client()
    res = await client.chat([{"role": "user", "content": "hi"}], max_tokens=50)
    assert res.message.content == "重试后成功"
    assert len(m.requests) == 2, f"429 没有触发重试（实际发了 {len(m.requests)} 次）"


def test_success_status_is_not_judged_by_status_code():
    """**200 不能被状态码判成 fatal**——正常响应也要走正文/finish_reason 判定。

    这是一个真实的回归：`_status_verdict()` 默认把非 5xx 都当 fatal，
    而 `_Attempt.status` 在**正常响应上也是 200**。于是所有成功响应都被定性为
    致命错误，"截断 → 翻倍重试"这条路径直接不触发（实测
    `test_doubled_retry_deducts_each_attempt` 红：`_once` 只被调了 1 次）。
    """
    from llm.client import _status_verdict

    for status in (200, 201, 204, 301):
        assert _status_verdict(status) == "ok", f"{status} 不该被当成错误"
    # 4xx/5xx 的判定保持不变
    assert _status_verdict(400) == "fatal"
    assert _status_verdict(503) == "retry"
    assert _status_verdict(0) == "retry"
    # `retry_on` 能把一个本来 fatal 的 4xx 拉回重试
    assert _status_verdict(418, retry_on=(418,)) == "retry"
    # `never_retry` 优先于 `retry_on`
    assert _status_verdict(429, retry_on=(429,), fatal_status=(429,)) == "fatal"


async def test_empty_choices_is_retried():
    """**空 `choices` 必须重试**，不能被状态码判成致命错误。

    这条是回归正控：`_Attempt` 的空响应用的是 `status=200`，而
    `_status_verdict()` 默认把非 5xx 当 fatal。没有 2xx 豁免时，
    "空响应 → 重试"这条路径会静默消失（实测 `NonRetryable status=200 requests=1`），
    而 focused 测试全绿——正是这种假绿最难发现。
    """
    from llm.client import RetriesExhausted

    m = Mock({"choices": []})
    client = m.client()
    with pytest.raises(RetriesExhausted):
        await client.chat([{"role": "user", "content": "hi"}], max_tokens=50)
    assert len(m.requests) == CFG.retry.max_attempts, (
        f"空 choices 没有重试：只发了 {len(m.requests)} 次请求"
        f"（应当用满 {CFG.retry.max_attempts} 次）")
