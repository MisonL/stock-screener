"""异动日的推送额度：**最重要的那条不能被"前面说够了"挡死**。

2026-11-27 实测（A 股涨停 250→306 的强势日）：

    13:22:23  第 15 条 agent_note 发出，通道 B 日上限打满
    13:43:09  agent 判定该说"飞利信站上箱体上沿，涨停家数破 250"
              → **被静默丢弃**，push_log 里连 suppressed 都没有
    13:43-14:00  又白跑 24 次发言，全部丢弃

也就是说：**全年最强的一天，用户在最猛的时段一条都没收到**。
本文件的用例固定三件事：极端行情可突破、突发节流、丢弃可查。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import load_config

from agent.loop import _is_critical


class _Attention:
    def __init__(self, critical=0.90):
        self.salience_min = 0.50
        self.salience_critical = critical


class _Strategy:
    def __init__(self, critical=0.90):
        self.attention = _Attention(critical)


class _Cfg:
    def __init__(self, critical=0.90):
        self.strategy = _Strategy(critical)


class _Ev:
    def __init__(self, salience):
        self.salience = salience


# ---- 极端行情判定 -------------------------------------------------------

@pytest.mark.parametrize("salience,expected", [
    (1.12, True),     # index_move 实测值
    (0.90, True),     # 边界：含等号
    (0.899, False),
    (0.585, False),   # watch_rule 实测值
    (0.0, False),
])
def test_salience_at_or_above_threshold_is_critical(salience, expected):
    assert _is_critical(_Ev(salience), _Cfg()) is expected


def test_missing_event_is_never_critical():
    """心跳回合没有事件，不能被当成极端行情。"""
    assert _is_critical(None, _Cfg()) is False


def test_broken_config_is_conservative():
    """配置读不到时**返回 False**：宁可少放几条，不能把上限整个打开。"""
    class Broken:
        pass
    assert _is_critical(_Ev(1.5), Broken()) is False


def test_non_numeric_salience_is_not_critical():
    class Bad:
        salience = "很高"
    assert _is_critical(Bad(), _Cfg()) is False


# ---- 推送门控 -----------------------------------------------------------

def test_critical_extra_lets_the_most_important_note_through():
    """日上限打满后：普通便签照旧被拦，极端行情还能再放 `critical_extra` 条。"""
    import inspect
    from agent.push import AgentPusher

    src = inspect.getsource(AgentPusher.agent_note)
    assert "channel_b_critical_extra" in src, (
        "agent_note 没有读 channel_b_critical_extra，极端行情仍会被日上限挡死")
    assert "critical" in src, "agent_note 没有 critical 参数"
    # 额度必须是 cap + extra，而不是无条件放行
    assert "cap + (extra if critical else 0)" in src, (
        "极端行情放行必须仍然有上限，否则等于关掉了日上限")


def test_burst_throttle_exists_and_skips_critical():
    """突发节流必须在，且不能拦极端行情。"""
    import inspect
    from agent.push import AgentPusher

    src = inspect.getsource(AgentPusher.agent_note)
    assert "agent_note_min_gap_s" in src, "没有突发节流：66 秒 5 条会重演"
    assert "if not critical:" in src, "节流把极端行情也拦了"


def test_dropped_notes_leave_a_countable_trace():
    """**被丢弃的便签必须留痕**。

    原来只在 `agent_turn` 写一行 `why`，而 `agent_turn` 是回合流水，
    没人会去数"今天被丢了多少条"。复盘 11-27 时我查了半天才发现
    13:43 那条根本没进 `push_log`。
    """
    import inspect
    from agent.push import AgentPusher

    src = inspect.getsource(AgentPusher._audit_cap)
    assert "push.dropped." in src, "丢弃没有落库计数，事后无法统计"


# ---- 行为级：真的跑一遍 agent_note ------------------------------------

def _harness(tmp_path, *, cap=2, extra=1, gap=45, minute=13, hour=22):
    """建库 + 造一条已满的通道 B 额度，返回 (pusher, conn, clock)。"""
    from datetime import datetime as _dt

    from agent.push import AgentPusher
    from core.clock import FixedClock
    from core.db import connect as _connect, migrate as _migrate
    from tests.helpers import SH as _SH

    conn = _connect(tmp_path / "p.db")
    _migrate(conn)
    now = _dt(2026, 11, 27, hour, minute, tzinfo=_SH)
    # **用真配置做底，只覆盖三个额度键**：手搓 SimpleNamespace 会一路漏字段
    # （第一次就漏了 `markets.enabled_markets`，报错点在 push.py 深处，与额度无关）。
    class _Proxy:
        def __init__(self, base, **over):
            object.__setattr__(self, "_base", base)
            object.__setattr__(self, "_over", over)

        def __getattr__(self, name):
            if name in self._over:
                return self._over[name]
            return getattr(self._base, name)

    real = load_config(Path("config"))
    cfg = SimpleNamespace(
        notify=real.notify, strategy=real.strategy, llm=real.llm,
        retry=real.retry, markets=real.markets,
        enabled_markets=real.enabled_markets,
        agent=_Proxy(real.agent, channel_b_daily_cap=cap,
                     channel_b_critical_extra=extra,
                     agent_note_min_gap_s=gap))

    class _Ntfy:
        # 方法名是 `publish`（`_send()` 调的就是它）。写成 `send` 时异常会被
        # `_send` 的 `except Exception` 吞掉、行标 failed、返回 None——测试只会
        # 看到"没发出去"，完全看不出是假件写错了。
        def publish(self, msg):
            return "ntfy-test-id"

    p = AgentPusher(conn, _Ntfy(), cfg, FixedClock(now))
    # 造 cap 条已发出的便签
    for i in range(cap):
        conn.execute(
            "INSERT INTO push_log(ts, kind, level, seq_id, segment, body, status,"
            " trade_date) VALUES (?,?,?,?,?,?,?,?)",
            (now.replace(minute=minute - 1 - i).isoformat(), "agent_note", "P2",
             f"s{i}", 1, "x", "accepted", 20261127))
    conn.commit()
    return p, conn, now


def test_normal_note_is_dropped_once_cap_is_reached(tmp_path):
    p, conn, _ = _harness(tmp_path, cap=2, extra=1, gap=0)
    d = SimpleNamespace(kind="speak", body="普通提醒", why="", level=None)

    class G:
        text = "普通提醒"
        ok = True
        violations = []
        rewritten = None

    assert p.agent_note(d, G(), None, None, critical=False) is None
    # 丢弃要留痕
    row = conn.execute("SELECT value FROM job_state WHERE name LIKE 'push.dropped.%'").fetchone()
    assert row is not None and int(row[0]) >= 1, "被丢弃的便签没有留下计数"


def test_critical_note_still_gets_through_after_the_cap(tmp_path):
    """**这条是 11-27 的核心**：额度满了，极端行情仍要放出来。"""
    p, conn, _ = _harness(tmp_path, cap=2, extra=1, gap=45)
    d = SimpleNamespace(kind="speak", body="涨停家数破 250", why="", level=None)

    class G:
        text = "涨停家数破 250"
        ok = True
        violations = []
        rewritten = None

    seq = p.agent_note(d, G(), None, None, critical=True)
    why = conn.execute("SELECT why FROM agent_turn ORDER BY id DESC LIMIT 1").fetchone()
    assert seq is not None, (
        f"极端行情被日上限挡掉了——11-27 那次的直接原因；最后一条 why={why}")
    n = conn.execute("SELECT count(*) FROM push_log WHERE kind='agent_note'").fetchone()[0]
    assert n == 3, f"放行后应有 cap+1 条，实际 {n}"


def test_critical_beyond_the_extra_is_still_capped(tmp_path):
    """**极端行情也不能无限放**：cap+extra 用完后必须重新拦住。"""
    p, conn, _ = _harness(tmp_path, cap=2, extra=1, gap=0)
    d = SimpleNamespace(kind="speak", body="又一条", why="", level=None)

    class G:
        text = "又一条"
        ok = True
        violations = []
        rewritten = None

    assert p.agent_note(d, G(), None, None, critical=True) is not None   # cap+1
    assert p.agent_note(d, G(), None, None, critical=True) is None       # cap+2 > cap+extra


def test_new_knobs_are_optional_so_old_sites_still_boot():
    """**新键不能进「必填」清单。**

    `config/agent.yaml` 是站点保留文件（`deploy.py::PRESERVE_ON_TARGET`），
    升级时不会被覆盖，而 `load_config()` 对必填键缺失是**直接抛 ConfigError** 的。
    把这两个键列进 `_REQUIRED_AGENT`，等于让所有老站点在升级当天起不来。

    同时，缺键时的默认值必须是"功能开启"——写 0 等于悄悄关掉，
    而这正是 11-27 两条修复要解决的问题。
    """
    from core.config import _EMPTY_AGENT, _REQUIRED_AGENT

    for key in ("channel_b_critical_extra", "agent_note_min_gap_s"):
        assert key not in _REQUIRED_AGENT, (
            f"{key} 进了必填清单，旧站点的 agent.yaml 没有它 → ConfigError 起不来")
        assert key in _EMPTY_AGENT, f"{key} 没有默认值"
        assert int(_EMPTY_AGENT[key]) > 0, f"{key} 默认值是 0，等于功能默认关闭"


def test_push_defaults_match_the_schema():
    """代码里的 getattr 默认值必须与 schema 默认值一致。"""
    from agent.push import _CRITICAL_EXTRA_DEFAULT, _NOTE_GAP_DEFAULT
    from core.config import _EMPTY_AGENT

    assert _CRITICAL_EXTRA_DEFAULT == _EMPTY_AGENT["channel_b_critical_extra"]
    assert _NOTE_GAP_DEFAULT == _EMPTY_AGENT["agent_note_min_gap_s"]
