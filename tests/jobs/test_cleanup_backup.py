"""清理任务（D19）、数据库备份与调度器装配。"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.clock import FixedClock
from core.db import connect, migrate
from jobs.backup import backup_db
from jobs.cleanup import cleanup, disk_used_pct
from tests.helpers import SH

T0 = datetime(2026, 10, 30, 16, 0, tzinfo=SH)


class Disp:
    def __init__(self):
        self.alerts = []

    def alert(self, kind, text, level="P1"):
        self.alerts.append((kind, text, level))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "c.db")
    migrate(c)
    return c


def test_cleanup_prunes_by_column_not_always_ts(conn):
    """`chat_session` 只有 `created_at`：照 `ts` 删会 no such column，整条链第一步就炸。"""
    # chat_session / chat_message 由 002_batch2.sql 建好，用例只灌数据
    conn.executemany("INSERT INTO chat_session(created_at) VALUES (?)",
                     [((T0 - timedelta(days=100)).isoformat(),), (T0.isoformat(),)])
    # chat_message.session_id 有外键：必须先有 session
    conn.executemany("INSERT INTO chat_message(session_id, ts, role, content) VALUES (1, ?, 'user', 'x')",
                     [((T0 - timedelta(days=100)).isoformat(),), (T0.isoformat(),)])
    conn.commit()

    out = cleanup(conn, FixedClock(T0))
    assert out["chat_session"] == 1 and out["chat_message"] == 1
    assert conn.execute("SELECT count(*) FROM chat_session").fetchone()[0] == 1


def test_cleanup_skips_missing_tables_and_still_checks_disk(conn):
    # 把批2 的表全删掉，模拟「批1 单跑」：清理必须跳过而不是抛 no such table
    for table in ("chat_session", "chat_message", "agent_schedule"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    out = cleanup(conn, FixedClock(T0), data_dir="/tmp")
    assert out["chat_session"] == -1 and out["agent_schedule"] == -1
    assert out["disk_used_pct"] >= 0                     # 表不存在也要算磁盘


def test_cleanup_prunes_upstream_calls_to_two_hours(conn):
    clock = FixedClock(T0)
    conn.execute("INSERT INTO upstream_calls(ts, scope) VALUES (?, 'pool')",
                 ((T0 - timedelta(hours=3)).isoformat(),))
    conn.execute("INSERT INTO upstream_calls(ts, scope) VALUES (?, 'market')",
                 ((T0 - timedelta(minutes=30)).isoformat(),))
    conn.commit()

    cleanup(conn, clock)
    rows = [r[0] for r in conn.execute("SELECT scope FROM upstream_calls")]
    assert rows == ["market"]


def test_cleanup_cancels_stale_pending_schedule(conn):
    conn.execute("INSERT INTO agent_schedule(created_at, due_at, market, note, status)"
                 " VALUES ('x', ?, 'cn_main', 'n', 'pending')",
                 ((T0 - timedelta(days=5)).isoformat(),))
    conn.commit()

    out = cleanup(conn, FixedClock(T0))
    # 先 UPDATE 成 cancelled（留痕），再被 DELETE 清掉——所以断言的是「两段都跑到了」
    assert out["agent_schedule.cancelled"] == 1
    assert out["agent_schedule"] == 1
    assert conn.execute("SELECT count(*) FROM agent_schedule").fetchone()[0] == 0


def test_disk_alert_thresholds(conn, monkeypatch):
    import jobs.cleanup as C

    monkeypatch.setattr(C, "disk_used_pct", lambda p: 96.0)
    disp = Disp()
    C.cleanup(conn, FixedClock(T0), dispatcher=disp)
    assert disp.alerts and disp.alerts[0][2] == "P0"

    monkeypatch.setattr(C, "disk_used_pct", lambda p: 88.0)
    disp2 = Disp()
    C.cleanup(conn, FixedClock(T0), dispatcher=disp2)
    assert disp2.alerts[0][2] == "P1"

    monkeypatch.setattr(C, "disk_used_pct", lambda p: 10.0)
    disp3 = Disp()
    C.cleanup(conn, FixedClock(T0), dispatcher=disp3)
    assert disp3.alerts == []


def test_disk_used_pct_returns_zero_on_bad_path():
    assert disk_used_pct("/definitely/not/here") == 0.0


# ---- 备份 ----------------------------------------------------------------

def test_backup_writes_integral_snapshot(tmp_path):
    conn = connect(tmp_path / "m.db")
    migrate(conn)
    conn.execute("INSERT INTO stock_meta(code,name,market,updated_at) VALUES ('sh600519','茅台','cn_main','x')")
    conn.commit()

    path = backup_db(conn, tmp_path, clock=FixedClock(T0))
    assert path is not None and path.name == "market-20261030.db"
    restored = sqlite3.connect(path)
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT name FROM stock_meta").fetchone()[0] == "茅台"
    assert not list((tmp_path / "backups").glob("*.tmp"))


def test_backup_failed_integrity_keeps_old(tmp_path):
    conn = connect(tmp_path / "m.db")
    migrate(conn)
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    old = out_dir / "market-20261029.db"
    old.write_bytes(b"old-backup")

    bad = sqlite3.connect(out_dir / ".market-20261030.db.tmp")
    bad.execute("CREATE TABLE t(x)")
    bad.commit()
    bad.close()

    # 让 integrity_check 返回非 ok：换成一份损坏文件
    (out_dir / ".market-20261030.db.tmp").write_bytes(b"not a database")
    assert backup_db(conn, tmp_path, clock=FixedClock(T0)) is None
    assert old.read_bytes() == b"old-backup"              # 旧备份没被动
    assert not (out_dir / "market-20261030.db").exists()


def test_backup_keeps_only_seven(tmp_path):
    conn = connect(tmp_path / "m.db")
    migrate(conn)
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    for day in range(1, 10):
        (out_dir / f"market-2026100{day}.db").write_bytes(b"x")

    backup_db(conn, tmp_path, keep=7, clock=FixedClock(T0))
    left = sorted(p.name for p in out_dir.glob("market-*.db"))
    assert len(left) == 7 and left[-1] == "market-20261030.db"


def test_cleanup_deletes_messages_of_old_session(conn):
    """90 天前的会话仍挂着新消息时必须连消息一起删，不能撞外键让整条清理链中断。"""
    conn.execute("INSERT INTO chat_session(id, created_at) VALUES (1, ?)",
                 ((T0 - timedelta(days=100)).isoformat(),))
    conn.execute("INSERT INTO chat_message(session_id, ts, role, content) VALUES (1, ?, 'user', 'x')",
                 (T0.isoformat(),))
    conn.commit()

    out = cleanup(conn, FixedClock(T0))
    # chat_message 那一轮的 rowcount 只算按 ts 删掉的；随会话级联删掉的记在 session 名下
    assert out["chat_session"] == 1
    assert conn.execute("SELECT count(*) FROM chat_message").fetchone()[0] == 0


def test_cleanup_survives_missing_chat_message_table(conn):
    """批1 单跑：chat_session 在、chat_message 不存在时整条清理链不能中断（D19 坑 2）。"""
    from jobs.cleanup import cleanup

    conn.execute("DROP TABLE chat_message")                 # 模拟只有 001 的库
    conn.execute("INSERT INTO chat_session(id, created_at) VALUES (1, '2020-01-01T00:00:00+08:00')")
    conn.commit()
    out = cleanup(conn, FixedClock(T0))
    assert out["chat_session"] == 1            # 父表照删，且没因缺表中断
    assert out["push_log"] >= 0


def test_backup_rejects_snapshot_failing_integrity_check(tmp_path, monkeypatch):
    """**备份完成后** integrity_check 返回非 ok 时，不能改名覆盖旧备份。

    现有用例的「坏备份」在 backup 之前就已经坏了（connect 直接抛），
    只覆盖到 sqlite3.Error 分支；这一条注入的是「copy 成功但校验失败」。
    """
    import jobs.backup as backup_mod

    conn = connect(tmp_path / "m.db")
    migrate(conn)
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    old = out_dir / "market-20261029.db"
    old.write_bytes(b"old")

    real_connect = sqlite3.connect

    class FakeDest(sqlite3.Connection):
        """真实连接，但 integrity_check 恒返回损坏——模拟「copy 成功、校验失败」。"""

        def execute(self, sql, *a):
            if "integrity_check" in sql:
                return SimpleNamespace(fetchone=lambda: ("*** in database main ***",))
            return super().execute(sql, *a)

    monkeypatch.setattr(backup_mod.sqlite3, "connect",
                        lambda *a, **k: real_connect(*a, **k, factory=FakeDest))
    assert backup_db(conn, tmp_path, clock=FixedClock(T0)) is None
    assert old.read_bytes() == b"old"                     # 旧备份没被覆盖
    assert not (out_dir / "market-20261030.db").exists()


def test_cleanup_reclaims_zombie_pending(conn):
    """认领后崩溃留下的 pending 行会永久占用当日熔断额度，必须回收。"""
    from jobs.cleanup import cleanup

    old = (T0 - timedelta(minutes=30)).isoformat()
    fresh = (T0 - timedelta(minutes=1)).isoformat()
    for ts in (old, fresh):
        conn.execute("INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
                     " VALUES (?, NULL, 'breakout', 'P1', ?, 1, 20261030, 'pending')", (ts, ts))
    conn.commit()
    out = cleanup(conn, FixedClock(T0))
    assert out["push_log.zombie"] == 1
    rows = {r[0]: r[1] for r in conn.execute("SELECT seq_id, status FROM push_log")}
    assert rows[old] == "failed" and rows[fresh] == "pending"     # 新鲜的 pending 不动


def test_cleanup_runs_optimize_to_refresh_statistics(conn, tmp_path):
    """cleanup 必须刷新优化器统计（`PRAGMA optimize`）。

    实测：`daily_bars` 有 816 万行、`WITHOUT ROWID`，从没跑过 ANALYZE，
    优化器因此对选择性一无所知，选池那条聚合查询走全表 SCAN，耗时 3570ms；
    补上统计后同一查询降到 1009ms（3.5 倍），且**不需要新索引**
    （实测给 WITHOUT ROWID 表加二级索引反而更慢：41 秒）。

    `PRAGMA optimize` 是 SQLite 官方推荐的轻量做法：只在统计过期时做必要的工作，
    不像全库 `ANALYZE` 那样每次都扫一遍。
    """

    class Recorder:
        """记录 execute 调用；`sqlite3.Connection.execute` 是只读属性，无法直接打桩。"""

        def __init__(self, real):
            self._real = real
            self.calls: list[str] = []

        def execute(self, sql, *a, **k):
            self.calls.append(str(sql))
            return self._real.execute(sql, *a, **k)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

    rec = Recorder(conn)
    cleanup(rec, FixedClock(T0), data_dir=str(tmp_path))
    assert any("optimize" in c.lower() for c in rec.calls), (
        "cleanup 没有跑 PRAGMA optimize：优化器统计会一直是空的，选池查询继续全表扫。"
        f"实际执行的语句里没有它（共 {len(rec.calls)} 条）")
