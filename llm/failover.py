"""LLM 故障转移的落账：写库 + 告警。

`llm/client.py` 只负责"发现该换了"并调用 `set_failover_sink()` 注册的回调；
**落账逻辑放这里**，因为需要 conn/dispatcher，而 `LlmClient` 在单测里会被直接
构造、不该背数据库依赖。

2026-11-27 的教训：v0.2.150 给客户端加了故障转移，但 `last_failover` 只写内存
和一行 WARNING 日志，**全仓没有任何消费方**。供应商真被换掉时没人知道——
和"`LLM_MODEL` 写了没人读"是同一类错误：机制存在，但不为人知。
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

#: `job_state` 里最近一次故障转移的键（JSON，跨进程重启仍在）。
LAST_KEY = "llm_failover.last"


def state_key(today: int) -> str:
    """当日切换次数的计数器键。"""
    return f"llm_failover.count.{today}"


def record(conn, info: dict[str, Any], today: int, dispatcher=None) -> None:
    """把一次故障转移写进 `job_state`，并（可选）推一条告警。

    **每一次都要落库**：一天切了 5 次说明供应商有问题，只留"最近一次"是不够的，
    所以另有 `llm_failover.count.<day>` 计数。

    告警走 `Dispatcher.alert()`，kind 固定 `llm_failover`：它有自己的 30 分钟去重，
    不会因为切得多而刷屏；但**第一次切换一定发得出来**（不同 kind 不互相压制）。
    """
    payload = json.dumps(info, ensure_ascii=False)
    with conn:
        conn.execute(
            "INSERT INTO job_state(name, value) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (LAST_KEY, payload))
        key = state_key(today)
        conn.execute(
            "INSERT INTO job_state(name, value) VALUES (?, '1')"
            " ON CONFLICT(name) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
            (key,))
    log.warning("LLM 故障转移已落账：%s", payload)
    if dispatcher is None:
        return
    try:
        dispatcher.alert(
            "llm_failover",
            f"LLM 已切到备用端点：{info.get('to_model', '?')}（原 {info.get('from_model', '?')}）",
            "P1")
    except Exception:                            # noqa: BLE001
        log.warning("LLM 故障转移告警发送失败", exc_info=True)


def last(conn) -> dict[str, Any] | None:
    """读出最近一次故障转移（供 `/api/status` 与复盘脚本用）。"""
    row = conn.execute("SELECT value FROM job_state WHERE name = ?", (LAST_KEY,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None


def count_today(conn, today: int) -> int:
    row = conn.execute("SELECT value FROM job_state WHERE name = ?",
                       (state_key(today),)).fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0
