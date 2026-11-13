"""访问日志降噪的单元测试。

实测 24 小时 `api` 有 1337 行日志，其中 1332 行是 `/healthz`——本地 healthcheck
与 cloudflared 各每分钟探一次，把真正有价值的信息淹没了。
"""

import logging

import pytest

from api.logs import HealthzFilter, install


def _record(path: str, status: int = 200) -> logging.LogRecord:
    """按 uvicorn.access 的真实形状造记录：args 是 5 元组。"""
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", path, "1.1", status), exc_info=None)


def test_healthz_is_filtered_out():
    assert HealthzFilter().filter(_record("/healthz")) is False


def test_healthz_with_query_is_filtered_out():
    # 探活带缓存击穿参数时也要压掉，否则又回到一屏 200
    assert HealthzFilter().filter(_record("/healthz?t=1")) is False


@pytest.mark.parametrize("path", ["/", "/api/status", "/api/agent-log", "/healthz2", "/auth"])
def test_real_requests_are_kept(path):
    """业务请求必须留痕——只压探活，不是关掉访问日志。"""
    assert HealthzFilter().filter(_record(path)) is True


def test_malformed_record_is_kept():
    """args 形状不对时**保留**：宁可多打一行，也不要吞掉意外格式的日志。"""
    rec = logging.LogRecord(name="uvicorn.access", level=logging.INFO, pathname=__file__,
                            lineno=1, msg="something odd", args=None, exc_info=None)
    assert HealthzFilter().filter(rec) is True


def test_install_is_idempotent():
    logger = logging.getLogger("uvicorn.access")
    before = len(logger.filters)
    try:
        install()
        install()
        after = len(logger.filters)
        assert after - before <= 1, f"install() 叠加了多个过滤器：{before} -> {after}"
    finally:
        logger.filters = [f for f in logger.filters if not isinstance(f, HealthzFilter)]
