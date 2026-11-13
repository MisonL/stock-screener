"""故障演练闸（RUNBOOK §11）：`SS_DRILL=1` 时**任何**外发都只记日志。

**必须是共享出口**：原先只有 `agent/push.py::AgentPusher` 里做了判定，
`notify/dispatcher.py`（jobs 的通道 A/C 告警、开盘简报、收盘摘要）与
`chat/session.py` 的确认卡走的是各自的 NtfyClient，完全不受演练约束——
演练进程一旦有可用网络就会真的发出去，还写下 `accepted` 的 `push_log`。
"""
from __future__ import annotations

import logging
import os

DRILL_ENV = "SS_DRILL"
_log = logging.getLogger("drill")


def drill_enabled() -> bool:
    """每次调用时读环境变量：演练用 `docker compose run -e` 注入，测试也直接 monkeypatch。"""
    return (os.environ.get(DRILL_ENV) or "").strip() == "1"


def drill_skip(where: str, **fields) -> bool:
    """演练中返回 True（调用方据此跳过外发，且**不写 `push_log` 的成功行**）。"""
    if not drill_enabled():
        return False
    _log.info("SS_DRILL=1 演练：%s 只记日志、不推送 %s", where, fields)
    return True
