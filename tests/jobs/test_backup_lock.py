import fcntl
import sqlite3
from datetime import datetime

import pytest

from jobs.backup import BackupBusy, backup_db


class _Clock:
    def now(self):
        return datetime(2026,11,21,23,30)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    return conn


def test_concurrent_backup_raises_busy_and_touches_nothing(tmp_path):
    """**两个备份进程不能同时写同一个 tmp**（v0.2.113，Codex复核）。

    临时文件名由日期决定，两个进程同时跑会写同一个
    `.market-YYYYMMDD.db.tmp`：内容交错，`integrity_check`可能照样通过——
    结果是一份"看着正常、其实混了两个事务"的备份。

    `APScheduler`的 `max_instances=1`只管**同一个调度器实例**，管不了
    "旧容器还没退、新容器已经起"的部署重叠窗口。
    """
    data = tmp_path / "data"
    out = data / "backups"
    out.mkdir(parents=True)
    conn = _db()

    with open(out / ".backup.lock", "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BackupBusy):
            backup_db(conn, data, clock=_Clock())

    assert not list(out.glob(".market-*.db.tmp")), "锁被占用时还留下了半截 tmp"
    assert not list(out.glob("market-*.db")), "锁被占用时仍然生成了正式备份"

    #锁释放后应当能正常备份（证明拒绝的是“并发”而不是“永远坏掉”）
    got = backup_db(conn, data, clock=_Clock())
    conn.close()
    assert got is not None and got.exists(), "锁释放后备份反而做不了"


def test_busy_backup_does_not_alert_p0(tmp_path):
    """**并发跳过不能报 P0**（v0.2.114，行为测试）。

    上一版用 `inspect.getsource`做文本匹配——评审的变异 H证明它放过了一个
    把 `except Exception`挪到 `except BackupBusy`前面的改动（子类被父类先
    接住，行为已经退回发 P0），**测试照样全绿**。

    这里换成行为：拿住锁 →调真实的 `backup_job` →断言 dispatcher一条告警
    都没有。
    """
    import fcntl
    import sqlite3
    from contextlib import nullcontext

    from jobs.main import make_scheduler

    class Disp:
        def __init__(self):
            self.alerts = []

        def alert(self, kind, text, level="P1"):
            self.alerts.append((kind, text, level))

    class Loops:
        """最小 loops替身：只需要 `.lock`。"""

        lock = nullcontext()

    class Cfg:
        pass

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()

    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)

    disp = Disp()
    sched, _state = make_scheduler(Cfg(), conn, _Clock(), loops=Loops(),
        dispatcher=disp, data_dir=str(data))
    job = {j.id: j for j in sched.get_jobs()}["backup"]

    with open(data / "backups" / ".backup.lock", "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        job.func()

    assert disp.alerts == [], (
        f"锁被占用时发了告警（应为静默跳过）：{disp.alerts}")
    conn.close()


def test_lock_is_held_while_the_copy_is_running(tmp_path):
    """**锁必须覆盖拷贝/校验/改名的全过程**（v0.2.114，Codex复核的假绿）。

    原测试只验了“入口拒绝”和“解锁后能备份”。把
    `return _backup_locked(...)`挪到 `with`外（锁只护住建文件名、不护拷贝），
    原断言**仍然全绿**——而那时拷贝、`integrity_check`、`os.replace`
    全在无锁状态下跑，两个进程照样能交错写同一个 tmp。

    这里在 `conn.backup()`执行期间**递归调一次 `backup_db()`**：
    如果锁护住了拷贝，这次递归必然拿到 `BackupBusy`。
    """
    data = tmp_path / "data"
    out = data / "backups"
    out.mkdir(parents=True)

    class ReentrantConn:
        """真 conn的代理：在 backup()期间偷偷再调一次 backup_db()。"""

        def __init__(self, real):
            self._real = real
            self.inner_error = None
            self.reentered = False

        def backup(self, dest):
            self.reentered = True
            try:
                backup_db(self._real, data, clock=_Clock())
            except BackupBusy as e:
                self.inner_error = e
            return self._real.backup(dest)

    real = _db()
    proxy = ReentrantConn(real)

    got = backup_db(proxy, data, clock=_Clock())

    assert proxy.reentered, "测试没真正进入 backup()"
    assert proxy.inner_error is not None, (
         "备份进行中第二次调用没有被拒绝——锁没覆盖拷贝过程")
    assert got is not None and got.exists(), "外层备份本身应当成功"
