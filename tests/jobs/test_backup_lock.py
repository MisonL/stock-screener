def test_concurrent_backup_is_refused_not_interleaved(tmp_path):
    """**两个备份进程不能同时写同一个 tmp**（v0.2.113，Codex复核）。

    临时文件名由日期决定，所以两个进程同时跑会写同一个
    `.market-YYYYMMDD.db.tmp`：内容交错，`integrity_check`可能照样通过——
    结果是一份"看着正常、其实混了两个事务"的备份。

    `APScheduler`的 `max_instances=1`只管**同一个调度器实例**，管不了
    "旧容器还没退、新容器已经起"的部署重叠窗口。这里用 `flock`
    在 `backup_db()`入口挡住第二个。

    测试手法：先手工拿住那把锁，再调 `backup_db()`，断言它返回 `None`
    且**一个文件都没动**（没有新备份、没有半截 tmp）。
    """
    import fcntl
    import sqlite3
    from datetime import datetime

    from jobs.backup import backup_db

    data = tmp_path / "data"
    out = data / "backups"
    out.mkdir(parents=True)

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()

    class C:
        def now(self):
            return datetime(2026,11,21,23,30)

    #模拟"另一个备份进程正持有锁"
    with open(out / ".backup.lock", "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        got = backup_db(conn, data, clock=C())

        assert got is None, "锁被占用时仍然做了备份——两份会写到同一个 tmp"
        assert not list(out.glob(".market-*.db.tmp")), "锁被占用时还留下了半截 tmp"
        assert not list(out.glob("market-*.db")), "锁被占用时仍然生成了正式备份"

    #锁释放后应当能正常备份（证明拒绝的是“并发”而不是“永远坏掉”）
    got2 = backup_db(conn, data, clock=C())
    conn.close()
    assert got2 is not None and got2.exists(), "锁释放后备份反而做不了"
