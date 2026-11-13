"""`snapshot_cache` 的只读视图（api 与批2 agent 工具共用）。

**只有 `as_of` 变了才重新解析 JSON**：这个函数每秒被 `/api/quotes`、`/api/quote/{code}`
和 agent 工具各调一次，若每次都 `json.loads` 一条上千行的 payload，纯粹是浪费 CPU。

解析走模块级别名 `_loads`：测试要能只替换本模块的解析入口，而不是全局 `json.loads`
（全局替换会递归并污染其它模块）。
"""
from __future__ import annotations

from json import loads as _loads


class SnapshotCache:
    def __init__(self, conn_factory):
        self.conn_factory = conn_factory
        self._cache: dict[str, tuple[str, dict]] = {}

    def get(self, scope: str) -> dict:
        """返回 `{"as_of": ..., "rows": [...]}`；没有该 scope 时返回空结构。"""
        # 不关连接：factory 的连接生命周期归调用方（进程级共享连接不该被一次查询关掉）
        row = self.conn_factory().execute(
            "SELECT as_of, payload FROM snapshot_cache WHERE scope = ?", (scope,)).fetchone()
        if row is None:
            return {"as_of": None, "rows": []}

        as_of, payload = row[0], row[1]
        hit = self._cache.get(scope)
        if hit is not None and hit[0] == as_of:
            return hit[1]
        parsed = _loads(payload)
        parsed.setdefault("rows", [])
        self._cache[scope] = (as_of, parsed)
        return parsed

    def row_for(self, scope: str, code: str) -> dict | None:
        for item in self.get(scope)["rows"]:
            if item.get("code") == code:
                return item
        return None

    def invalidate(self, scope: str | None = None) -> None:
        if scope is None:
            self._cache.clear()
        else:
            self._cache.pop(scope, None)
