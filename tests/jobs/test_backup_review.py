def test_removed_list_only_contains_files_actually_deleted(tmp_path):
    """**返回值不能比事实乐观**（v0.2.112，评审用变异抓出的假绿）。

    评审的变异：把 `removed.append(...)` 挪到 `unlink` 失败的路径上也照样执行，
    原测试仍绿——因为没有一个用例去**验证 removed 与磁盘状态一致**。
    这里用"同名目录"制造 unlink 失败（`IsADirectoryError`），
    断言失败项**不在** removed 里。
    """
    import os
    import time

    from jobs.backup import prune_orphan_tmp

    d = tmp_path / "backups"
    d.mkdir(parents=True)
    now = time.time()
    old = now - 3 * 3600

    # 做成**目录**：unlink 抛 IsADirectoryError，删不掉
    bad = d / ".market-20261120.db.tmp"
    bad.mkdir()
    os.utime(bad, (old, old))

    # 一个正常的、能被删掉的
    good = d / ".market-20261119.db.tmp"
    good.write_bytes(b"x")
    os.utime(good, (old, old))

    removed = prune_orphan_tmp(d, now=now)
    assert good.name in removed, f"能删的没被报告：{removed}"
    assert bad.name not in removed, (
        f"删失败了却报成功（{removed}）——日志会声称清了但磁盘上还在")
    assert bad.exists(), "目录本来就不该被删"


def test_backup_db_cleans_orphans_before_writing(tmp_path):
    """**`backup_db()` 起点必须清一次**（v0.2.112，评审变异抓出的假绿）。

    去掉那行调用后原测试仍绿——没有用例走"备份"这条真实入口去验清理。
    这里直接调 `backup_db()`，断言历史半截文件在备份完成后已经不在了。
    """
    import os
    import sqlite3
    import time
    from datetime import datetime

    from jobs.backup import backup_db

    data = tmp_path / "data"
    out = data / "backups"
    out.mkdir(parents=True)

    # 历史遗留（3 小时前的中断备份）
    stale = out / ".market-20260101.db.tmp"
    stale.write_bytes(b"old")
    old = time.time() - 3 * 3600
    os.utime(stale, (old, old))

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()

    class C:
        def now(self):
            return datetime(2026, 11, 21, 23, 30)

    path = backup_db(conn, data, clock=C())
    conn.close()
    assert path is not None and path.exists(), "备份本身没生成"
    assert not stale.exists(), "起点清理没跑：历史半截备份还在"