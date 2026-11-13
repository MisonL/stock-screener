"""故障演练闸（RUNBOOK §11）：`SS_DRILL=1` 时任何外发都必须只记日志。

这个模块代码很短，但它守的是**「演练时不能真的推消息出去」**这条线，
而且历史上正是因为它只在 `agent/push.py` 一处判定、`notify/dispatcher.py`
与 `chat/session.py` 各有自己的 NtfyClient，演练进程一旦有网就真发了。

所以这里测的是三件事：
1. 开关判定本身（含 `""` / `"0"` / `"true"` 这些常见误写）；
2. 每次调用都重读环境变量（不是 import 时缓存——`docker compose run -e` 后才注入）；
3. `drill_skip` 只在演练时返回 True，且返回 False 时不产生任何日志副作用。
"""
from __future__ import annotations

import logging

import pytest

from notify.drill import DRILL_ENV, drill_enabled, drill_skip


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从「没设 SS_DRILL」开始，避免被外部环境串味。"""
    monkeypatch.delenv(DRILL_ENV, raising=False)


def test_drill_disabled_by_default():
    assert drill_enabled() is False
    assert drill_skip("dispatcher", kind="alert") is False


@pytest.mark.parametrize("value", ["1", " 1 ", "1\n"])
def test_drill_enabled_accepts_one_with_whitespace(monkeypatch, value):
    """`-e SS_DRILL=1` 经 shell/compose 传递后可能带空白，必须容错。"""
    monkeypatch.setenv(DRILL_ENV, value)
    assert drill_enabled() is True
    assert drill_skip("dispatcher") is True


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "on", "2"])
def test_drill_only_accepts_exactly_one(monkeypatch, value):
    """**只有字面 `1` 才算演练**。

    `SS_DRILL=true` / `SS_DRILL=yes` 这类"看起来像真值"的写法若被当成开启，
    会把正常生产的推送静默吞掉——比演练失效严重得多。
    """
    monkeypatch.setenv(DRILL_ENV, value)
    assert drill_enabled() is False
    assert drill_skip("dispatcher") is False


def test_env_is_read_each_call_not_cached_at_import(monkeypatch):
    """开关必须在**每次调用**时重读。

    `docker compose run -e SS_DRILL=1` 是在进程启动后才注入的；
    若在 import 时缓存，演练会静默失效（该拦的没拦）。
    """
    assert drill_enabled() is False
    monkeypatch.setenv(DRILL_ENV, "1")
    assert drill_enabled() is True, "环境变量在 import 之后才设，开关没跟上"
    monkeypatch.delenv(DRILL_ENV)
    assert drill_enabled() is False, "删掉环境变量后开关没有恢复"


def test_drill_skip_logs_the_reason(monkeypatch, caplog):
    """演练跳过时必须有日志，否则事后无法确认「是谁拦下的」。"""
    monkeypatch.setenv(DRILL_ENV, "1")
    with caplog.at_level(logging.INFO, logger="drill"):
        assert drill_skip("dispatcher", kind="alert", code="sh600519") is True
    assert any("SS_DRILL" in r.message for r in caplog.records), \
        "演练拦截没有留下日志"
    # 关键信息要能对上：哪个出口、什么参数
    assert any("dispatcher" in r.message for r in caplog.records)


def test_non_drill_call_produces_no_log(monkeypatch, caplog):
    """非演练时 `drill_skip` 不该写任何日志——否则每次推送都多一行噪声。"""
    with caplog.at_level(logging.DEBUG, logger="drill"):
        assert drill_skip("dispatcher", kind="alert") is False
    assert [r for r in caplog.records if r.name == "drill"] == []


def test_drill_gate_is_used_by_all_egress_paths():
    """三个外发出口都必须**真的**把 `drill_skip` 用在条件里。

    这是这条规则最容易破的地方：新增一个推送出口时忘了加闸，演练就会真发出去，
    而没有任何测试会因此变红。

    **必须用 AST 而不是 grep**：最初这版只检查源码里出现 `drill_skip` 这个字符串，
    把 `if drill_skip(...)` 改成 `if False and drill_skip(...)` 后测试依然全绿
    （字符串还在），等于没测。AST 能确认它出现在 `if`/`and` 等判定的求值路径上。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    expected = {
        "agent/push.py": "agent 的推送出口",
        "notify/dispatcher.py": "jobs 的通道 A/C 告警、开盘简报、收盘摘要",
        "chat/session.py": "对话的确认卡",
    }

    # 三个出口的写法不同，都要认：
    #   notify/dispatcher.py  -> 直接调用 `drill_skip(...)`
    #   agent/push.py         -> 包成方法 `self._drill_skip(...)`
    #   chat/session.py       -> 别名导入后调用 `_drill(...)`
    GATE_NAMES = {"drill_skip", "_drill_skip", "_drill"}

    def _calls_drill_skip(node) -> bool:
        for n in ast.walk(node):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if name in GATE_NAMES:
                return True
        return False

    def _guarded(tree) -> bool:
        """是否存在一个 if/while/三元/布尔运算，把 drill_skip 的返回值真的拿来判定。"""
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.While)) and _calls_drill_skip(node.test):
                # `if False and drill_skip(...)` 这种短路等于没判
                if any(isinstance(c, ast.Constant) and c.value is False
                       for c in ast.walk(node.test)):
                    continue
                return True
            if isinstance(node, ast.IfExp) and _calls_drill_skip(node.test):
                return True
            if isinstance(node, ast.Assign) and _calls_drill_skip(node.value):
                return True
        return False

    missing = []
    for rel, why in expected.items():
        src = (root / rel).read_text(encoding="utf-8")
        if not _guarded(ast.parse(src)):
            missing.append(f"{rel}（{why}）")
    assert not missing, (
        "以下外发出口没有把演练闸用在判定里，演练时会真的推出消息：" + "；".join(missing))
