"""故障转移必须**有人看得见**。

v0.2.150 加了故障转移，但 `last_failover` 只写内存 + 一行 WARNING 日志，
全仓没有任何消费方。供应商真被换掉时没人知道——和"`LLM_MODEL` 写了没人读"
是同一类错误：机制存在，但不为人知。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm import failover
from llm.client import set_failover_sink


@pytest.fixture()
def conn(tmp_path):
    from core.db import connect, migrate

    c = connect(tmp_path / "f.db")
    migrate(c)
    return c


def _info(**over):
    base = {"from_base_url": "http://old/v1", "from_model": "m-old",
            "to_base_url": "http://new/v1", "to_model": "m-new",
            "reason": "NonRetryable: 不可重试的状态码 403"}
    base.update(over)
    return base


def test_record_persists_last_and_count(conn):
    failover.record(conn, _info(), 20261127)
    got = failover.last(conn)
    assert got == _info()
    assert failover.count_today(conn, 20261127) == 1
    failover.record(conn, _info(to_model="m-new2"), 20261127)
    assert failover.count_today(conn, 20261127) == 2
    assert failover.last(conn)["to_model"] == "m-new2"


def test_count_is_per_day(conn):
    failover.record(conn, _info(), 20261127)
    assert failover.count_today(conn, 20261128) == 0


def test_last_is_none_before_any_failover(conn):
    assert failover.last(conn) is None


def test_corrupt_last_is_not_fatal(conn):
    with conn:
        conn.execute("INSERT INTO job_state(name, value) VALUES (?, '不是JSON')",
                     (failover.LAST_KEY,))
    assert failover.last(conn) is None


def test_alert_is_sent_once_per_switch(conn):
    calls = []

    class D:
        def alert(self, kind, text, level):
            calls.append((kind, level))
            return True

    failover.record(conn, _info(), 20261127, D())
    assert calls == [("llm_failover", "P1")], calls


def test_alert_failure_does_not_lose_the_record(conn):
    """告警发不出去也得先落库——否则"切了且没人知道"最坏的情况同时发生。"""
    class Boom:
        def alert(self, *a, **kw):
            raise RuntimeError("ntfy down")

    failover.record(conn, _info(), 20261127, Boom())
    assert failover.last(conn) == _info()


def test_client_calls_the_registered_sink(monkeypatch):
    """**客户端必须真的调用 sink**——只测 `record()` 证明不了它被接上。"""
    from llm.client import LlmClient, NonRetryable

    seen = []
    set_failover_sink(seen.append)
    try:
        class _Llm:
            base_url = "http://primary/v1"
            model = "m1"
            api_key_env = "TEST_KEY"
            fallbacks = [{"base_url": "http://backup/v1", "model": "m2"}]

        monkeypatch.setenv("TEST_KEY", "k")
        monkeypatch.delenv("LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        c = LlmClient(SimpleNamespace(llm=_Llm()))

        async def fake(ep, *a, **kw):
            if ep.base_url == "http://primary/v1":
                raise NonRetryable(403, "denied")
            return "ok"

        monkeypatch.setattr(c, "_chat_at", fake)
        import asyncio
        assert asyncio.run(c.chat([{"role": "user", "content": "hi"}])) == "ok"
        assert len(seen) == 1, "故障转移没有通知任何人"
        assert seen[0]["to_base_url"] == "http://backup/v1"
    finally:
        set_failover_sink(None)


def test_a_broken_sink_never_breaks_the_call(monkeypatch):
    """落账回调抛异常不能把一次**已经成功**的模型调用弄失败。"""
    from llm.client import LlmClient, NonRetryable

    def bad(_info):
        raise RuntimeError("db locked")

    set_failover_sink(bad)
    try:
        class _Llm:
            base_url = "http://primary/v1"
            model = "m1"
            api_key_env = "TEST_KEY"
            fallbacks = [{"base_url": "http://backup/v1", "model": "m2"}]

        monkeypatch.setenv("TEST_KEY", "k")
        monkeypatch.delenv("LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        c = LlmClient(SimpleNamespace(llm=_Llm()))

        async def fake(ep, *a, **kw):
            if ep.base_url == "http://primary/v1":
                raise NonRetryable(403, "denied")
            return "ok"

        monkeypatch.setattr(c, "_chat_at", fake)
        import asyncio
        assert asyncio.run(c.chat([{"role": "user", "content": "hi"}])) == "ok"
    finally:
        set_failover_sink(None)


def test_both_processes_register_the_sink():
    """api 与 agent 都要注册，而且**注册的必须是真回调**。

    只查子串是不够的：把 `set_failover_sink(lambda ...)` 改成
    `set_failover_sink(None)`，子串检查照样绿（实测）。所以这里看 AST——
    实参不能是 `None`。
    """
    import ast

    for path in ("api/main.py", "agent/main.py"):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "set_failover_sink"):
                found.append(node)
        assert found, f"{path} 没有注册故障转移落账"
        for call in found:
            assert call.args, f"{path} 的 set_failover_sink() 没传回调"
            arg = call.args[0]
            assert not (isinstance(arg, ast.Constant) and arg.value is None), (
                f"{path} 注册的是 None——等于没注册，切换仍然静默")
