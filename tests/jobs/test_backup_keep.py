"""备份保留份数：5 份，别让磁盘被历史备份吃掉。

81.4 上 98G 盘用到 83%，其中 7 份 × 1.3 GB ≈ **9.1 GB** 是本项目最大的一块
盘上占用（镜像与构建缓存属于同机其它项目，动不得）。5 份 = 6.5 GB，仍覆盖
一周业务日，够"发现某天数据坏了再回退"。
"""

from pathlib import Path

from jobs import backup


def test_keep_default_is_five():
    assert backup.BACKUP_KEEP == 5


def test_both_entrypoints_use_the_constant():
    """两个入口（定时 + 补备）必须用同一个值，否则补备会把份数又堆回 7。"""
    import inspect

    for fn in (backup.backup_db, backup.catch_up_missed_backup):
        src = inspect.getsource(fn)
        assert "BACKUP_KEEP" in src, f"{fn.__name__} 的 keep 默认值没跟常量走"


def test_prune_only_keeps_the_newest(tmp_path):
    out = tmp_path / "backups"
    out.mkdir()
    names = ["market-20261119.db", "market-20261121.db", "market-20261122.db",
             "market-20261123.db", "market-20261125.db", "market-20261126.db",
             "market-20261127.db"]
    for n in names:
        (out / n).write_bytes(b"x")
    backup._prune(out, backup.BACKUP_KEEP)
    left = sorted(p.name for p in out.glob("market-*.db"))
    assert left == names[-5:], left
    assert "market-20261119.db" not in left
    assert "market-20261121.db" not in left


def test_prune_still_refuses_zero(tmp_path):
    """`keep < 1` 必须报错：`os.replace` 已把今天这份改好名，
    此时删全部会让"备份成功"变成"目录里一份都没有"。"""
    import pytest

    out = tmp_path / "b"
    out.mkdir()
    (out / "market-20261127.db").write_bytes(b"x")
    with pytest.raises(ValueError):
        backup._prune(out, 0)
    assert (out / "market-20261127.db").exists()
