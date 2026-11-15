"""`GET /api/agent-log` 与 agent_turn 归档。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from core.clock import FixedClock
from core.config import load_config
from core.db import connect, migrate
from tests.helpers import SH

CFG = load_config(__import__("pathlib").Path("config"))
T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)
DAY = 20261030


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "log.db")
    migrate(conn)
    clock = FixedClock(T0)

    from api.agent_log import register
    from fastapi import FastAPI

    app = FastAPI()
    register(app, lambda: conn, clock, CFG)
    return type("E", (), dict(conn=conn, clock=clock, cfg=CFG,
                              client=TestClient(app)))


def _turn(env, *, decision="silent", why="无事可报", tokens=(100, 50), wake="heartbeat",
          days_ago=0):
    when = env.clock.now() - timedelta(days=days_ago)
    env.conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, why,"
        " fallback, market, tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (when.isoformat(), wake, "d", 1, decision, why, 0, "cn_main", tokens[0], tokens[1]))
    env.conn.commit()


def test_silent_turns_are_listed(env):
    """沉默的回合也要出现——「为什么没说」是这张表的信息主体。"""
    _turn(env, decision="silent", why="波动不足")
    _turn(env, decision="speak", why="跌破箱体")
    body = env.client.get("/api/agent-log").json()
    assert [t["decision"] for t in body["turns"]] == ["silent", "speak"]
    assert body["turns"][0]["why"] == "波动不足"


def test_summary_counts(env):
    for _ in range(3):
        env.conn.execute(
            "INSERT INTO event_log(ts, trade_date, market, kind, scope, target, payload,"
            " salience, escalated, agent_status, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (T0.isoformat(), DAY, "cn_main", "breakout", "stock", "sh600519", "{}", 0.5,
             1, "escalated", f"k{_}"))
    env.conn.commit()
    _turn(env, decision="speak")
    _turn(env, decision="silent")
    s = env.client.get("/api/agent-log").json()["summary"]
    assert s["detected"] == 3 and s["escalated"] == 3
    assert s["spoke"] == 1 and s["silent"] == 1


def test_cost_uses_configured_pricing(env):
    """单价来自 `config/llm.yaml`（不是代码里写死的数字）。"""
    _turn(env, tokens=(1_000_000, 1_000_000))
    s = env.client.get("/api/agent-log").json()["summary"]
    p = CFG.llm.pricing
    expected = p.in_cny_per_mtok + p.out_cny_per_mtok
    assert s["cost_cny"] == pytest.approx(round(expected, 4), abs=1e-4)
    assert s["pricing_is_estimate"] is True


def test_date_filter_selects_other_day(env):
    _turn(env, why="今天的")
    _turn(env, why="昨天的", days_ago=1)
    body = env.client.get("/api/agent-log?date=20261029").json()
    assert [t["why"] for t in body["turns"]] == ["昨天的"]


def test_bad_date_falls_back_to_today(env):
    _turn(env, why="今天的")
    body = env.client.get("/api/agent-log?date=not-a-date").json()
    assert [t["why"] for t in body["turns"]] == ["今天的"]


def test_tool_calls_decoded(env):
    env.conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback,"
        " market, tool_calls) VALUES (?,?,?,?,?,?,?,?)",
        (T0.isoformat(), "event", "d", 2, "speak", 0, "cn_main",
         json.dumps([{"name": "get_quote", "args": {"code": "sh600519"}}])))
    env.conn.commit()
    t = env.client.get("/api/agent-log").json()["turns"][0]
    assert t["tool_calls"][0]["name"] == "get_quote"


# ---- 归档 --------------------------------------------------------------

def test_archive_moves_old_turns_and_keeps_total(env):
    from jobs.archive import run

    _turn(env, days_ago=100)
    _turn(env, days_ago=0)
    before = env.conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0]
    out = run(env.conn, env.clock)
    after = (env.conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0]
             + env.conn.execute("SELECT count(*) FROM agent_turn_archive").fetchone()[0])
    assert out["archived"] == 1 and out["deleted"] == 1
    assert after == before


def test_archive_nulls_memory_reference(env):
    """长期记忆引用 90 天前的回合：直接 DELETE 会 FOREIGN KEY constraint failed。"""
    from jobs.archive import run

    _turn(env, days_ago=100)
    turn_id = env.conn.execute("SELECT max(id) FROM agent_turn").fetchone()[0]
    env.conn.execute(
        "INSERT INTO agent_memory(created_at, scope, kind, content, source_turn_id)"
        " VALUES (?,?,?,?,?)", (T0.isoformat(), "stock", "judgment", "长期判断", turn_id))
    env.conn.commit()
    run(env.conn, env.clock)
    row = env.conn.execute("SELECT source_turn_id FROM agent_memory").fetchone()
    assert row[0] is None
    assert env.conn.execute("SELECT count(*) FROM agent_memory").fetchone()[0] == 1


def test_archive_is_rerunnable(env):
    """重跑（或上次在「插完、删前」崩溃）不能因 UNIQUE 冲突整体失败。"""
    from jobs.archive import run

    _turn(env, days_ago=100)
    turn_id = env.conn.execute("SELECT max(id) FROM agent_turn").fetchone()[0]
    from jobs.archive import _ARCHIVE_COLS

    cols = _ARCHIVE_COLS.split(",")
    snapshot = env.conn.execute(
        f"SELECT {_ARCHIVE_COLS} FROM agent_turn WHERE id = ?", (turn_id,)).fetchone()
    run(env.conn, env.clock)
    # 把**逐列完全相同的一行**再塞回原表，模拟上次在「插完、删前」崩溃。
    # 必须复制全部归档列：少写一列（比如 why 留空）就变成「另一行」，
    # 按全列相等判断时会正确地不删——那是测试造错了数据，不是实现的问题。
    env.conn.execute(
        f"INSERT INTO agent_turn({_ARCHIVE_COLS}) VALUES"
        f" ({','.join('?' * len(cols))})", tuple(snapshot))
    env.conn.commit()
    out = run(env.conn, env.clock)          # 不能抛（原来会 UNIQUE constraint failed）
    # 重跑时这一行「内容与归档表里的完全相同」，归档不动、原表清掉即可
    assert out["deleted"] == 1
    assert env.conn.execute("SELECT count(*) FROM agent_turn_archive").fetchone()[0] == 1
    assert env.conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0] == 0


def test_archive_does_not_drop_reused_id(env):
    """P2-11：`agent_turn.id` 是普通 INTEGER PRIMARY KEY，会被新回合重用。

    归档表里已有同 id 的旧行时，`INSERT OR IGNORE` 会把**新行**悄悄丢掉，
    随后 DELETE 又把它删了——日志直接消失。必须按 (id, ts, decision) 三元组判断。
    """
    from jobs.archive import run

    _turn(env, why="很老的回合", days_ago=100)
    old_id = env.conn.execute("SELECT max(id) FROM agent_turn").fetchone()[0]
    run(env.conn, env.clock)
    # 手工造一个「id 被重用」的新回合（时间也是 100 天前，但内容不同）
    env.conn.execute(
        "INSERT INTO agent_turn(id, ts, wake_kind, context_digest, rounds, decision, why,"
        " fallback, market) VALUES (?,?,?,?,?,?,?,?,?)",
        (old_id, (T0 - timedelta(days=101)).isoformat(), "heartbeat", "d", 1, "silent",
         "另一条被重用的 id", 0, "cn_main"))
    env.conn.commit()

    out = run(env.conn, env.clock)
    archived = env.conn.execute("SELECT why FROM agent_turn_archive WHERE id = ?",
                                (old_id,)).fetchall()
    assert len(archived) == 1 and archived[0][0] == "很老的回合"
    # 归档表 id 是主键，同 id 两行存不下：新行既不归档也不删，必须被报告出来，
    # 不能像原来那样 INSERT OR IGNORE 吞掉 + DELETE 删掉
    kept = env.conn.execute("SELECT why FROM agent_turn").fetchall()
    assert [r[0] for r in kept] == ["另一条被重用的 id"]
    assert out["leftover"] == 1, "未归档的行必须报告，静默丢才是真事故"


def test_archive_keeps_row_that_was_not_fully_archived(env):
    """P2：同 (id, ts, decision, context_digest) 但 `why` 不同的行没被真正搬走。

    只比四列就删 → 一行**从未归档**的日志被静默删除。必须逐列相等才删。
    """
    from jobs.archive import run, _ARCHIVE_COLS

    _turn(env, days_ago=100)
    turn_id = env.conn.execute("SELECT max(id) FROM agent_turn").fetchone()[0]
    run(env.conn, env.clock)
    # 同 id、同 ts、同 decision、同 digest，只有 why 不同
    row = env.conn.execute(
        f"SELECT {_ARCHIVE_COLS} FROM agent_turn_archive WHERE id = ?", (turn_id,)).fetchone()
    cols = _ARCHIVE_COLS.split(",")
    values = list(row)
    values[cols.index(" why")] = "这条没被归档过"
    env.conn.execute(
        f"INSERT INTO agent_turn({_ARCHIVE_COLS}) VALUES ({','.join('?' * len(cols))})",
        tuple(values))
    env.conn.commit()

    out = run(env.conn, env.clock)
    kept = env.conn.execute("SELECT why FROM agent_turn WHERE id = ?", (turn_id,)).fetchall()
    assert [r[0] for r in kept] == ["这条没被归档过"], "没归档的行被删了"
    assert out["leftover"] == 1
