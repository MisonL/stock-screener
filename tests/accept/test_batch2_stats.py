"""Task 15：第二批自动统计脚本的口径（F-33/34/35）。

每一条都对着一个「写错了指标永远绿」的失败模式：
- 分母用「所有通道 A 事件」→ 合并条被判成缺陷；
- 时延用文本相减 → SQLite 里恒为 0；
- 不排除 shadow 行 → 观测期数据混进正式统计。
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.config import load_config
from core.db import connect, migrate
from tests.helpers import SH

CFG = load_config(Path("config"))
T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)
DAY = 20261030


def _load():
    path = Path("scripts/accept/batch2.py")
    spec = importlib.util.spec_from_file_location("batch2_accept", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _load()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "acc.db")
    migrate(c)
    return c


def _event(conn, eid, *, kind="breakout", status="done", ts=None, salience=0.6):
    conn.execute(
        "INSERT INTO event_log(id, ts, trade_date, market, kind, scope, target, payload,"
        " salience, escalated, agent_status, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, (ts or T0).isoformat(), DAY, "cn_main", kind, "stock", f"sh{600000 + eid}",
         "{}", salience, 1 if status == "done" else 0, status, f"k{eid}"))
    conn.commit()


def _turn(conn, *, wake="event", decision="speak", rounds=1, ts=None):
    conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback, market)"
        " VALUES (?,?,?,?,?,?,?)",
        ((ts or T0).isoformat(), wake, "d", rounds, decision, 0, "cn_main"))
    conn.commit()


def _push(conn, *, event_id, segment=1, seq=None, ts=None, status="accepted",
          kind="breakout", seed=0):
    conn.execute(
        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, event_id,"
        " trade_date, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ((ts or T0).isoformat(), "600519", kind, "P1",
         seq or f"sig-{event_id}-{seed}", segment, "正文", event_id, DAY, status))
    conn.commit()


# ---- rounds 门槛 ---------------------------------------------------------

def test_rounds_ratio_and_mean():
    stats = B.round_stats([{"wake_kind": "event", "rounds": 1, "decision": "speak"}] * 9
                          + [{"wake_kind": "event", "rounds": 3, "decision": "speak"}])
    assert stats["one_round_ratio"] == 0.9
    assert stats["mean_rounds"] == 1.2


def test_non_event_turns_not_counted():
    stats = B.round_stats([{"wake_kind": "heartbeat", "rounds": 6, "decision": "speak"},
                           {"wake_kind": "event", "rounds": 1, "decision": "speak"}])
    assert stats["n"] == 1 and stats["one_round_ratio"] == 1.0


def test_zero_round_audit_rows_excluded_from_rounds():
    """`deduped`/`shadow`/`gave_up` 是注意力层在调模型前写的审计行，
    算进分母会把「一轮收场」摊薄（实测 0.90 → 0.8696 判不合格）。"""
    kept = [{"wake_kind": "event", "rounds": 1, "decision": "speak"}] * 18
    audit = [{"wake_kind": "event", "rounds": 0, "decision": "deduped"}] * 3
    assert B.round_stats(kept + audit)["one_round_ratio"] == 1.0


# ---- 分母口径 ------------------------------------------------------------

def test_denominator_is_events_with_accepted_first_segment(conn):
    """合并条只给批次首事件写 push_log：拿全量事件做分母会把「故意合并」判成缺陷。"""
    for eid in (1, 2, 3):
        _event(conn, eid)
    _push(conn, event_id=1)
    assert [f["event_id"] for f in B.first_segment_events(conn, DAY)] == [1]


def test_suppressed_events_not_in_denominator(conn):
    _event(conn, 1)
    _event(conn, 2)
    _push(conn, event_id=1)
    _push(conn, event_id=2, status="suppressed")
    assert len(B.first_segment_events(conn, DAY)) == 1


# ---- 时延必须用 julianday ------------------------------------------------

def test_latency_uses_julianday_not_text_subtraction(conn):
    """文本相减在 SQLite 里恒为 0，指标永远绿——最容易写错的一条。"""
    _event(conn, 1)
    _push(conn, event_id=1, segment=1, ts=T0)
    _push(conn, event_id=1, segment=2, ts=T0 + timedelta(seconds=12))
    second = B.second_segment_ratio(conn, DAY, B.first_segment_events(conn, DAY))
    assert second["pairs"][0]["latency_s"] == pytest.approx(12.0, abs=0.1)
    assert B.latency_stats(second)["ok"] == 1


def test_latency_over_25s_is_slow(conn):
    _event(conn, 1)
    _push(conn, event_id=1, segment=1, ts=T0)
    _push(conn, event_id=1, segment=2, ts=T0 + timedelta(seconds=26))
    stats = B.latency_stats(
        B.second_segment_ratio(conn, DAY, B.first_segment_events(conn, DAY)))
    assert stats["ok"] == 0 and stats["slow"][0]["event_id"] == 1


def test_latency_denominator_is_actual_sample_not_hardcoded_20(conn):
    """40 对里只有 15 对及时：拿 20 当分母会报「15/20 通过」——不合格显示成合格。"""
    for eid in range(1, 41):
        _event(conn, eid)
        _push(conn, event_id=eid, seq=f"sig-{eid}", ts=T0)
        if eid <= 15:
            _push(conn, event_id=eid, segment=2, seq=f"sig-{eid}",
                  ts=T0 + timedelta(seconds=5))
        else:
            _push(conn, event_id=eid, segment=2, seq=f"sig-{eid}",
                  ts=T0 + timedelta(seconds=60))
    stats = B.latency_stats(
        B.second_segment_ratio(conn, DAY, B.first_segment_events(conn, DAY)))
    assert stats["ok"] == 15 and stats["total"] == 40
    assert stats["ratio"] == pytest.approx(0.375)


def test_missing_second_segment_counts_as_slow(conn):
    """缺第二段没有 latency，但显然不在 25 秒内，不能漏出 slow 列表。"""
    _event(conn, 1)
    _push(conn, event_id=1, segment=1, ts=T0)
    stats = B.latency_stats(
        B.second_segment_ratio(conn, DAY, B.first_segment_events(conn, DAY)), conn)
    assert stats["ok"] == 0
    assert stats["slow"][0]["missing_second"] is True


# ---- 缺第一段的归因 ------------------------------------------------------

def test_missing_first_segment_flagged(conn):
    _event(conn, 1)
    _event(conn, 2, status="suppressed")          # 被压掉的不算缺
    assert B.missing_first_segment_reasons(conn, DAY, [])["missing"] == [1]


# ---- 报告与退出码 --------------------------------------------------------

def test_report_fails_when_thresholds_missed(conn):
    for eid in (1, 2, 3, 4):
        _event(conn, eid)
        _push(conn, event_id=eid)
        _turn(conn, rounds=3)                     # 平均轮数超标
    report, failures = B.run(conn, DAY, CFG)
    assert report["rounds"]["mean_rounds"] == 3.0
    assert any("平均轮数" in f for f in failures)


def test_report_passes_on_clean_day(conn):
    for eid in range(1, 21):
        _event(conn, eid)
        _push(conn, event_id=eid)
        _push(conn, event_id=eid, segment=2, ts=T0 + timedelta(seconds=10))
        _turn(conn, rounds=1)
    report, failures = B.run(conn, DAY, CFG)
    assert failures == []
    assert report["second_segment"]["ratio"] == 1.0
    assert report["latency"]["ok"] == 20


def test_shadow_rows_excluded_from_report(conn):
    _turn(conn, rounds=1)
    _turn(conn, rounds=6, decision="shadow", wake="heartbeat")
    assert all(e["decision"] != "shadow" for e in B._events_for_day(conn, DAY, False))


def test_exclude_drill_filters_wake_kinds(conn):
    _turn(conn, wake="event", rounds=1)
    _turn(conn, wake="open", rounds=6)
    _turn(conn, wake="close", rounds=6)
    assert len(B._events_for_day(conn, DAY, False)) == 3
    assert len(B._events_for_day(conn, DAY, True)) == 1


# ---- 直方图 --------------------------------------------------------------

def test_salience_histogram_bins(conn):
    for i, (sal, status) in enumerate([(0.12, "suppressed"), (0.13, "done"),
                                       (0.52, "escalated"), (0.99, "done")]):
        conn.execute(
            "INSERT INTO event_log(id, ts, trade_date, market, kind, scope, target, payload,"
            " salience, escalated, agent_status, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (i + 1, T0.isoformat(), DAY, "cn_main", "index_move", "market", "sh000001",
             "{}", sal, 0, status, f"h{i}"))
    conn.commit()
    bins, thresholds = B.salience_histogram(conn, DAY, CFG)
    assert [b["lo"] for b in bins] == [0.1, 0.5, 0.95]
    assert bins[0]["n"] == 2 and bins[0]["suppressed"] == 1 and bins[0]["allowed"] == 1
    assert "salience_min" in thresholds


# ---- 计划 Task 15 Step 2 的三处契约 ---------------------------------------

def test_open_brief_paired_by_seq_id(conn):
    """开盘简报没有 event_id，必须按 `open-{trade_date}` 配对。

    只按 event_id 配时它永远配不上，「开盘简报第二段」这条指标被静默漏掉。
    """
    T = "20261030"
    _push(conn, event_id=None, seq=f"open-{T}", kind="open_brief", ts=T0)
    _push(conn, event_id=None, seq=f"open-{T}", kind="open_brief", seed=1,
          segment=2, ts=T0 + timedelta(seconds=8))
    pair = B.open_brief_pair(conn, DAY)
    assert pair is not None and pair["latency_s"] == pytest.approx(8.0, abs=0.1)
    second = B.second_segment_ratio(conn, DAY, [])
    assert second["open_brief_paired"] is True
    # **开盘简报不进事件分母**：它是每日固定一条，混进去会让事件口径失真
    assert second["denominator"] == 0
    assert second["open_brief"]["latency_s"] == pytest.approx(8.0, abs=0.1)


def test_open_brief_missing_second_segment_is_reported(conn):
    _push(conn, event_id=None, seq=f"open-{DAY}", kind="open_brief", ts=T0)
    second = B.second_segment_ratio(conn, DAY, [])
    assert second["open_brief"]["seg2_ts"] is None
    assert second["denominator"] == 0          # 事件分母不受影响


def test_slow_entries_carry_decision(conn):
    """超时条目要带 decision：`timeout` 是硬截止，`speak` 是模型慢/排队积压。"""
    _event(conn, 1)
    _push(conn, event_id=1, segment=1, ts=T0)
    _push(conn, event_id=1, segment=2, ts=T0 + timedelta(seconds=40))
    conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, trigger_id, context_digest, rounds, decision,"
        " fallback, market) VALUES (?,?,?,?,?,?,?,?)",
        (T0.isoformat(), "event", 1, "d", 1, "timeout", 0, "cn_main"))
    conn.commit()
    stats = B.latency_stats(
        B.second_segment_ratio(conn, DAY, B.first_segment_events(conn, DAY)), conn)
    assert stats["slow"][0]["decision"] == "timeout"


def test_denominator_listed_per_exception_class(conn):
    """三类例外必须单列：去重 suppressed、熔断 suppressed、被合并的非首事件。"""
    _event(conn, 1)
    _event(conn, 2, status="suppressed")
    _event(conn, 3)                     # 合并批的成员：有 event_id 但没自己的 push_log
    _push(conn, event_id=1, seq="sig-1")
    _push(conn, event_id=1, seq="sig-1", segment=2)
    out = B.exceptions(conn, DAY)
    assert out["dedupe_or_cap_suppressed"] == [2]
    assert out["merged_members"] == []        # 没有批次证据就不能算合并
    assert out["missing_first_segment"] == [3]


def test_merged_batch_members_need_batch_evidence(conn):
    """同一 `seq_id` 上有别的成员，才算「被合并」；否则是真缺段。"""
    for eid in (1, 2, 3):
        _event(conn, eid)
    # 一个批次：seq=sig-9 下有 1、2、3 三条第一段
    for eid in (1, 2, 3):
        _push(conn, event_id=eid, seq="sig-9")
    out = B.exceptions(conn, DAY)
    assert out["missing_first_segment"] == []   # 三条都有自己的第一段


def test_push_suppressed_with_done_event_is_not_merge(conn):
    """生产里推送层写 `suppressed`、事件是 `done`：这是去重/熔断，不是合并成员。"""
    _event(conn, 7)
    _push(conn, event_id=7, seq="sig-7", status="suppressed")
    out = B.exceptions(conn, DAY)
    assert 7 in out["dedupe_or_cap_suppressed"]
    assert out["merged_members"] == []
