"""访问日志降噪：把 `/healthz` 的探活记录压掉。

实测 24 小时里 `api` 有 1337 行日志，其中 **1332 行是 `/healthz`**——
本地 healthcheck 与 cloudflared 各每分钟探一次 200，真正有价值的信息只剩个位数，
`docker logs` 基本没法读。

不是关掉访问日志（业务请求仍要留痕），只过滤掉这一个路径。
`record.args` 是 uvicorn access log 的固定 5 元组：
`(client_addr, method, full_path, http_version, status_code)`。
"""

from __future__ import annotations

import logging

#: 要静音的探活路径（精确匹配整条路径，不做前缀匹配——`/healthz2` 之类仍要留痕）
QUIET_PATHS: frozenset[str] = frozenset({"/healthz"})


class HealthzFilter(logging.Filter):
    """丢弃 `QUIET_PATHS` 的 access 记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # 正常是 5 元组；形状不对时**保留**（宁可多打一行，也不要吞掉意外格式的日志）
        if isinstance(args, tuple) and len(args) >= 3:
            path = str(args[2]).split("?")[0]
            if path in QUIET_PATHS:
                return False
        return True


def install() -> None:
    """装到 `uvicorn.access` 上（幂等：重复调用不会叠加过滤器）。"""
    logger = logging.getLogger("uvicorn.access")
    if any(isinstance(f, HealthzFilter) for f in logger.filters):
        return
    logger.addFilter(HealthzFilter())
