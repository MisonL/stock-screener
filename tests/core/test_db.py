import json
import sqlite3
import threading
import time

import pytest

from core.db import connect, make_conn_factory, migrate, write_retry
from tests.helpers import set_beat


def test_set_beat_upserts_single_row(tmp_db):
    # 心跳是原地更新（三行，不累积）：/api/status 的 services 与 watchdog 都按 service 取最后一条。
    set_beat(tmp_db, "api")
    set_beat(tmp_db, "api", ago_s=120, detail={"calls_by_scope": {"pool": 3}})
    rows = tmp_db.execute("SELECT service, detail FROM service_heartbeat").fetchall()
    assert len(rows) == 1 and rows[0]["service"] == "api"
    assert json.loads(rows[0]["detail"]) == {"calls_by_scope": {"pool": 3}}


def test_connect_sets_required_pragmas(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.row_factory is sqlite3.Row


def test_readonly_connection_can_read_but_not_write(tmp_path):
    path = tmp_path / "t.db"
    migrate(connect(path))
    ro = connect(path, readonly=True)
    assert ro.execute("SELECT count(*) FROM signals").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO job_state(name, value) VALUES ('k', 'v')")


def test_connect_honours_ss_db_path(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "market.db"
    monkeypatch.setenv("SS_DB_PATH", str(target))
    connect().close()
    assert target.exists()


def test_connection_is_usable_from_another_thread(tmp_db):
    # jobs 的 pool/sector 两个快照线程会共用主线程建的连接（check_same_thread=False）。
    seen = []
    thread = threading.Thread(target=lambda: seen.append(tmp_db.execute("SELECT 1").fetchone()[0]))
    thread.start()
    thread.join()
    assert seen == [1]


def test_make_conn_factory_returns_migrated_conn(tmp_path):
    # 计划 Task 24 的 api/main.py 用 make_conn_factory() 装配 create_app：
    # 每次调用都要给出一个已迁移的库，否则 api 首次启动读不存在的表。
    factory = make_conn_factory(tmp_path / "t.db")
    conn = factory()
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0
    assert factory() is not conn  # 每次新连接，不是共享单例


def test_concurrent_migrate_from_two_connections(tmp_path):
    # 首次 `docker compose up` 时 api 与 jobs 两个进程同时 connect(); migrate()：
    # 两边都在事务外读到空版本表、各自拿到写锁后重跑脚本，会撞「table already exists」。
    path = tmp_path / "t.db"
    connect(path).close()  # 先把库文件与 WAL 建好，避免把建库竞争混进本测试
    results = []

    def run():
        c = connect(path)
        results.append(migrate(c))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # **动态列举**而不是写死清单：每加一个迁移文件都要改测试的话，
    # 迟早有人为了让测试变绿而删掉断言。
    from core.db import SCHEMA_DIR
    every = sorted(p.name for p in SCHEMA_DIR.glob("*.sql"))
    assert sorted(results) == [[], [], [], every]  # 只应用一次，其余空跑
    conn = connect(path)
    # 版本表条数 == 磁盘上的迁移文件数（不写死 2：加了 003 就该是 3）
    assert (conn.execute("SELECT count(*) FROM schema_version").fetchone()[0]
            == len(every))


def test_migrate_creates_all_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "t.db")
    first = migrate(conn)
    second = migrate(conn)
    from core.db import SCHEMA_DIR
    every = sorted(p.name for p in SCHEMA_DIR.glob("*.sql"))
    assert first == every and second == []
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"daily_bars", "daily_bars_adj", "corporate_actions", "pool", "event_log", "snapshot_cache",
            "service_heartbeat", "auction_daily", "push_log", "signals"} <= names


def test_event_dedupe_unique(tmp_db):
    sql = ("INSERT INTO event_log(ts, trade_date, market, kind, scope, target, payload, salience, dedupe_key) "
           "VALUES ('2026-10-30T09:45:00+08:00', 20261030, 'cn_main', 'breakout', 'stock', 'sh600519', "
           "'{}', 0.8, 'breakout:sh600519:20261030:1')")
    tmp_db.execute(sql)
    tmp_db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(sql)


def test_write_retry_backs_off(tmp_path):
    path = tmp_path / "t.db"
    conn = connect(path)
    migrate(conn)
    locker = connect(path)
    locker.execute("BEGIN IMMEDIATE")  # 另一个连接持有写锁
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            write_retry(conn, lambda c: c.execute(
                "INSERT INTO job_state(name, value) VALUES ('k', 'v')"))
        assert time.monotonic() - started >= 0.1  # 3 次尝试之间有 2 次 50ms 退避
    finally:
        locker.rollback()
    rows = write_retry(conn, lambda c: c.execute(
        "INSERT INTO job_state(name, value) VALUES ('k', 'v')").rowcount)
    assert rows == 1
    assert conn.execute("SELECT value FROM job_state WHERE name='k'").fetchone()[0] == "v"
