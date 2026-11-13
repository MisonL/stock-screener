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
from urllib.parse import unquote

#: 要静音的探活路径（精确匹配整条路径，不做前缀匹配——`/healthz2` 之类仍要留痕）
QUIET_PATHS: frozenset[str] = frozenset({"/healthz"})


#: 查询串里要打码的参数名。推送链接会带 `?t=<签名>&exp=<到期>`，
#: 那把整个 URL 记进 `docker logs` 等于把**免密凭据**写进日志文件，
#: 任何能看日志的人都能拿它登录。日志留痕只保留路径就够了。
SECRET_QUERY_KEYS: frozenset[str] = frozenset({"t", "exp"})


def _redact_query(raw: str) -> str:
    """把敏感查询参数的值换成 `***`，其余原样保留。

    不能用「整条丢掉」：`/stock/sh600519?tab=k` 这类业务参数对排障有用。

    **键要先做百分号解码再比对**：`?%74=xxx` 会被 Starlette 解成 `t=xxx`
    并成功验签（票据照样生效），而日志里还是 `%74`——按原文比对就不会打码，
    等于把一张有效票据原样写进日志。这正是「鉴权解析」与「日志解析」
    用两套规则时的经典漏法。
    """
    if "?" not in raw:
        return raw
    path, _, query = raw.partition("?")
    if not query:
        return path
    kept = []
    for pair in query.split("&"):
        key, sep, _value = pair.partition("=")
        name = unquote(key)
        if name in SECRET_QUERY_KEYS:
            kept.append(f"{key}{sep}***" if sep else f"{key}***")
        else:
            kept.append(pair)
    return f"{path}?{'&'.join(kept)}"


class HealthzFilter(logging.Filter):
    """丢弃 `QUIET_PATHS` 的 access 记录，并给查询串里的凭据打码。"""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # 正常是 5 元组；形状不对时**保留**（宁可多打一行，也不要吞掉意外格式的日志）
        if isinstance(args, tuple) and len(args) >= 3:
            raw = str(args[2])
            path = raw.split("?")[0]
            if path in QUIET_PATHS:
                return False
            if "?" in raw:
                # 元组是不可变的，重建一份；只替换路径这一项
                record.args = tuple(
                    _redact_query(str(a)) if i == 2 else a for i, a in enumerate(args))
        return True


def install() -> None:
    """装到 `uvicorn.access` 上（幂等：重复调用不会叠加过滤器）。"""
    logger = logging.getLogger("uvicorn.access")
    if any(isinstance(f, HealthzFilter) for f in logger.filters):
        return
    logger.addFilter(HealthzFilter())
