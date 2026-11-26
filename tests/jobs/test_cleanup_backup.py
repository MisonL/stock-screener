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


def test_disk_used_pct_returns_none_on_bad_path():
    """**读不到必须是 `None`，绝不能是 `0.0`。**

    0 的含义是"磁盘很空"，它让 75/85/95 三档全部不触发——
    告警彻底静默，而 `push_log` 里只会留下"磁盘 0%"的假记录。
    这和"口径偏低导致 P0 永不触发"是同一个后果。
    """
    assert disk_used_pct("/definitely/not/here") is None


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


def test_check_disk_alerts_when_unreadable(monkeypatch):
    """读不到磁盘时要发 **P1 disk_unknown**，不能静默。"""
    import jobs.cleanup as cleanup_mod

    class FakeDispatcher:
        def __init__(self):
            self.calls = []

        def alert(self, kind, body, level):
            self.calls.append((kind, level))

    d = FakeDispatcher()
    # **必须 monkeypatch，不能直接赋值模块属性**：直接赋值不会在测试后恢复，
    # 后面的用例会拿到这个假函数——实测出现过"单跑通过、合跑失败"。
    monkeypatch.setattr(cleanup_mod, "disk_used_pct", lambda p: None)
    cleanup_mod.check_disk(None, d, "/nope")
    assert ("disk_unknown", "P1") in d.calls, d.calls


def test_prune_removes_orphan_sidecars_whose_body_is_gone(tmp_path):
    """**主体早已不在的孤儿伴生文件也必须清掉。**

    只删主体时连带的伴生（上一版的做法）只能防未来：如果某次 `_prune`
    在删主体之后、删伴生之前被杀，或者有人手工 `rm market-*.db`，
    留下的 `-shm`/`-wal` 没有主体可依附，`market-*.db` 的 glob 永远
    匹配不到它们——81.4 上那对 32KB 残留就是这个形态。
    """
    from jobs.backup import _prune

    out = tmp_path
    (out / "market-20260101.db").write_bytes(b"x")
    # 只有伴生、没有主体
    (out / "market-20251231.db-shm").write_bytes(b"y")
    (out / "market-20251231.db-wal").write_bytes(b"z")

    _prune(out, keep=1)

    left = sorted(p.name for p in out.iterdir())
    assert left == ["market-20260101.db"], f"孤儿伴生没清掉：{left}"


def test_prune_refuses_keep_zero(tmp_path):
    """**`keep < 1` 必须报错**：`os.replace` 已经把今天这份改好名了，
    此时删全部会让"备份成功"变成"目录里一份都没有"，而调用方看不出来。
    """
    import pytest

    from jobs.backup import _prune

    (tmp_path / "market-20260101.db").write_bytes(b"x")
    with pytest.raises(ValueError, match="keep"):
        _prune(tmp_path, keep=0)
    assert (tmp_path / "market-20260101.db").exists(), "报错前不该先删文件"


def test_api_and_jobs_agree_on_the_same_statvfs(monkeypatch):
    """**两个实现必须对同一块盘给出同一个数。**

    v0.2.130→0.2.131 的真实事故就是"jobs 改了、api 没改"：
    jobs 报 84.7%、api 报 90.1%，运维不知道该信谁。两边各自硬编码
    88.9% 只是巧合式守护；这条把两者**接在同一组 statvfs 数据**上比对。
    """
    import os as _os

    import jobs.cleanup as cleanup_mod
    from api.routes import _disk_used_pct

    class FakeStat:
        f_frsize = 1
        f_blocks = 100 * 2**30
        f_bfree = 20 * 2**30          # 含保留块
        f_bavail = 10 * 2**30         # 不含保留块

    total = FakeStat.f_blocks
    free = FakeStat.f_bavail
    used = total - FakeStat.f_bfree

    class FakeUsage:
        pass

    usage = FakeUsage()
    usage.total = total
    usage.used = used
    usage.free = free

    monkeypatch.setattr(_os, "statvfs", lambda p: FakeStat())
    monkeypatch.setattr("shutil.disk_usage", lambda p: usage)
    monkeypatch.setattr(cleanup_mod.shutil, "disk_usage", lambda p: usage)

    a = cleanup_mod.disk_used_pct("/app/data")
    b = _disk_used_pct("/app/data")
    assert a is not None and b is not None
    assert abs(a - b) < 0.01, f"两个接口口径分叉了：jobs={a:.2f}% api={b:.2f}%"


