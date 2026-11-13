"""盘后出池：15:05 增量落库之后，15:20 前把观察池写好（spec §5.1、§10）。

调用契约只有一条硬约束：**`run_eod` 跳过（非交易日 / 错日快照 / 时间门未到）时不出池**。
其余交给 `core.strategy.pool.build_pool`，本模块只做三件事——刷物化字段、按启用的市场
逐个出池、把告警转给推送层。

`stock_stats` 在这里刷**一次**（不是每个市场刷一次）：它是全表的，市场只是过滤条件。
"""
from __future__ import annotations

import logging

from core.strategy.pool import PoolReport, build_pool, refresh_stock_stats

log = logging.getLogger(__name__)


def run(cfg, conn, clock, eod_report=None, dispatcher=None) -> list[PoolReport]:
    """按 `cfg.enabled_markets()` 出池，返回每个市场的 `PoolReport`。

    `eod_report` 是 `jobs.eod.run_eod` 的返回值：`skipped=True` 时整天不出池（节假日、
    上游还停在上一个交易日、当日 bar 未定盘）。`dispatcher` 是推送层（Task 23）；
    传 `None` 时只写库与日志。
    """
    if eod_report is not None and getattr(eod_report, "skipped", False):
        log.info("after_close: eod 跳过，今天不出池")
        return []

    as_of = clock.today()
    refresh_stock_stats(conn, as_of)

    reports: list[PoolReport] = []
    for market in cfg.enabled_markets():
        report = build_pool(conn, cfg, market, as_of)
        reports.append(report)
        # 两个数都要落日志：只看 size 时「溢出」永远是 pool_top_n，出池规模也失真
        log.info(
            "after_close: %s as_of=%s candidates=%d size=%d alerts=%s",
            market, as_of, report.candidates, report.size, report.alerts or "-",
        )
        _dispatch(dispatcher, report)
    return reports


def _dispatch(dispatcher, report: PoolReport) -> None:
    """把 `alerts` 交给推送层；`Dispatcher.pool_alerts(report)` 负责分级与渲染。

    没注入推送层（单测、手工跑脚本）时什么都不做——池子告警不能因为推送层
    缺失就让出池本身失败。分级：`pool_low` → P1，`pool_overflow` → P2。
    """
    if dispatcher is None or not report.alerts:
        return
    send = getattr(dispatcher, "pool_alerts", None)
    if send is None:
        log.warning("after_close: dispatcher 没有 pool_alerts，告警未推送: %s", report.alerts)
        return
    send(report)
