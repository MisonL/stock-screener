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


def test_ticket_in_query_is_redacted():
    """推送链接的免密票据不能落到日志里。

    链接长这样：`/stock/sh600519?t=<签名>&exp=<到期>`。整条记进 `docker logs`
    等于把免密凭据写进日志文件——能看日志的人就能登录。只保留路径就够排障了。
    """
    import logging

    from api.logs import HealthzFilter

    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s - %s", None, None)
    rec.args = ("1.2.3.4:5", "GET", "/stock/sh600519?t=abcdef&exp=1797174302", "HTTP/1.1", 302)
    assert HealthzFilter().filter(rec) is True          # 保留这条日志
    logged = str(rec.args[2])
    assert "abcdef" not in logged, "票据明文进了日志"
    assert "1797174302" not in logged, "到期时刻进了日志"
    assert "/stock/sh600519" in logged, "路径被一起抹掉了，日志失去排障价值"


def test_business_query_params_are_kept():
    """业务参数（如 `tab=k`）必须留着，只打码凭据。"""
    import logging

    from api.logs import HealthzFilter

    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", None, None)
    rec.args = ("1.2.3.4:5", "GET", "/stock/sh600519?tab=k&t=secret123&exp=99", "HTTP/1.1", 200)
    HealthzFilter().filter(rec)
    logged = str(rec.args[2])
    assert "tab=k" in logged
    assert "secret123" not in logged


def test_redaction_survives_an_unexpected_args_shape():
    """uvicorn 若改了 access log 的参数结构，票据也不能明文进日志。

    过滤器原先只处理 5 元组；形状变了就整条放行，票据会**明文写进 docker logs**
    而没有任何测试会发现——"保留原日志"这个取舍在凭据面前不成立。
    """
    import logging

    from api.logs import HealthzFilter

    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        "%s", ("GET /stock/sh600519?t=abc123&exp=99 HTTP/1.1",), None)
    assert HealthzFilter().filter(rec) is True
    logged = str(rec.msg) + str(rec.args)
    assert "abc123" not in logged, f"非元组形状时票据明文进了日志：{logged}"
