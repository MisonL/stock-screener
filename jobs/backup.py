"""每日 23:30 的数据库备份（Task 28 的 RUNBOOK 按它写「升级与回滚」）。

顺序是硬性的：**先备份到临时文件 → `PRAGMA integrity_check` 必须为 ok → 原子改名**。
校验不过时既不覆盖旧备份、也不改名——最坏情况下你手上还有昨天的完整库，
而不是一个半截的新库把昨天那份盖掉。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path


def backup_db(conn, data_dir: Path, keep: int = 7, clock=None) -> Path | None:
    """备份到 `data_dir/backups/market-YYYYMMDD.db`；校验失败返回 `None` 并发 P0。"""
    data_dir = Path(data_dir)
    out_dir = data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = (clock.now().strftime("%Y%m%d") if clock is not None
             else datetime.now().strftime("%Y%m%d"))
    final = out_dir / f"market-{today}.db"
    tmp = out_dir / f".market-{today}.db.tmp"
    # 先收拾历史遗留的半截备份，免得它们和本次的同名 tmp 混在一起
    prune_orphan_tmp(out_dir)

    try:
        dest = sqlite3.connect(tmp)
        try:
            conn.backup(dest)
            row = dest.execute("PRAGMA integrity_check").fetchone()
        finally:
            dest.close()
    except sqlite3.Error:
        row = None                              # 磁盘写满/文件损坏：当校验失败处理，不动旧备份

    if not row or row[0] != "ok":
        tmp.unlink(missing_ok=True)
        return None

    os.replace(tmp, final)                 # 原子改名：不会出现「写了一半的备份」
    _prune(out_dir, keep)
    return final


#: 半截备份的保留时长。**它们必须以 `.` 开头**（保证 `_prune` 的
#: `market-*.db` 不会把它们当成正式备份），但代价是**永远不会被任何清理逻辑
#: 匹配到**——2026-11-21 实测：11-15 和 11-20 各留了 1.2~1.3 GB 的
#: `.market-*.db.tmp`，谁都不知道，`ls` 不显示。加上 11-21 我手工跑备份时
#: 被 `timeout` 打断的那份，一共 ~3.9 GB 白占着。
ORPHAN_TMP_S = 2 * 3600


def prune_orphan_tmp(out_dir: Path, *, max_age_s: float = ORPHAN_TMP_S,
                     now: float | None = None) -> list[str]:
    """清掉中断留下的半截备份（`.market-*.db.tmp` 及其 `-shm`/`-wal`）。

    **只清"已经不可能有人还在写"的**：默认 2 小时内的一律保留，
    因为那可能是正在进行的备份。返回被删的文件名，供调用方记录。

    为什么需要它：备份顺序是"写 tmp → 校验 → 原子改名"，**中途被杀**
    （部署重启、OOM、`timeout`）就停在 tmp 阶段，正式备份不存在、
    tmp 又藏起来——外部只能看到"今天没有备份"，看不出真因。
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

    # **glob 只匹配主体 `.tmp`，不带尾随 `*`**（Codex 复核抓到的）：
    # 带 `*` 时 `-shm` / `-wal` 也是独立候选项，各自按自己的 mtime 判——
    # 主文件还新鲜、伴生文件超过 2 小时，伴生就会被**单独删掉**，
    # 而那份备份其实还在写，删掉 shm 等于破坏它。
    for f in sorted(out_dir.glob(".market-*.db.tmp")):
        try:
            if now_ts - f.stat().st_mtime < max_age_s:
                continue                      # 可能正在写，主体和伴生都别动
        except OSError:
            continue
        if not _try_unlink(f):
            continue
        removed.append(f.name)
        # 伴生文件**跟着主体走**：主体都删了它们就是孤儿。
        # SQLite 关闭时可能重写 shm，按 mtime 判会被当成"新鲜"而漏掉。
        for ext in ("-shm", "-wal"):
            side = f.with_name(f.name + ext)
            if side.exists() and _try_unlink(side):
                removed.append(side.name)

    # 第二遍：**主体已经不在了的孤儿伴生文件**。第一遍碰不到它们
    # （它们不匹配主体的 glob），不主动清就会一直留着。
    for side in sorted(out_dir.glob(".market-*.db.tmp-*")):
        main = side.with_name(side.name.rsplit("-", 1)[0])
        if main.exists():
            continue                          # 主体还在，交给第一遍按主体判
        if _try_unlink(side):
            removed.append(side.name)

    return removed


def _prune(out_dir: Path, keep: int) -> None:
    files = sorted(out_dir.glob("market-*.db"))
    for old in files[:-keep] if keep > 0 else files:
        old.unlink(missing_ok=True)
