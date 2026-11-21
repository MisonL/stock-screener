

def test_prune_orphan_tmp_removes_stale_half_written_backups(tmp_path):
    """**中断留下的半截备份必须被清掉**（v0.2.111）。

    2026-11-21 实测：11-15 和 11-20 各留了 1.2~1.3 GB 的 `.market-*.db.tmp`，
    谁都不知道——它们以 `.` 开头，`ls` 不显示、`_prune` 的 `market-*.db`
    也匹配不到。11-21 我手工备份又被 `timeout` 打断，又来一份。共 ~3.9 GB。

    备份顺序是"写 tmp → 校验 → 原子改名"，**中途被杀**就停在 tmp 阶段，
    外部只能看到"今天没备份"，看不出真因是进程被中断。
    """
    import os
    import time
    from jobs.backup import prune_orphan_tmp

    d = tmp_path / "backups"
    d.mkdir(parents=True)
    now = time.time()

    # 一份新鲜的（2 小时内，可能是正在写的）——不能删
    fresh = d / ".market-20261121.db.tmp"
    fresh.write_bytes(b"x")
    os.utime(fresh, (now - 600, now - 600))     # 10 分钟前
    # 一份 3 小时前的——该删
    stale = d / ".market-20261120.db.tmp"
    stale.write_bytes(b"y" * 100)
    os.utime(stale, (now - 3 * 3600, now - 3 * 3600))
    # 它的 -shm / -wal 伴生文件也该删
    (d / ".market-20261120.db.tmp-shm").write_bytes(b"")
    (d / ".market-20261120.db.tmp-wal").write_bytes(b"")
    # 正式备份——绝不碰
    good = d / "market-20261119.db"
    good.write_bytes(b"z")

    removed = prune_orphan_tmp(d, now=now)
    assert fresh.exists(), "新鲜的 tmp 被删了（那可能是正在写的备份）"
    assert good.exists(), "正式备份被误删了"
    assert not stale.exists(), "陈旧的半截备份没被清掉"
    assert not (d / ".market-20261120.db.tmp-shm").exists(), "-shm 没跟着清"
    assert not (d / ".market-20261120.db.tmp-wal").exists(), "-wal 没跟着清"
    assert any("20261120" in n for n in removed), f"没报告删了哪些：{removed}"
    assert all("20261120" in n for n in removed), f"不该删的也被报告了：{removed}"


def test_fresh_main_with_old_companion_is_not_touched(tmp_path):
    """**新主体 + 旧伴生**：伴生不能单独删（Codex 复核的假绿分支）。

    glob 写成 `.market-*.db.tmp*` 时，`-shm` / `-wal` 也是独立候选项，
    各自按自己的 mtime 判断。主文件 10 分钟前刚写、还在备份中，
    伴生文件因为 mtime 较早（比如 SQLite `PRAGMA optimize` 重写过 shm）
    超过 2 小时——旧代码会把伴生**单独删掉**，破坏正在进行的备份。

    现在第一遍只遍历主体；主体新鲜就整个跳过，伴生一起留着。
    """
    import os
    import time
    from jobs.backup import prune_orphan_tmp

    d = tmp_path / "backups"
    d.mkdir(parents=True)
    now = time.time()

    main = d / ".market-20261121.db.tmp"
    main.write_bytes(b"x")
    os.utime(main, (now - 600, now - 600))       # 10 分钟前，可能正在写
    shm = d / ".market-20261121.db.tmp-shm"
    shm.write_bytes(b"y")
    os.utime(shm, (now - 3 * 3600, now - 3 * 3600))  # 3 小时前
    wal = d / ".market-20261121.db.tmp-wal"
    wal.write_bytes(b"z")
    os.utime(wal, (now - 3 * 3600, now - 3 * 3600))

    removed = prune_orphan_tmp(d, now=now)
    assert removed == [], f"主体还新鲜，伴生却被单独删了：{removed}"
    assert main.exists() and shm.exists() and wal.exists(), (
        "主体还新鲜，主体/伴生任何一个都不该被删")


def test_orphan_companion_without_main_is_cleaned(tmp_path):
    """**主体已不存在**的孤儿伴生也要清掉，第二遍扫描负责它。"""
    import os
    import time
    from jobs.backup import prune_orphan_tmp

    d = tmp_path / "backups"
    d.mkdir(parents=True)
    now = time.time()

    shm = d / ".market-20261120.db.tmp-shm"
    shm.write_bytes(b"y")
    os.utime(shm, (now - 3 * 3600, now - 3 * 3600))

    removed = prune_orphan_tmp(d, now=now)
    assert shm.name in removed, f"孤儿伴生没被清掉：{removed}"
    assert not shm.exists()
