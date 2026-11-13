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


def _prune(out_dir: Path, keep: int) -> None:
    files = sorted(out_dir.glob("market-*.db"))
    for old in files[:-keep] if keep > 0 else files:
        old.unlink(missing_ok=True)
