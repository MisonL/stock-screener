"""心跳线程：全天每 `every_s` 秒写一行 `service_heartbeat`。

`detail_provider` 是 `detail` 的**唯一入口**：每次写心跳前调一次取当前值，
返回 `None`/`{}` 时写空 dict。D9 的 `loop_last_tick_at`、`calls_by_scope` 与批2 的
`budget_exhausted` 都靠它注入——没有这个参数，watchdog 的「循环未推进」判据与
`/api/status` 的 `upstream_1h` 永远为空。
"""
from __future__ import annotations

import json
import threading
from typing import Callable


class Heartbeat:
    def __init__(self, conn_factory, service: str = "agent", every_s: int = 30,
                 detail_provider: Callable[[], dict | None] | None = None, clock=None):
        self.conn_factory = conn_factory
        self.service = service
        self.every_s = every_s
        self.detail_provider = detail_provider
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        """写一次心跳。任何时候都可以直接调（测试与启动时先写一次都走这里）。"""
        conn = self.conn_factory()
        detail = None
        if self.detail_provider is not None:
            try:
                detail = self.detail_provider()
            except Exception:                  # noqa: BLE001 —— 取 detail 失败不能打断心跳
                detail = None
        now = self.clock.now().isoformat() if self.clock is not None else _now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO service_heartbeat(service, last_beat_at, detail) VALUES (?,?,?)",
            (self.service, now, json.dumps(detail or {}, ensure_ascii=False)),
        )
        conn.commit()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=f"beat-{self.service}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.beat()
            except Exception:                  # noqa: BLE001 —— 心跳线程不能死
                pass
            self._stop.wait(self.every_s)


def _now_iso() -> str:
    from core.clock import SystemClock

    return SystemClock().now().isoformat()
