"""LLM 端点故障转移。

2026-11-27 生产事故：供应商收回了密钥所属分组的权限，`/chat/completions`
对**每一个**模型都返回 403，agent 整段静默 24 小时。

重试救不了这个——`403` 在 `retry.never_retry` 里是刻意的，权限错误重试
一万次也不会变好。缺的是**换一个端点**。
"""

import time

import pytest

from llm.client import (
    FAILOVER_COOLDOWN_S,
    BudgetExceeded,
    LlmClient,
    NonRetryable,
    RetriesExhausted,
)


class _Llm:
    base_url = "http://primary/v1"
    model = "model-primary"
    api_key_env = "TEST_LLM_KEY"

    def __init__(self, fallbacks=None):
        if fallbacks is not None:
            self.fallbacks = fallbacks


class _Cfg:
    def __init__(self, fallbacks=None):
        self.llm = _Llm(fallbacks)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("TEST_LLM_KEY", "k-primary")


def _client(fallbacks=None) -> LlmClient:
    return LlmClient(_Cfg(fallbacks))


def test_fallbacks_are_loaded_after_the_primary():
    c = _client([{"base_url": "http://backup/v1", "model": "model-backup"}])
    assert [(e.base_url, e.model) for e in c.endpoints] == [
        ("http://primary/v1", "model-primary"),
        ("http://backup/v1", "model-backup"),
    ]


def test_fallback_model_defaults_to_primary_when_omitted():
    c = _client([{"base_url": "http://backup/v1"}])
    assert c.endpoints[1].model == "model-primary"


def test_bad_fallback_entries_are_skipped_not_fatal():
    """**备用配置写错不能把主渠道一起弄坏。**

    空地址、非映射项、与主渠道完全重复的项都跳过——主渠道该能用还是能用。
    """
    c = _client([
        {"base_url": ""},
        {"model": "no-url"},
        "不是映射",
        {"base_url": "http://primary/v1", "model": "model-primary"},
        {"base_url": "http://backup/v1", "model": "model-backup"},
    ])
    assert [e.base_url for e in c.endpoints] == [
        "http://primary/v1", "http://backup/v1",
    ]


def test_no_fallbacks_keeps_the_single_endpoint():
    assert len(_client().endpoints) == 1
    assert len(_client([]).endpoints) == 1


async def test_fatal_error_fails_over_to_the_next_endpoint(monkeypatch):
    """403（`NonRetryable`）必须换端点，而不是直接失败。"""
    c = _client([{"base_url": "http://backup/v1", "model": "model-backup"}])
    calls = []

    async def fake(ep, *a, **kw):
        calls.append((ep.base_url, ep.model))
        if ep.base_url == "http://primary/v1":
            raise NonRetryable(403, "No permission to access group")
        return "备用渠道的结果"

    monkeypatch.setattr(c, "_chat_at", fake)
    got = await c.chat([{"role": "user", "content": "hi"}])
    assert got == "备用渠道的结果"
    assert [x[0] for x in calls] == ["http://primary/v1", "http://backup/v1"]
    # 必须留下记录，否则"悄悄换了供应商"没人知道
    assert c.last_failover is not None
    assert c.last_failover["to_base_url"] == "http://backup/v1"
    assert "403" in c.last_failover["reason"] or "NonRetryable" in c.last_failover["reason"]


async def test_retries_exhausted_also_fails_over(monkeypatch):
    c = _client([{"base_url": "http://backup/v1"}])
    calls = []

    async def fake(ep, *a, **kw):
        calls.append(ep.base_url)
        if ep.base_url == "http://primary/v1":
            raise RetriesExhausted("连续超时", 3)
        return "ok"

    monkeypatch.setattr(c, "_chat_at", fake)
    assert await c.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert len(calls) == 2


async def test_budget_exceeded_never_fails_over(monkeypatch):
    """**`BudgetExceeded` 绝不能触发切换**——那等于给额度上限开后门。"""
    c = _client([{"base_url": "http://backup/v1"}])
    calls = []

    async def fake(ep, *a, **kw):
        calls.append(ep.base_url)
        raise BudgetExceeded("预算不足以再发一次请求")

    monkeypatch.setattr(c, "_chat_at", fake)
    with pytest.raises(BudgetExceeded):
        await c.chat([{"role": "user", "content": "hi"}])
    assert calls == ["http://primary/v1"], f"预算超限竟然换了端点：{calls}"


async def test_last_endpoint_failure_raises_the_original_error(monkeypatch):
    """全部端点都不可用时，抛出的必须是**原始异常**，不是包装过的。"""
    c = _client([{"base_url": "http://backup/v1"}])

    async def fake(ep, *a, **kw):
        # 每个端点给不同的 payload，才能确认抛上来的是"最后一个"的
        raise NonRetryable(403, f"denied at {ep.base_url}")

    monkeypatch.setattr(c, "_chat_at", fake)
    with pytest.raises(NonRetryable) as ei:
        await c.chat([{"role": "user", "content": "hi"}])
    assert ei.value.status == 403
    assert ei.value.payload == "denied at http://backup/v1", (
        f"抛上来的不是最后一个端点的错误：{ei.value.payload!r}")


def test_failed_endpoint_moves_to_the_back_then_recovers():
    """**冷却不是"永久禁用"**：到点后主渠道自动回到队首。

    "切过去就回不来"比不切换更糟——主渠道恢复了却一直用备用（可能更贵/更差）。
    """
    c = _client([{"base_url": "http://backup/v1"}])
    c._skip_until["http://primary/v1"] = time.monotonic() + 60
    assert [e.base_url for e in c._ordered_endpoints()] == [
        "http://backup/v1", "http://primary/v1",
    ]
    # 模拟冷却到期
    c._skip_until["http://primary/v1"] = time.monotonic() - 1
    assert [e.base_url for e in c._ordered_endpoints()] == [
        "http://primary/v1", "http://backup/v1",
    ]


async def test_failover_sets_a_cooldown_so_the_backup_is_tried_first_next_time(monkeypatch):
    c = _client([{"base_url": "http://backup/v1"}])
    order = []

    async def fake(ep, *a, **kw):
        order.append(ep.base_url)
        if ep.base_url == "http://primary/v1":
            raise NonRetryable(401, "unauthorized")
        return "ok"

    monkeypatch.setattr(c, "_chat_at", fake)
    await c.chat([{"role": "user", "content": "hi"}])
    order.clear()
    await c.chat([{"role": "user", "content": "hi"}])
    assert order == ["http://backup/v1"], (
        f"冷却期内仍然先试主渠道，每次都要多等一次失败：{order}")
    assert FAILOVER_COOLDOWN_S > 0


def test_fallback_can_use_its_own_api_key(monkeypatch):
    monkeypatch.setenv("TEST_BACKUP_KEY", "k-backup")
    c = _client([{"base_url": "http://backup/v1",
                  "api_key_env": "TEST_BACKUP_KEY"}])
    assert c.endpoints[1].api_key == "k-backup"
    assert c.endpoints[0].api_key == "k-primary"


def test_missing_fallback_key_env_falls_back_to_the_primary_key(monkeypatch):
    monkeypatch.delenv("TEST_NOPE_KEY", raising=False)
    c = _client([{"base_url": "http://backup/v1",
                  "api_key_env": "TEST_NOPE_KEY"}])
    assert c.endpoints[1].api_key == "k-primary"
