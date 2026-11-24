"""每日23:30的数据库备份（Task28的 RUNBOOK按它写「升级与回滚」）。

顺序是硬性的：**先备份到临时文件 → `PRAGMA integrity_check`必须为 ok →原子改名**。
校验不过时既不覆盖旧备份、也不改名——最坏情况下你手上还有昨天的完整库，
而不是一个半截的新库把昨天那份盖掉。
"""
from __future__ import annotations

import fcntl # POSIX-only；本项目只跑 Linux容器
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

#:半截备份的保留时长。**它们必须以 `.`开头**（保证 `_prune`的
#: `market-*.db`不会把它们当成正式备份），但代价是**永远不会被任何清理逻辑
#:匹配到**——2026-11-21实测：11-15和11-20各留了1.2~1.3 GB的
#: `.market-*.db.tmp`，谁都不知道，`ls`不显示。加上11-21我手工跑备份时
#:被 `timeout`打断的那份，一共 ~3.9 GB白占着。
ORPHAN_TMP_S =2 *3600


class BackupBusy(RuntimeError):
    """另一份备份正持有锁——**不是失败**，是并发重叠时的正常跳过。

    为什么单独一个异常类型：`backup_db()`返回 `None`的语义是「真失败」
    （写盘错/校验不过），调用方据此发 **P0**。锁被占用如果也走 `None`，
    部署重叠窗口就会平白发一条「备份失败」P0。这两件事必须分开。
    """


def backup_db(conn, data_dir: Path, keep: int =7, clock=None) -> Path | None:
    """备份到 `data_dir/backups/market-YYYYMMDD.db`；校验失败返回 `None`并发 P0。

    **同名 tmp +跨进程互斥**（Codex复核）：临时文件名由日期决定，两个备份
    进程同时跑会写同一个 `.market-YYYYMMDD.db.tmp`，内容交错、`integrity_check`
    可能照样通过——结果是一份「看着正常、其实混了两个事务」的备份。
    `APScheduler`的 `max_instances=1`只管**同一个调度器实例**，管不了
    「旧容器还没退、新容器已经起」的部署重叠窗口。

    所以用 `flock`拿一把排他锁：拿不到就抛 `BackupBusy`（**不是失败**），
    **一个字节都不碰**。
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
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # **只把「竞争」当成「并发跳过」**：`flock(LOCK_NB)`在锁被占用时抛
        # `BlockingIOError`。原来写 `except OSError`会把 `ENOLCK`/`EACCES`/`EIO`
        # （锁机制本身坏了）一起吃下，于是真故障变成静默跳过——零告警。
        except BlockingIOError:
            raise BackupBusy("另一份备份正在进行") from None
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

    **只清「已经不可能有人还在写」的**：默认2小时内的一律保留，
    因为那可能是正在进行的备份。返回被删的文件名，供调用方记录。

    为什么需要它：备份顺序是「写 tmp →校验 →原子改名」，**中途被杀**
    （部署重启、OOM、`timeout`）就停在 tmp阶段，正式备份不存在、
    tmp又藏起来——外部只能看到「今天没有备份」，看不出真因。
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

    def _too_young(p: Path) -> bool:
        """可能正在写——主体和伴生都别动。"""
        try:
            return now_ts - p.stat().st_mtime < max_age_s
        except OSError:
            return True #读不到状态就当它是新的，不删

    # **glob只匹配主体 `.tmp`，不带尾随 `*`**（Codex复核抓到的）：
    #带 `*`时 `-shm` / `-wal`也是独立候选项，各自按自己的 mtime判——
    #主文件还新鲜、伴生文件超过2小时，伴生就会被**单独删掉**，
    #而那份备份其实还在写，删掉 shm等于破坏它。
    for f in sorted(out_dir.glob(".market-*.db.tmp")):
        if _too_young(f):
            continue
        if not _try_unlink(f):
            continue
        removed.append(f.name)
        #伴生文件**跟着主体走**：主体都删了它们就是孤儿。
        # SQLite关闭时可能重写 shm，按 mtime判会被当成「新鲜」而漏掉。
        for ext in ("-shm", "-wal", "-journal"):
            side = f.with_name(f.name + ext)
            if side.exists() and _try_unlink(side):
                removed.append(side.name)

    #第二遍：**主体已经不在了的孤儿伴生文件**。第一遍碰不到它们
    #（它们不匹配主体的 glob），不主动清就会一直留着。
    for side in sorted(out_dir.glob(".market-*.db.tmp-*")):
        main = side.with_name(side.name.rsplit("-",1)[0])
        if main.exists():
            continue #主体还在，交给第一遍按主体判
        # **伴生也要 mtime宽限**：原来这一遍对伴生零宽限，主体守2小时、
        #伴生不守，是不对称的。正常流程虽是「先建主体再产生伴生」，
        #但一旦出现「伴生已建、主体还没建」的瞬间，零宽限会直接删掉它。
        if _too_young(side):
            continue
        if _try_unlink(side):
            removed.append(side.name)

    return removed


def _prune(out_dir: Path, keep: int) -> None:
    """只保留最新的 `keep` 份归档备份，并清掉**孤儿伴生文件**。

    **`-shm` / `-wal` / `-journal` 必须显式处理**：它们的名字是
    `market-YYYYMMDD.db-shm`，**不匹配** `market-*.db`
    （glob 要求以 `.db` 结尾，`*` 不会拐过 `-shm` 后缀）。
    实测 81.4 上就有一对 `-shm`(32KB)/`-wal`(0B)，`_prune` 跑了几个月没碰。

    分两步，缺一不可：
    ① **先删伴生、再删主体**。顺序反过来的话，中途被杀就留下
         「主体没了、伴生还在」的孤儿，而第二步的 glob 永远捞不到它。
    ② **扫一遍孤儿**。上一版（或手工 `rm market-*.db`）已经造出的孤儿，
         没有任何主体可依附，只能靠这一遍捞。不加这遍，**修完也只防未来**。

    `keep < 1` 直接报错：`os.replace` 已经把今天这份改好名了，
    此时删全部会让"备份成功"变成"目录里一份都没有"，且调用方看不出来。
    """
    if keep < 1:
        raise ValueError(f"keep 必须 >= 1，否则会把刚写好的备份也删掉（got {keep}）")

    files = sorted(out_dir.glob("market-*.db"))
    try:
        for old in files[:-keep]:
            # **先伴生、后主体**：反过来被杀会留下捞不回的孤儿。
            for ext in ("-shm", "-wal", "-journal"):
                old.with_name(old.name + ext).unlink(missing_ok=True)
            old.unlink(missing_ok=True)
        # 第二遍：主体已经不在的孤儿伴生文件。
        for side in out_dir.glob("market-*.db-*"):
            body = out_dir / (side.name.split(".db-", 1)[0] + ".db")
            if not body.exists():
                side.unlink(missing_ok=True)
    except OSError as e:
        # `missing_ok` 挡不住 EACCES/EROFS/EIO。这里抛出去会让
        # `backup_db` 报「备份失败」——但 `os.replace` 早就成功了，
        # 备份其实是好的。删不掉旧文件不值得谎报备份失败。
        log.warning("清理旧备份时出错（备份本身已成功）：%s", e)