def _setup(tmp_path, now_str, existing=None):
    """造一个最小库 + backups 目录，返回 (catch_up 结果, backups 目录, conn)。

    `existing`: [(ymd_str, "YYYY-MM-DD HH:MM")] —— 预置的历史备份文件。
    """
    import os
    from datetime import datetime as _dt

    from core.clock import SH, FixedClock
    from core.db import connect, migrate
    from jobs.backup import catch_up_missed_backup

    root = tmp_path / "backups"
    root.mkdir(exist_ok=True)
    for ymd, mtime in (existing or []):
        f = root / f"market-{ymd}.db"
        f.write_bytes(b"x")
        # **必须按 SH 时区解释**：容器 TZ 是 Asia/Shanghai 但 pytest 进程
        # 未必，naive `.timestamp()` 会按本机时区算，偏差正好 8 小时——
        # 那会把"断档 33 小时"变成"断档 25 小时"，测试就测不到真实的漏备。
        ts = _dt.strptime(mtime, "%Y-%m-%d %H:%M").replace(tzinfo=SH).timestamp()
        os.utime(f, (ts, ts))

    conn = connect(tmp_path / "c.db")
    migrate(conn)
    # backup_db 会做 integrity_check，库必须至少有一张有数据的表
    conn.execute(
        "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
        " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean, pos_in_box)"
        " VALUES (20261119,'cn_main','sh600519',1,82.9,'{}',40,10.14,9.86,10.14,9.86,10.04,6e6,0.5)")
    conn.commit()

    clock = FixedClock(_dt.strptime(now_str, "%Y-%m-%d %H:%M"))
    return catch_up_missed_backup(conn, tmp_path, clock), root, conn


def test_catch_up_backup_when_deploy_ate_the_2330_window(tmp_path):
    """**2026-11-24 的真实事故**：部署压在 23:30:14，当晚备份被静默跳过。

    `misfire_grace_time=3600` 只在"调度器在跑但被阻塞"时生效；进程在
    23:30 那一刻**不在**，那次备份就永久丢。原来只补了"陈旧检查"，
    没补"备份本身"。模拟：11-23 有备份，次日 09:00 启动 → 必须立刻补。
    """
    made, root, _ = _setup(tmp_path, "2026-11-25 09:00",
                           existing=[("20261123", "2026-11-23 23:30")])
    assert made is not None, "断档 33 小时后启动，却没有补备份"
    assert made.exists() and made.stat().st_size > 0


def test_catch_up_backup_when_restarting_right_after_2330(tmp_path):
    """**11-24 事故的精确复现**：23:30:14 部署，23:33:12 新容器启动。

    条件①：今天的文件不存在、且已过 23:30 → 立刻补。
    这条和 `..._ate_the_2330_window` 覆盖的是**不同**条件：
    那条走的是条件②（隔天发现已断档），这条走的是条件①（当晚过点）。
    """
    made, root, _ = _setup(tmp_path, "2026-11-24 23:33",
                           existing=[("20261123", "2026-11-23 23:30")])
    assert made is not None, "23:33 重启（错过 23:30），没有补备份"
    assert (root / "market-20261124.db").exists(), "补出来的应叫今天的日期"


def test_catch_up_does_nothing_before_the_window(tmp_path):
    """23:30 之前重启**不该**提前备——否则当晚 23:30 的定时任务被占掉。

    这是条件①的反面：今天还没到点，等定时任务即可。
    """
    made, _root, _ = _setup(tmp_path, "2026-11-25 23:00",
                            existing=[("20261124", "2026-11-24 23:30")])
    assert made is None


def test_catch_up_does_nothing_when_today_already_backed_up(tmp_path):
    """今天已有备份：不许重复备。"""
    made, _root, _ = _setup(tmp_path, "2026-11-25 23:40",
                            existing=[("20261125", "2026-11-25 23:31")])
    assert made is None, "今天已经备过，不该再备"


def test_catch_up_fires_when_the_directory_is_empty(tmp_path):
    """一份备份都没有：必须立刻备（否则 stale 检查会一直报警却没人管）。"""
    made, _root, _ = _setup(tmp_path, "2026-11-25 10:00")
    assert made is not None, "没有任何备份时启动，应该立刻补一份"


def test_disk_alert_is_not_repeated_at_a_steady_waterline(conn):
    """**水位不变就不许反复推**。

    生产实测 2026-11-25：磁盘一整天稳在 80~82%，`disk_p2` 推了 **17 条**，
    每条都是同一句"磁盘水位 82%（75% 以上，建议清理）"。
    `Dispatcher.alert` 的 30 分钟去重只按 `kind + 时间`、不看内容，
    而巡检每 10 分钟一次，于是稳态下不停地推。

    这种推送比不推送更糟——它训练用户忽略告警，真正该升档的那条被划掉。
    """
    from jobs import cleanup as cl

    calls = []

    class D:
        def alert(self, kind, text, level="P1"):
            calls.append((kind, level))

    # 同一个水位连续巡检 6 次（模拟一小时）
    for _ in range(6):
        cl._alert_disk(D(), 82.0, conn)
    assert len(calls) == 1, f"稳态 82% 推了 {len(calls)} 条：{calls}"

    # 涨到 86% 跨档：必须再喊（P1）
    cl._alert_disk(D(), 86.0, conn)
    assert calls[-1] == ("disk_p1", "P1"), calls

    # 同档位小幅波动：闭嘴
    before = len(calls)
    for pct in (86.5, 87.0, 87.9):
        cl._alert_disk(D(), pct, conn)
    assert len(calls) == before, f"同档位小幅波动又推了：{calls[before:]}"

    # 同档位但明显恶化（≥3 个百分点）：再喊一次
    cl._alert_disk(D(), 91.0, conn)
    assert calls[-1] == ("disk_p1", "P1"), calls
    assert len(calls) == before + 1


