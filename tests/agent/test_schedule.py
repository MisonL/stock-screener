"""自适应心跳与自排闹钟。"""
from datetime import datetime, timedelta

import pytest

from agent.schedule import (after_alarm, due_alarms, next_heartbeat, schedule_alarm,
                            set_fired_turn)
from core.clock import FixedClock
from core.db import connect, migrate
from tests.helpers import SH

T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)


def at(hh, mm, ss=0):
    return T0.replace(hour=hh, minute=mm, second=ss)


@pytest.mark.parametrize("hh,mm,delta", [
    (9, 20, 120),      # 09:15–10:00 密集：2 分钟
    (9, 59, 120),
    (10, 30, 300),     # 其余：5 分钟
    (11, 10, 600),     # 11:00–11:30 放松：10 分钟
    (13, 10, 600),     # 13:00–13:30 放松：10 分钟
    (14, 45, 120),     # 14:30–15:00 密集：2 分钟
])
def test_next_heartbeat_intervals(hh, mm, delta):
    start = at(hh, mm)
    assert next_heartbeat(start) - start == timedelta(seconds=delta)


def test_never_more_than_ten_minutes_within_sessions():
    """逐分钟扫描两段交易时段（不含午休）：间隔最大值 == 10 分钟。

    午休 95 分钟本来就不排心跳，把午休算进「每分钟间隔 ≤ 10 分钟」永远失败。
    """
    def segment(dt):
        hm = dt.strftime("%H:%M")
        if "09:15" <= hm < "11:30":
            return "am"
        if "13:00" <= hm < "15:00":
            return "pm"
        return None

    worst = timedelta(0)
    for start, end in ((at(9, 15), at(11, 30)), (at(13, 0), at(15, 0))):
        cur = start
        while cur < end:
            nxt = next_heartbeat(cur)
            assert nxt > cur
            # 跨越午休/收盘的那一跳不算「盘中间隔」：午休本来就不排心跳
            if segment(nxt) == segment(cur):
                worst = max(worst, nxt - cur)
            cur += timedelta(minutes=1)
    assert worst == timedelta(minutes=10)


def test_lunch_break_targets_1300():
    """11:25 排出来的下一次不能落在午休里（午休没有心跳消费者）。"""
    nxt = next_heartbeat(at(11, 29))
    assert nxt.hour == 13 and nxt.minute == 0


def test_after_close_targets_next_morning():
    nxt = next_heartbeat(at(15, 30))
    assert nxt == (at(15, 30) + timedelta(days=1)).replace(hour=9, minute=15, second=0)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "s.db")
    migrate(c)
    return c


def test_schedule_and_due(conn):
    due = at(10, 5).isoformat()
    alarm_id = schedule_alarm(conn, 1, due, "cn_main", "sh600519", "看回落")
    assert due_alarms(conn, at(10, 0)) == []
    rows = due_alarms(conn, at(10, 6))
    assert [r["id"] for r in rows] == [alarm_id]
    assert rows[0]["target"] == "sh600519"


def test_alarm_claim_is_one_shot(conn):
    """认领是条件更新：第二次必须失败，否则 60 秒的闹钟回合期间会被反复取出。"""
    alarm_id = schedule_alarm(conn, 1, at(10, 5).isoformat(), "cn_main")
    assert after_alarm(conn, alarm_id) is True          # 认领
    assert after_alarm(conn, alarm_id) is False         # 第二次必须失败
    set_fired_turn(conn, alarm_id, 7)                   # record 之后补回合号
    row = conn.execute("SELECT status, fired_turn FROM agent_schedule WHERE id=?",
                       (alarm_id,)).fetchone()
    assert row[0] == "fired" and row[1] == 7
    assert due_alarms(conn, at(10, 6)) == []
