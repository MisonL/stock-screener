"""Task 2：LLM 客户端（重试分类 / thinking 注入 / 抖动）。"""
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
    import random

    client = Mock(ok_body()).client()
    first = [client.backoff_delay(1, random.Random(i)) for i in range(200)]
    second = [client.backoff_delay(2, random.Random(i)) for i in range(200)]
    assert min(first) >= 0.35 and max(first) <= 0.65
    assert min(second) >= 0.7 and max(second) <= 1.3


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
    """对话回合的默认超时是 60 秒（chat_s），盯盘回合才是 20 秒。"""
    client = Mock(ok_body()).client()
    assert client.default_timeout(None, None) == 60
    assert client.default_timeout(None, [{"type": "function"}]) == 20
