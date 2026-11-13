"""进程级冒烟：把 `agent.main()` 真的跑起来。

**这是唯一能抓住「容器秒退 / 心跳没起」的测试**：`test_codex_fixes.py` 里的源码断言
只能确认代码里写了 `asyncio.run` 和 `Heartbeat(`，写在 if 分支里、或者被异常吞掉，
断言照样通过。

**必须用隔离子进程**，不能用 thread：`Heartbeat` 每次心跳都通过 `make_conn_factory()`
重新读 `SS_DB_PATH`，monkeypatch 在测试结束后会还原环境变量，而 daemon 线程还在跑
——它接下来就会写**默认库** `/app/data/market.db`，污染真实数据。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PROBE = r"""
import sqlite3, sys, threading, time
from agent import main as agent_main

worker = threading.Thread(target=agent_main.main, daemon=True)
worker.start()
deadline = time.time() + 90
row = None
while time.time() < deadline:
    time.sleep(0.4)
    try:
        c = sqlite3.connect(sys.argv[1])
        row = c.execute("SELECT detail FROM service_heartbeat WHERE service='agent'").fetchone()
        c.close()
    except sqlite3.Error:
        row = None
    if row is not None:
        break
if row is None:
    print("NOHEARTBEAT")
    sys.exit(1)
print("HEARTBEAT", row[0] or "")
# **还要确认主循环没退出**：`run()` 不 await 时进程照样写心跳（Heartbeat 是独立线程），
# 光查心跳发现不了「容器秒退」——必须看工作线程是否还活着。
time.sleep(1.5)
print("ALIVE" if worker.is_alive() else "DEAD")
sys.exit(0 if worker.is_alive() else 2)
"""


def test_agent_main_starts_and_writes_heartbeat(tmp_path):
    db = tmp_path / "boot.db"
    env = dict(os.environ)
    env.update({"PYTHONPATH": "/app", "SS_DB_PATH": str(db), "NTFY_TOKEN": "smoke"})
    # **超时要给足**：`agent.main()` 的启动路径要真的 import 一整套依赖（sqlite、
    # httpx、notify、agent 包……）。实测干净环境约 11 秒，CI 机器负载高时更久。
    # 原先 PROBE 内部只等 20 秒、subprocess 60 秒——机器一忙就假绿变假红，
    # 而这条恰恰是**唯一**能抓住「容器秒退/心跳没起」的测试，不能靠运气通过。
    out = subprocess.run([sys.executable, "-c", PROBE, str(db)], capture_output=True,
                         text=True, timeout=180, env=env, cwd="/app")
    assert out.returncode == 0, f"agent 进程没起来：{out.stdout}{out.stderr[-800:]}"
    assert "budget_exhausted" in out.stdout, "心跳 detail 没有 budget_exhausted（规则 3 抑制读不到）"
