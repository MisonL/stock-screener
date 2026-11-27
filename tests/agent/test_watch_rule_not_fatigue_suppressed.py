"""自选股告警不能被"今天已经说够了"压住。

2026-11-27 核实：`watch_rule` 的显著度是 `base 0.45 × focus.watch 1.3 = 0.585`，
而疲劳阈值在当天推满 13 条后就涨到 0.65 以上。结果是**自选股告警历史上
一条都没推出去过**：

    event_log   kinds='watch_rule' → 2 条（都是 11-27）
    这两条的 agent_status       → 全 suppressed
    push_log    kinds='watch_rule' → 0 条

用户手动加的票是**最高意图**，不该被全局疲惫度压住。
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agent.attention import AttentionLayer, _is_watch
from core.config import load_config
from core.clock import FixedClock
from core.db import connect, migrate
from tests.helpers import SH


@pytest.fixture()
def layer(tmp_path):
    conn = connect(tmp_path / "a.db")
    migrate(conn)
    clock = FixedClock(datetime(2026, 11, 27, 13, 35, tzinfo=SH))
    cfg = load_config(__import__("pathlib").Path("config"))
    return AttentionLayer(conn, cfg, clock), conn, clock, cfg


_SEQ = {"n": 0}


def _event(conn, clock, *, kind, code, salience, minutes_ago=0):
    _SEQ["n"] += 1
    ts = (clock.now() - timedelta(minutes=minutes_ago)).isoformat()
    conn.execute(
        "INSERT INTO event_log(ts, trade_date, market, kind, scope, target, payload,"
        " salience, escalated, agent_status, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, 20261127, "cn_main", kind, "stock", code,
         '{"code": "%s"}' % code, salience, 0, "new", f"{kind}-{code}-{ts}-{_SEQ['n']}"))
    conn.commit()


def _fill_channel_b(conn, clock, n):
    """把当天的通道 B 计数顶上 n，让疲劳阈值涨起来。"""
    for i in range(n):
        conn.execute(
            "INSERT INTO push_log(ts, kind, level, seq_id, segment, body, status, trade_date)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ((clock.now() - timedelta(minutes=60 - i)).isoformat(), "agent_note", "P2",
             f"s{i}", 1, "x", "accepted", 20261127))
    conn.commit()


def test_is_watch_only_recognises_watch_rule():
    assert _is_watch(SimpleNamespace(kind="watch_rule", payload={}))


def test_watch_event_survives_a_high_fatigue_threshold(layer):
    """**核心用例**：通道 B 已经推了 20 条，普通事件会被压，自选股必须放行。"""
    at, conn, clock, _cfg = layer
    _fill_channel_b(conn, clock, 20)                 # 阈值已远超 0.585
    _event(conn, clock, kind="watch_rule", code="sh600519", salience=0.585)

    groups = at.poll()
    assert [g.id for g in groups], "自选股告警被疲劳阈值压掉了"
    assert groups[0].watch is True


def test_normal_event_is_still_suppressed_at_that_threshold(layer):
    """对照组：普通事件本来该被压——证明阈值确实涨起来了，不是没生效。"""
    at, conn, clock, _cfg = layer
    _fill_channel_b(conn, clock, 20)
    _event(conn, clock, kind="stall", code="sh600000", salience=0.30)

    assert at.poll() == []
    status = conn.execute("SELECT agent_status FROM event_log").fetchone()[0]
    assert status == "suppressed"


def test_watch_still_respects_the_30_minute_dedupe(layer):
    """豁免的是**疲劳**，不是去重：同一只票 30 分钟内说过就不再放行。"""
    at, conn, clock, _cfg = layer
    _fill_channel_b(conn, clock, 20)
    _event(conn, clock, kind="watch_rule", code="sh600519", salience=0.585)
    assert at.poll(), "第一次必须放行"
    conn.execute("UPDATE event_log SET agent_status = 'done'")
    conn.commit()
    _event(conn, clock, kind="watch_rule", code="sh600519", salience=0.585, minutes_ago=0)
    assert at.poll() == [], "30 分钟内重复触发却被放行了"
