"""Task 12：两段式第二段（同 sequence_id）、通道 B 与共用去重。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.decision import TurnDecision
from agent.push import AgentPusher, action_desc, code6_of
from core.clock import FixedClock
from core.config import load_config
from core.db import connect, migrate
from llm.grounding import GroundedText
from tests.helpers import SH

CFG = load_config(Path("config"))
T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)
DAY = 20261030


class FakeNtfy:
    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    def publish(self, msg):
        if self.fail:
            raise RuntimeError("ntfy 挂")
        self.published.append(msg)
        return f"ntfy-{len(self.published)}"


class Recorder:
    """把 GroundedText 当决策载体：`_append` 优先取 `grounded.text`。"""

    def __init__(self, text):
        self.text = text


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "p.db")
    migrate(conn)
    ntfy = FakeNtfy()
    clock = FixedClock(T0)
    pusher = AgentPusher(conn, ntfy, CFG, clock)
    return SimpleNamespace(conn=conn, ntfy=ntfy, clock=clock, pusher=pusher, cfg=CFG)


def _first_segment(env, *, event_id=1234, seq_id="sig-600519-20261030-1", body=None,
                   kind="breakout", level="P0", status="accepted", code="600519",
                   segment=1, trade_date=DAY):
    body = body or ("📈 贵州茅台 放量突破\n现价 1680.00 ｜ 箱体上沿 1650.00\n"
                    "涨停 33.10 ｜ 距涨停 3.2%")
    cur = env.conn.execute(
        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, event_id,"
        " signal_id, trade_date, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (env.clock.now().isoformat(), code, kind, level, seq_id, segment, body, event_id,
         7, trade_date, status))
    env.conn.commit()
    return cur.lastrowid


def _decision(**kw):
    base = dict(kind="speak", speak=True, level="P1", title="解读标题",
                body="半导体资金流入（get_fund_flow.data.net）")
    base.update(kw)
    return TurnDecision(**base)


# ---- 事件第二段 ----------------------------------------------------------

def test_second_segment_finds_first_by_event_id(env):
    """D2：按 `push_log.event_id` 定位，不解析 dedupe_key。"""
    _first_segment(env, event_id=1234, seq_id="sig-600519-20261030-1")
    out = env.pusher.second_segment(1234, _decision(), Recorder("补充解读"))
    assert out == "sig-600519-20261030-1"
    row = env.conn.execute(
        "SELECT body, seq_id, segment, event_id FROM push_log WHERE segment=2").fetchone()
    assert row[0].startswith("📈 贵州茅台 放量突破")     # 原文一字不改
    assert "\n\n——\n补充解读" in row[0]
    assert row[1] == "sig-600519-20261030-1" and row[3] == 1234


def test_second_segment_keeps_all_first_segment_lines(env):
    """突破模板是 5 行；只发 4 行会丢掉涨停/可买信息（§7.2.2）。"""
    body = "行1\n行2\n行3\n行4\n行5"
    _first_segment(env, event_id=9, body=body)
    env.pusher.second_segment(9, _decision(), Recorder("解读"))
    row = env.conn.execute("SELECT body FROM push_log WHERE segment=2").fetchone()
    for line in ("行1", "行2", "行3", "行4", "行5"):
        assert line in row[0]


def test_second_segment_same_seq_id_two_rows(env):
    _first_segment(env, event_id=1234)
    env.pusher.second_segment(1234, _decision(), Recorder("解读"))
    rows = env.conn.execute("SELECT segment, ts FROM push_log WHERE seq_id=?",
                            ("sig-600519-20261030-1",)).fetchall()
    assert len(rows) == 2


def test_second_segment_picks_the_right_first_segment_for_seq2(env):
    """同 code 同日 seq=2 的通知必须取到它自己的第一段（D2/F4）。"""
    _first_segment(env, event_id=111, seq_id="sig-600519-20261030-1", body="第一段之一")
    _first_segment(env, event_id=222, seq_id="sig-600519-20261030-2", body="第一段之二")
    env.pusher.second_segment(222, _decision(), Recorder("解读"))
    row = env.conn.execute("SELECT body FROM push_log WHERE segment=2").fetchone()
    assert row[0].startswith("第一段之二")


def test_second_segment_skipped_when_first_failed(env):
    """第一段 failed 时第二段会单独出现（同 seq），用户看到一条没有原文的解读。"""
    _first_segment(env, event_id=55, status="failed")
    assert env.pusher.second_segment(55, _decision(), Recorder("解读")) is None
    assert env.conn.execute("SELECT count(*) FROM push_log WHERE segment=2").fetchone()[0] == 0


def test_second_segment_unknown_event_returns_none(env):
    assert env.pusher.second_segment(999999, _decision(), Recorder("解读")) is None


def test_second_segment_signal_id_copied(env):
    _first_segment(env, event_id=1234)
    env.pusher.second_segment(1234, _decision(), Recorder("解读"))
    row = env.conn.execute("SELECT signal_id FROM push_log WHERE segment=2").fetchone()
    assert row[0] == 7


# ---- 开盘简报第二段 ------------------------------------------------------

def test_open_second_segment_inserts_new_row(env):
    """D4：同 seq_id **两行**（不是 UPDATE 同一行），验收口径就是两行 ts 差。"""
    _first_segment(env, event_id=None, seq_id="open-20261030", body="开盘简报正文",
                   kind="open_brief", level="P2")
    out = env.pusher.open_second_segment(DAY, _decision(), Recorder("第二段"))
    assert out == "open-20261030"
    rows = env.conn.execute(
        "SELECT segment, ts, body FROM push_log WHERE seq_id='open-20261030' ORDER BY segment"
    ).fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert rows[1][2].startswith("开盘简报正文")


def test_open_second_segment_no_first_returns_none(env):
    assert env.pusher.open_second_segment(DAY, _decision(), Recorder("x")) is None


# ---- 通道 B --------------------------------------------------------------

def test_agent_note_row_shape(env):
    """`(kind='agent_note', level='P2', segment=1, event_id=触发事件或 NULL)`。

    `channel_b_today` 计数与「对原条发第二段」两处去重都靠这三个字段识别。
    """
    env.pusher.agent_note(_decision(), Recorder("主动说一句"), code="sh600519")
    row = env.conn.execute(
        "SELECT kind, level, segment, event_id, status, trade_date FROM push_log"
        " WHERE kind='agent_note'").fetchone()
    assert tuple(row) == ("agent_note", "P2", 1, None, "accepted", DAY)


def test_agent_note_seq_id_normalises_code(env):
    """`sh600519` 必须归一成 `600519`，与模板 `sig-600519-…` 口径一致。"""
    env.pusher.agent_note(_decision(), Recorder("说"), code="sh600519")
    seq = env.conn.execute("SELECT seq_id FROM push_log WHERE kind='agent_note'").fetchone()[0]
    assert seq == f"note-600519-{DAY}-1"
    assert code6_of("sh600519") == "600519" and code6_of("600519") == "600519"


def test_agent_note_seq_id_variants(env):
    env.pusher.agent_note(_decision(), Recorder("a"), code="sh600519")
    env.pusher.agent_note(_decision(), Recorder("b"), target="pt半导体")
    env.pusher.agent_note(_decision(), Recorder("c"), target="sh000300")
    env.pusher.agent_note(_decision(), Recorder("d"))
    seqs = {r[0] for r in env.conn.execute(
        "SELECT seq_id FROM push_log WHERE kind='agent_note'").fetchall()}
    assert len(seqs) == 4, seqs
    assert all("--" not in s and not s.endswith("-") for s in seqs), seqs


def test_agent_note_cap_does_not_write_push_log(env):
    """超日上限：只写 agent_turn，不写 push_log（否则污染 seq 计数）。"""
    cap = int(CFG.agent.channel_b_daily_cap)
    for i in range(cap):
        env.conn.execute(
            "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (T0.isoformat(), "600519", "agent_note", "P2", f"note-x-{i}", 1, DAY, "accepted"))
    env.conn.commit()
    assert env.pusher.agent_note(_decision(), Recorder("还要说"), code="sh600519") is None
    assert env.conn.execute(
        "SELECT count(*) FROM push_log WHERE kind='agent_note'").fetchone()[0] == cap
    row = env.conn.execute(
        "SELECT decision, why FROM agent_turn ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "speak" and "通道 B 日上限" in row[1]


def test_agent_note_total_cap_counts_only_new_notifications(env):
    """日熔断计的是「新通知」：第二段与确认卡不占额度。"""
    cap = int(CFG.notify.daily_cap)
    for i in range(cap):
        env.conn.execute(
            "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (T0.isoformat(), "600519", "breakout", "P0", f"sig-{i}", 1, DAY, "accepted"))
        env.conn.execute(
            "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (T0.isoformat(), "600519", "breakout", "P0", f"sig-{i}", 2, DAY, "accepted"))
    env.conn.commit()
    assert env.pusher.agent_note(_decision(), Recorder("说"), code="sh600519") is None


def test_channel_b_today_ignores_channel_a(env):
    """当日已有 60 条通道 A 时 channel_b_today 仍为 0（决定 B9）。"""
    for i in range(60):
        env.conn.execute(
            "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (T0.isoformat(), "600519", "breakdown", "P1", f"bd-{i}", 1, DAY, "accepted"))
    env.conn.commit()
    from notify.policy import channel_b_today

    assert channel_b_today(env.conn, DAY) == 0


def test_agent_note_dedupes_to_second_segment(env):
    """刚推过突破的票 10 分钟后 agent 想主动提醒 → 对原条追加，而不是新开一条。"""
    _first_segment(env, event_id=1234, code="600519", kind="breakout",
                   seq_id="sig-600519-20261030-1", body="突破原文")
    env.pusher.agent_note(_decision(), Recorder("追一句"), code="sh600519")
    kinds = [r[0] for r in env.conn.execute(
        "SELECT kind FROM push_log ORDER BY id").fetchall()]
    assert kinds == ["breakout", "breakout"], "不该新开 agent_note"
    row = env.conn.execute("SELECT body FROM push_log WHERE segment=2").fetchone()
    assert row[0].startswith("突破原文") and "追一句" in row[0]


def test_agent_note_falls_back_when_prev_has_no_event_id(env):
    """event_id 为 NULL 的原条必须回落为新 note：`second_segment(None)` 永不命中，
    会把这条提醒无声吞掉且不计入 channel_b_today（§7.2 去重表）。"""
    env.conn.execute(
        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, event_id,"
        " trade_date, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (T0.isoformat(), "600519", "agent_note", "P2", "note-600519-20261030-1", 1,
         "之前的提醒", None, DAY, "accepted"))
    env.conn.commit()
    env.pusher.agent_note(_decision(), Recorder("再说一句"), code="sh600519")
    kinds = [r[0] for r in env.conn.execute(
        "SELECT kind FROM push_log ORDER BY id").fetchall()]
    assert kinds == ["agent_note", "agent_note"]
    from notify.policy import channel_b_today

    assert channel_b_today(env.conn, DAY) == 2


def test_dispatcher_dedupe_is_kind_scoped(env):
    """批1 的去重按 kind：同一票先突破后跌破，跌破是新事实，不能被压掉。

    （`agent_note` 侧相反——它要跨 kind 找「刚说过这只票」，两者不是一回事。）
    """
    from notify.policy import recent_independent_push

    _first_segment(env, event_id=1234, code="600519", kind="breakout")
    hit = recent_independent_push(env.conn, "600519", kind="breakout", minutes=30,
                                  now=env.clock.now())
    miss = recent_independent_push(env.conn, "600519", kind="breakdown", minutes=30,
                                   now=env.clock.now())
    assert hit is not None and miss is None
    assert hit.event_id == 1234


def test_failed_publish_marks_row_failed(env):
    env.ntfy.fail = True
    assert env.pusher.agent_note(_decision(), Recorder("说"), code="sh600519") is None
    row = env.conn.execute(
        "SELECT status, error FROM push_log WHERE kind='agent_note'").fetchone()
    assert row[0] == "failed" and "ntfy" in row[1]


# ---- 确认卡 --------------------------------------------------------------

def test_confirm_card_has_two_actions_and_signature(env):
    env.conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback, market)"
        " VALUES (?,?,?,?,?,?,?)", (T0.isoformat(), "event", "d", 1, "speak", 0, "cn_main"))
    env.conn.commit()
    turn_id = env.conn.execute("SELECT max(id) FROM agent_turn").fetchone()[0]
    out = env.pusher.confirm_card(turn_id, {"tool": "mcp_add_watch", "args": {"code": "sh600519"}})
    assert out == f"confirm-{turn_id}"
    msg = env.ntfy.published[-1]
    assert len(msg.actions) == 2
    http_action = [a for a in msg.actions if a.action == "http"][0]
    assert "?t=" in http_action.url and "exp=" in http_action.url
    row = env.conn.execute("SELECT kind, level, seq_id, body FROM push_log WHERE kind='confirm_card'"
                           ).fetchone()
    assert row[1] == "P2" and row[2] == f"confirm-{turn_id}"
    assert "加入自选" in row[3]
    ap = env.conn.execute("SELECT pending_action, pending_status FROM agent_turn WHERE id=?",
                          (turn_id,)).fetchone()
    assert json.loads(ap[0])["tool"] == "mcp_add_watch" and ap[1] == "pending"


def test_action_desc_labels_and_fallback():
    assert action_desc({"tool": "mcp_add_watch", "args": {"code": "sh600519"}}) == "加入自选 sh600519"
    assert action_desc({"tool": "mcp_set_alert", "args": {"code": "sh600519"}}) == "设置提醒 sh600519"
    # 未知工具不抛：确认卡照发，用户至少能看到是什么
    text = action_desc({"tool": "mcp_unknown", "args": {"x": 1}})
    assert text.startswith("mcp_unknown") and '"x"' in text


def test_confirm_card_expires_with_clock(env):
    env.conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback, market)"
        " VALUES (?,?,?,?,?,?,?)", (T0.isoformat(), "event", "d", 1, "speak", 0, "cn_main"))
    env.conn.commit()
    turn_id = 1
    env.pusher.confirm_card(turn_id, {"tool": "mcp_add_watch", "args": {"code": "sh600519"}})
    url = [a for a in env.ntfy.published[-1].actions if a.action == "http"][0].url
    exp = int(url.split("exp=")[1].split("&")[0])
    ttl = int(CFG.agent.pending_action_ttl_h)
    assert exp == int(T0.timestamp()) + ttl * 3600
