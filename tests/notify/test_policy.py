"""Task 7：`channel_b_today` 的计数口径（B9/D8）。"""
from datetime import datetime, timedelta

import pytest

from core.clock import FixedClock
from notify.policy import (NON_PUSH_KINDS, agent_notes_today, channel_b_today,
                           pushed_today, recent_independent_push)
from tests.helpers import SH

T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)
DAY = 20261030


def _row(conn, *, kind="agent_note", segment=1, status="accepted", day=DAY, seq="s1"):
    conn.execute(
        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (T0.isoformat(), "sh600519", kind, "P1", seq, segment, day, status))
    conn.commit()


def test_counts_only_channel_b_notes(conn):
    _row(conn)
    _row(conn, seq="s2")
    assert channel_b_today(conn, DAY) == 2


def test_template_channel_a_not_counted(conn):
    """模板通道 A（breakout 等）不是通道 B：算进来会让 agent 的开口额度被模板吃掉。"""
    _row(conn, kind="breakout")
    _row(conn, kind="breakdown")
    assert channel_b_today(conn, DAY) == 0


def test_second_segment_not_counted(conn):
    """第二段解读（segment=2）是模板消息的附段，不是独立的一次开口。"""
    _row(conn, segment=2)
    assert channel_b_today(conn, DAY) == 0


def test_suppressed_and_failed_not_counted(conn):
    """被压/失败的行不算「已推」：算进来阈值会被自己抬成「越压越压」的正反馈。"""
    _row(conn, status="suppressed", seq="a")
    _row(conn, status="failed", seq="b")
    assert channel_b_today(conn, DAY) == 0


def test_other_day_not_counted(conn):
    _row(conn, day=20261029)
    assert channel_b_today(conn, DAY) == 0


def test_defaults_to_shanghai_today(conn):
    from core.clock import SystemClock

    today = SystemClock().today()
    _row(conn, day=today)
    assert channel_b_today(conn) == 1


# ---- Task 12：共用计数口径 ----------------------------------------------

def _push(conn, *, kind="breakout", segment=1, status="accepted", seq=None, code="600519",
          event_id=None, day=DAY):
    conn.execute(
        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, event_id, trade_date, status)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (T0.isoformat(), code, kind, "P1", seq or f"s-{kind}-{segment}-{status}", segment,
         event_id, day, status))
    conn.commit()


def test_pushed_today_counts_distinct_seq_not_rows(conn):
    """合并批每只成员一行：按行数算会把 1 条消息算成 N 条，日熔断提前触发。"""
    for _ in range(5):
        _push(conn, seq="sig-600519-20261030-1")
    assert pushed_today(conn, day=DAY) == 1


def test_pushed_today_excludes_second_segment_and_cards(conn):
    _push(conn, seq="sig-1", segment=1)
    _push(conn, seq="sig-1", segment=2)                 # 第二段：同一 seq 的更新
    _push(conn, kind="confirm_card", seq="confirm-7")
    _push(conn, kind="confirm_result", seq="confirm-result-7")
    # 计数按 seq_id 去重：第二段与第一段同 seq，本来就不额外计数
    assert pushed_today(conn, day=DAY) == 3
    assert pushed_today(conn, exclude_segments=(2,), exclude_kinds=NON_PUSH_KINDS,
                        day=DAY) == 1


def test_pushed_today_channel_filter(conn):
    _push(conn, kind="breakout", seq="a1")
    _push(conn, kind="breakdown", seq="a2")
    _push(conn, kind="agent_note", seq="b1")
    assert pushed_today(conn, channel="A", day=DAY) == 2
    assert pushed_today(conn, channel="B", day=DAY) == 1
    assert pushed_today(conn, day=DAY) == 3


def test_pushed_today_ignores_suppressed(conn):
    _push(conn, status="suppressed", seq="x1")
    _push(conn, status="failed", seq="x2")
    assert pushed_today(conn, day=DAY) == 0


def test_recent_independent_push_skips_second_segments(conn):
    _push(conn, seq="sig-1", segment=2)
    assert recent_independent_push(conn, "600519", minutes=30, now=T0) is None


def test_recent_independent_push_window(conn):
    _push(conn, seq="sig-1", segment=1)
    assert recent_independent_push(conn, "600519", minutes=30, now=T0) is not None
    assert recent_independent_push(conn, "600519", minutes=30,
                                   now=T0 + timedelta(minutes=31)) is None


def test_agent_notes_today_counts_suppressed_too(conn):
    """序号必须单调：被压的条目也要占号，否则后来者会撞同一个 seq_id。"""
    _push(conn, kind="agent_note", seq="n1", status="accepted")
    _push(conn, kind="agent_note", seq="n2", status="suppressed")
    assert agent_notes_today(conn, day=DAY) == 2
    assert channel_b_today(conn, day=DAY) == 1


def test_all_production_calls_pass_explicit_day():
    """`policy` 的默认 day 走 `SystemClock`；拿 FixedClock 的调用方必须显式传 day。

    这条用 AST 静态检查，而不是靠某条用例碰巧覆盖——`test_defaults_to_shanghai_today`
    只测了 SystemClock 分支，永远无法证伪「生产调用忘了传 day」。
    """
    import ast
    from pathlib import Path as _P

    targets = {"channel_b_today": 1, "pushed_today": None, "agent_notes_today": 1}
    offenders = []
    for path in ("agent/attention.py", "agent/push.py", "notify/dispatcher.py",
                 "scripts/accept/batch2.py"):
        tree = ast.parse(_P(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name not in targets:
                continue
            # 位置参数也算（`channel_b_today(conn, today)`）
            has_day = any(k.arg == "day" for k in node.keywords) or len(node.args) >= 2
            if not has_day:
                offenders.append(f"{path}:{node.lineno} {name}()")
    assert offenders == [], f"这些调用没显式传 day：{offenders}"
