"""每日23:30的数据库备份（Task28的 RUNBOOK按它写「升级与回滚」）。

顺序是硬性的：**先备份到临时文件 → `PRAGMA integrity_check`必须为 ok →原子改名**。
校验不过时既不覆盖旧备份、也不改名——最坏情况下你手上还有昨天的完整库，
而不是一个半截的新库把昨天那份盖掉。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

#:半截备份的保留时长。**它们必须以 `.`开头**（保证 `_prune`的
#: `market-*.db`不会把它们当成正式备份），但代价是**永远不会被任何清理逻辑
#:匹配到**——2026-11-21实测：11-15和11-20各留了1.2~1.3 GB的
#: `.market-*.db.tmp`，谁都不知道，`ls`不显示。加上11-21我手工跑备份时
#:被 `timeout`打断的那份，一共 ~3.9 GB白占着。
ORPHAN_TMP_S =2 *3600


def backup_db(conn, data_dir: Path, keep: int =7, clock=None) -> Path | None:
    """备份到 `data_dir/backups/market-YYYYMMDD.db`；校验失败返回 `None`并发 P0。

    **同名 tmp +跨进程互斥**（Codex复核）：临时文件名由日期决定，两个备份
    进程同时跑会写同一个 `.market-YYYYMMDD.db.tmp`，内容交错、`integrity_check`
    可能照样通过——结果是一份"看着正常、其实混了两个事务"的备份。
    `APScheduler`的 `max_instances=1`只管**同一个调度器实例**，管不了
    "旧容器还没退、新容器已经起"的部署重叠窗口。

    所以用 `flock`拿一把排他锁：拿不到就说明另一份备份正在跑，返回 `None`
    （调用方当"没做成"处理），**一个字节都不碰**。
    """
    data_dir = Path(data_dir)
    out_dir = data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = (clock.now().strftime("%Y%m%d") if clock is not None
        else datetime.now().strftime("%Y%m%d"))
    final = out_dir / f"market-{today}.db"
    tmp = out_dir / f".market-{today}.db.tmp"

    # **锁文件常驻不删**：删掉再建的瞬间，两个进程会各拿到一把
    # "路径相同但 inode不同"的锁，互斥就失效了。
    with open(out_dir / ".backup.lock", "a+") as lock_fd:
        try:
            import fcntl

            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None #另一份备份正在进行，别碰任何文件
        return _backup_locked(conn, out_dir, final, tmp, keep)


def _backup_locked(conn, out_dir: Path, final: Path, tmp: Path,
    keep: int) -> Path | None:
    """持锁之后的实际流程（拆出来只是让上面那把锁的作用域一眼可见）。"""
    #先收拾历史遗留的半截备份，免得它们和本次的同名 tmp混在一起
    prune_orphan_tmp(out_dir)

    try:
        dest = sqlite3.connect(tmp)
        try:
            conn.backup(dest)
            row = dest.execute("PRAGMA integrity_check").fetchone()
        finally:
            dest.close()
    except sqlite3.Error:
        row = None #磁盘写满/文件损坏：当校验失败处理，不动旧备份

    if not row or row[0] != "ok":
        tmp.unlink(missing_ok=True)
        return None

    os.replace(tmp, final) #原子改名：不会出现「写了一半的备份」
    _prune(out_dir, keep)
    return final


def prune_orphan_tmp(out_dir: Path, *, max_age_s: float = ORPHAN_TMP_S,
    now: float | None = None) -> list[str]:
    """清掉中断留下的半截备份（`.market-*.db.tmp`及其 `-shm` / `-wal`）。

    **只清"已经不可能有人还在写"的**：默认2小时内的一律保留，
    因为那可能是正在进行的备份。返回被删的文件名，供调用方记录。

    为什么需要它：备份顺序是"写 tmp →校验 →原子改名"，**中途被杀**
    （部署重启、OOM、`timeout`）就停在 tmp阶段，正式备份不存在、
    tmp又藏起来——外部只能看到"今天没有备份"，看不出真因。
    """
    import time

    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    now_ts = time.time() if now is None else now
    removed: list[str] = []

    def _try_unlink(f: Path) -> bool:
        """真删掉了才返回 True——**返回值和日志不能比事实乐观**。"""
        try:
            f.unlink()
            return True
        except OSError:
            return False

    # **glob只匹配主体 `.tmp`，不带尾随 `*`**（Codex复核抓到的）：
    #带 `*`时 `-shm` / `-wal`也是独立候选项，各自按自己的 mtime判——
    #主文件还新鲜、伴生文件超过2小时，伴生就会被**单独删掉**，
    #而那份备份其实还在写，删掉 shm等于破坏它。
    for f in sorted(out_dir.glob(".market-*.db.tmp")):
        try:
            if now_ts - f.stat().st_mtime < max_age_s:
                continue #可能正在写，主体和伴生都别动
        except OSError:
            continue
        if not _try_unlink(f):
            continue
        removed.append(f.name)
        #伴生文件**跟着主体走**：主体都删了它们就是孤儿。
        # SQLite关闭时可能重写 shm，按 mtime判会被当成"新鲜"而漏掉。
        for ext in ("-shm", "-wal"):
            side = f.with_name(f.name + ext)
            if side.exists() and _try_unlink(side):
                removed.append(side.name)

    #第二遍：**主体已经不在了的孤儿伴生文件**。第一遍碰不到它们
    #（它们不匹配主体的 glob），不主动清就会一直留着。
    for side in sorted(out_dir.glob(".market-*.db.tmp-*")):
        main = side.with_name(side.name.rsplit("-",1)[0])
        if main.exists():
            continue #主体还在，交给第一遍按主体判
        if _try_unlink(side):
            removed.append(side.name)

    return removed


def _prune(out_dir: Path, keep: int) -> None:
    files = sorted(out_dir.glob("market-*.db"))
    for old in files[:-keep] if keep >0 else files:
        old.unlink(missing_ok=True)
