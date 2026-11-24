"""SQLite 连接、迁移与写事务重试。

生产库是 `/app/data/market.db`（容器内），`SS_DB_PATH` 供批2 的故障演练指向临时库副本，
否则 `docker compose run agent` 会写生产库。

`connect()` 不自动迁移：jobs / api / backfill 三个入口各自第一行显式
`conn = connect(); migrate(conn)`。
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

DEFAULT_DB_PATH = Path("/app/data/market.db")
SCHEMA_DIR = Path(__file__).parent / "schema"


def connect(path: Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    if path is None:
        path = Path(os.environ.get("SS_DB_PATH", str(DEFAULT_DB_PATH)))
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    # **临时表/b-tree 放内存，不落盘**：默认 `temp_store = 0`（用文件）会把
    # SQLite 的中间结果写进 `$TMPDIR`，也就是**容器的可写层**——那里没有任何
    # 监控（`/app/data` 才是 bind mount）。大 `GROUP BY` 或 `ANALYZE` 时
    # 可能写出几百 MB，正常退出会清，**被 OOM/重启杀掉就会残留**，
    # 而残留只会体现为"某个容器在长"，这正是我们放弃的那项能力。
    # 代价是内存占用：jobs 容器 `mem_limit 1g`，实测最大查询的排序集
    # 远小于此。设成 MEMORY 后临时文件不再成为盲区。
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def _split_statements(script: str) -> list[str]:
    """按 sqlite3.complete_statement 切分脚本。

    不能用 executescript：它会在执行前隐式 COMMIT，把外层 BEGIN IMMEDIATE 提前放掉，
    版本检查与 DDL 就不再原子（两个进程仍可能都执行一遍建表）。
    """
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise sqlite3.OperationalError("schema 文件以不完整的语句结尾")
    return statements


def migrate(conn: sqlite3.Connection) -> list[str]:
    """按文件名顺序执行 core/schema/*.sql，返回本次新应用的文件名；重复调用返回 []。

    首次 `docker compose up` 时 api 与 jobs 会同时调用本函数。已应用版本必须在
    拿到写锁之后再读：锁外读到的空集合会让后拿到锁的进程重跑脚本、撞 already exists。
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.commit()
    done: list[str] = []
    for path in sorted(SCHEMA_DIR.glob("*.sql"), key=lambda p: p.name):
        # 写锁 + 版本检查 + 执行 + 记版本，全部在同一个事务里，中途失败不留半套表、不漏记版本。
        conn.execute("BEGIN IMMEDIATE")
        try:
            applied = {row[0] for row in conn.execute("SELECT name FROM schema_version")}
            if path.name not in applied:
                for statement in _split_statements(path.read_text(encoding="utf-8")):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version(name, applied_at) VALUES (?, ?)",
                    (path.name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                done.append(path.name)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return done
def make_conn_factory(path: Path | None = None, migrate_each: bool = True) -> Callable[[], sqlite3.Connection]:
    """返回「每次调用都建一个新连接并迁移」的闭包。

    `api` 按需建连接、不在 import 期建：`create_app(cfg, conn_factory, clock)` 每次
    取连接时都拿到已迁移的库。迁移是幂等的，重复调用只多一次版本表查询。

    **api 侧请用 `migrate_each=False`**：HTTP 请求里跑 `BEGIN IMMEDIATE` 会跟 jobs 的
    写事务互撞，`busy_timeout` 一过就是 500 `database is locked`。
    """

    def factory() -> sqlite3.Connection:
        conn = connect(path)
        if migrate_each:
            migrate(conn)
        return conn

    return factory


def write_retry(
    conn: sqlite3.Connection,
    fn: Callable[[sqlite3.Connection], T],
    *,
    attempts: int = 3,
    backoff_s: float = 0.05,
) -> T:
    """在一个 BEGIN IMMEDIATE 事务里跑 fn，`database is locked` 时退避重试。

    尝试期间把 busy_timeout 置 0，让锁冲突立刻返回、由本函数的退避节奏接管；
    否则 SQLite 会先静默等满 5 秒，再叠加退避，等待时间失控。

    **并发前提（硬性）**：连接是 `check_same_thread=False` 的共享连接，而本函数会把
    该连接的 busy_timeout 临时改成 0。调用方必须用一把 `threading.Lock` 把「同一连接上的
    所有写」串起来（`jobs` 的做法就是整条 `detect → ingest → dispatch` 在同一把锁内）——
    否则别的线程在这段窗口里写同一个连接会立刻拿到 `database is locked`，而不是等 5 秒。
    """
    previous_busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute("PRAGMA busy_timeout = 0")
    try:
        for attempt in range(1, attempts + 1):
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == attempts:
                    raise
                time.sleep(backoff_s)
                continue
            try:
                result = fn(conn)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            return result
        raise AssertionError("unreachable")
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(previous_busy)}")
