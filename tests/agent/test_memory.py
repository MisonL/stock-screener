"""Task 8：记忆检索与写入（`agent_memory`）。"""
from datetime import datetime, timedelta

import pytest

from agent import memory
from core.clock import FixedClock
from tests.helpers import SH

T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)


@pytest.fixture
def conn(tmp_path):
    from core.db import connect, migrate

    c = connect(tmp_path / "m.db")
    migrate(c)
    return c


def _add(conn, *, target="sh600519", scope="stock", content="判断", created=None,
         expires=None, kind="judgment"):
    return memory.write(conn, kind=kind, scope=scope, target=target, content=content,
                        turn_id=None, expires_at=expires,
                        created_at=(created or T0).isoformat())


def test_recall_newest_first_and_capped(conn):
    for i in range(5):
        _add(conn, content=f"第{i}条", created=T0 + timedelta(minutes=i))
    rows = memory.recall(conn, "sh600519", T0 + timedelta(hours=1))
    assert len(rows) == 3
    assert [r.content for r in rows] == ["第4条", "第3条", "第2条"]


def test_recall_skips_superseded(conn):
    old = _add(conn, content="旧判断")
    new = _add(conn, content="新判断", created=T0 + timedelta(minutes=1))
    memory.supersede(conn, old, new)
    rows = memory.recall(conn, "sh600519", T0 + timedelta(hours=1))
    assert [r.content for r in rows] == ["新判断"]


def test_recall_skips_expired(conn):
    _add(conn, content="过期", expires=(T0 - timedelta(days=1)).isoformat())
    _add(conn, content="长期")
    rows = memory.recall(conn, "sh600519", T0)
    assert [r.content for r in rows] == ["长期"]


def test_recall_filters_by_target_and_scope(conn):
    _add(conn, target="sh600519", scope="stock", content="个股")
    _add(conn, target="半导体", scope="sector", content="板块")
    assert [r.content for r in memory.recall(conn, "sh600519", T0)] == ["个股"]
    assert [r.content for r in memory.recall(conn, "半导体", T0, scope="sector")] == ["板块"]


def test_supersede_does_not_delete(conn):
    old = _add(conn, content="旧")
    new = _add(conn, content="新")
    memory.supersede(conn, old, new)
    assert conn.execute("SELECT count(*) FROM agent_memory").fetchone()[0] == 2
    row = conn.execute("SELECT superseded_by FROM agent_memory WHERE id=?", (old,)).fetchone()
    assert row[0] == new
