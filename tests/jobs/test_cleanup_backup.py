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


def test_cleanup_alarm_day_cutoff_matches_sweep(tmp_path):
    """**cleanup 与 `sweep_stale_alarms()` 必须用同一个"过期"定义**（评审 L-b）。

    上一版 cleanup 用 `due_at < now - 3d`（满 72 小时），sweep 用"日期更早"。
    两条口径**不是同一个集合**：`date(due_at) < date(now-3d)` 严格包含于
    `due_at < now-3d`——前者以零点为界，后者以当前时刻为界。于是
    「3 天前 05:00 到期、now 是 10:00」这种样本，旧口径删、新口径留，
    同一批数据在两条路径上得出不同答案，排查时对不上。

    现有 `test_cleanup_cancels_stale_pending_schedule` 用 5 天前的数据，
    两种口径都过，所以抓不到这个差异。这里构造**恰好卡在两者之间**的样本：
    到期时间 3 天前的 05:00（按日期算就是第 3 天，按 72 小时算已过期 5 小时）。
    """
    now = datetime(2026, 11, 19, 10, 0, tzinfo=SH)
    conn = connect(tmp_path / "c.db")
    migrate(conn)

    edge = datetime(2026, 11, 16, 5, 0, tzinfo=SH)
    # 前提：两种口径在这个样本上结论相反
    assert (now - edge) > timedelta(days=3), "样本没落在 72 小时之外"
    assert edge.date().isoformat() == (now - timedelta(days=3)).date().isoformat(), \
        "样本没卡在同一个自然日上"

    far = datetime(2026, 11, 14, 14, 0, tzinfo=SH)      # 真正的陈货，两条口径都该删
    recent = datetime(2026, 11, 18, 14, 0, tzinfo=SH)   # 昨天的，都该留
    for due, note in ((edge, "边界样本"), (far, "四天前的"), (recent, "昨天的")):
        conn.execute(
            "INSERT INTO agent_schedule(created_at, due_at, market, target, note, status)"
            " VALUES (?,?,?,?,?,?)",
            (due.isoformat(), due.isoformat(), "cn_main", "sh600519", note, "cancelled"))
    conn.commit()

    cleanup(conn, FixedClock(now), data_dir=str(tmp_path))

    notes = {r[0] for r in conn.execute("SELECT note FROM agent_schedule")}
    assert "四天前的" not in notes, "真正的陈货没被删"
    assert "昨天的" in notes, "3 天之内的被误删了"
    assert "边界样本" in notes, (
        "边界样本被删了：cleanup 用的仍是 '满 72 小时' 口径，"
        "与 sweep_stale_alarms 的「按日期」口径不一致")


def test_disk_used_pct_matches_df_not_used_over_total(monkeypatch):
    """**必须与 `df` 同口径**（`used/(used+free)`），不是 `used/total`。

    ext4 默认给 root 保留 5%。`total` 把保留区算进分母，于是同一个磁盘
    本函数报 80.3%、`df` 报 84.6%——差 4.3 个百分点。

    后果不是数字难看，而是**告警永远按时不触发**：P1（85%）要等 df 到 89.5%，
    P0（95%）要等 df 到 100.2%——磁盘写满时 P0 还没到线。
    """
    import shutil as _sh

    usage = _sh.disk_usage
    # 模拟一块 100G 的盘：used=80G，非 root 可用 free=10G，另 10G 是保留区
    class Fake:
        total = 100 * 2**30
        used = 80 * 2**30
        free = 10 * 2**30
    monkeypatch.setattr(_sh, "disk_usage", lambda p: Fake())

    got = disk_used_pct("/")
    assert abs(got - 88.9) < 0.2, f"应该是 used/(used+free)=88.9%，实际 {got:.1f}%"
    # 用旧公式（used/total）会得到 80.0%——必须**不等于**它
    assert abs(got - 80.0) > 5, f"还在用 used/total：{got:.1f}%"


def test_prune_removes_sidecar_files_of_deleted_backups(tmp_path):
    """**伴生文件必须跟主体一起删**。

    `market-*.db` 这个 glob **匹配不到** `market-20261121.db-shm`
    （要求以 `.db` 结尾），所以主体删掉后伴生会永远留着。
    实测 81.4 上就有一对 `-shm`(32KB)/`-wal`(0B)，`_prune` 跑了几个月没碰。
    单看体积小，但每天一份、永不回收 = 无界增长。
    """
    from jobs.backup import _prune

    out = tmp_path
    for d in ("20261101", "20261102", "20261103"):
        (out / f"market-{d}.db").write_bytes(b"x")
        (out / f"market-{d}.db-shm").write_bytes(b"x")
        (out / f"market-{d}.db-wal").write_bytes(b"x")

    _prune(out, keep=1)

    left = sorted(p.name for p in out.iterdir())
    # 只剩最新那一份（主体 + 它的伴生）。旧的两份连同伴生全部消失。
    assert left == ["market-20261103.db", "market-20261103.db-shm",
                   "market-20261103.db-wal"], left
    # 最新那份的伴生**必须还在**（删了会破坏正在用的归档）
    # 最新那份的伴生**必须还在**（主体还在，删伴生等于破坏归档）
    assert (out / "market-20261103.db-shm").exists()
    assert (out / "market-20261103.db-wal").exists()
