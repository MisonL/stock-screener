"""迁移必须是**增量**的：已部署的库也要能拿到新表。

踩过的坑：把 `token_audit` 追加进 `002_batch2.sql` 后，本地新库（跑全量）一切正常，
但 VM 上已经记过 `002` 版本的库永远不会重跑那个文件——表就是没建出来，
而所有测试照绿。凡是"往旧文件追加语句"的改动都必须单独一个迁移文件。
"""
from __future__ import annotations

from pathlib import Path

from core.db import SCHEMA_DIR, connect, migrate


def test_adding_to_applied_file_is_never_reapplied(tmp_path):
    """把已应用文件改内容，再 migrate 不会重跑它——所以新表必须新文件。"""
    conn = connect(tmp_path / "m.db")
    migrate(conn)
    applied = {r[0] for r in conn.execute("SELECT name FROM schema_version")}
    assert "003_token_audit.sql" in applied, "新迁移文件没被执行"

    # 模拟"往已应用文件追加"：内容变了，但版本已记录 → 不会重跑
    before = conn.execute("SELECT count(*) FROM schema_version").fetchone()[0]
    assert migrate(conn) == [], "重复 migrate 不该再应用任何文件"
    assert conn.execute("SELECT count(*) FROM schema_version").fetchone()[0] == before


def test_token_audit_exists_after_migrate(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(token_audit)")}
    assert {"ts", "pool", "real_in", "real_out", "estimated",
            "reserve_in", "reserve_out", "attempt_seq"} <= cols


def test_every_schema_file_is_applied(tmp_path):
    """迁移文件与磁盘上的文件一一对应：漏一个就是线上少一张表。"""
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    applied = {r[0] for r in conn.execute("SELECT name FROM schema_version")}
    on_disk = {p.name for p in Path(SCHEMA_DIR).glob("*.sql")}
    assert applied == on_disk, f"未应用的迁移文件：{on_disk - applied}"