def test_disk_alert_resets_after_falling_below_the_warning_line(conn):
    """**回到安全线以下要清状态**，否则下次越线会被自己的去抖压掉。"""
    from jobs import cleanup as cl

    calls = []

    class D:
        def alert(self, kind, text, level="P1"):
            calls.append((kind, level))

    cl._alert_disk(D(), 82.0, conn)
    assert len(calls) == 1

    # 清理后回落到 60%：不告警，但状态要清
    cl._alert_disk(D(), 60.0, conn)
    assert len(calls) == 1, "回落到安全线不该告警"
    assert cl._disk_state(conn) == (None, None), "回落之后状态没清"

    # 再次越线：必须能立刻喊（不被上次的 82% 压掉）
    cl._alert_disk(D(), 82.5, conn)
    assert len(calls) == 2, f"再次越线没告警：{calls}"


def test_disk_state_not_recorded_when_alert_was_not_delivered(conn):
    """**没送达就不能记「已告警」**（Codex 评审）。

    `Dispatcher.alert()` 在维护模式或 30 分钟去重时直接返回 False，推送失败
    也只记 `failed`。原来先写状态再发，于是"记成已告警、其实一条都没送达"，
    之后同水位一直被去抖压着——磁盘真涨上去也不会再有第二条。
    """
    from jobs import cleanup as cl

    sent = []

    class NotDelivered:
        """模拟维护模式/去重：一条都没发出去。"""

        def alert(self, kind, text, level="P1"):
            sent.append(kind)
            return False

    for _ in range(3):
        cl._alert_disk(NotDelivered(), 82.0, conn)
    # 三次都尝试了（说明去抖没有压掉重试）
    assert len(sent) == 3, f"未送达后应该每轮都重试，实际只试了 {len(sent)} 次"
    # 但状态没有被记成"已告警"
    assert cl._disk_state(conn) == (None, None), "未送达却记了已告警状态"

    # 送达之后才记
    class Delivered:
        def alert(self, kind, text, level="P1"):
            return True

    cl._alert_disk(Delivered(), 82.0, conn)
    assert cl._disk_state(conn)[0] == "disk_p2"


def test_catch_up_backup_after_midnight_when_last_night_was_missed(tmp_path):
    """**午夜后启动也要补**（Codex 评审）。

    11-24 23:30 备过 → 11-25 晚漏备 → 11-26 00:10 才重启。
    此时"今天"是 11-26、还没到 23:30（条件①不成立），最新备份只陈旧约
    25 小时（条件②的 29 小时也不成立）→ 两个条件都不触发，
    一直等到 11-26 23:30，间隔接近 **48 小时**。

    条件③「上一个已过去的 23:30 之后没有新备份」才能兜住这个形态。
    """
    made, _root, _ = _setup(tmp_path, "2026-11-26 00:10",
                            existing=[("20261124", "2026-11-24 23:30")])
    assert made is not None, "昨天漏备、今天凌晨启动，却没有补备份"


def test_catch_up_backup_is_not_repeated_right_after_tonight_backup(tmp_path):
    """当晚 23:31 刚备完，重启不该再备一次（条件③不能太激进）。"""
    made, _root, _ = _setup(tmp_path, "2026-11-25 23:40",
                            existing=[("20261125", "2026-11-25 23:31")])
    assert made is None, "刚备完又补了一次"


def test_catch_up_backup_runs_when_backup_dir_is_missing(tmp_path):
    """**备份目录不存在也要补**：那正是最需要备份的状态。

    原来 `if not root.is_dir(): return None`，于是"backups 目录被误删"
    或首次部署时永远不补。
    """
    from datetime import datetime as _dt

    from core.clock import FixedClock
    from core.db import connect, migrate
    from jobs.backup import catch_up_missed_backup

    conn = connect(tmp_path / "c.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days,"
        " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean, pos_in_box)"
        " VALUES (20261119,'cn_main','sh600519',1,82.9,'{}',40,10.14,9.86,10.14,9.86,10.04,6e6,0.5)")
    conn.commit()
    assert not (tmp_path / "backups").exists()
    made = catch_up_missed_backup(conn, tmp_path, FixedClock(_dt(2026, 11, 25, 23, 40)))
    assert made is not None, "备份目录不存在时没有补备"
