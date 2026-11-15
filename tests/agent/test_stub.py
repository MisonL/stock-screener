"""watchdog 的进程级契约：agent 主循环必然 import llm / openai（批2 Task 11 起）。

批1 曾断言 `agent.main` **不** import llm——批2 换成真实主循环后这条断言必须改写：
真实主循环要调 `LlmClient`，不 import 才是有问题的。现在钉的是**进程能被正常 import**
（前向依赖只剩 Task 12 的 `AgentPusher`，组装点必须是 `None` 占位）。
"""
import sys
import subprocess


def test_agent_main_imports_cleanly_in_subprocess():
    """子进程 import：不能因为 Task 12 的前向依赖在 import 期就失败。

    `AgentPusher` 还没创建，`agent/main.py` 只许在函数体内 import 它；模块级
    import 会让整个 agent 进程起不来。
    """
    out = subprocess.run(
        [sys.executable, "-c", "import sys, agent.main; print('llm' in sys.modules)"],
        capture_output=True, text=True, check=True,
        env={"PYTHONPATH": "/app", "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    assert out.stdout.strip() == "True"


def test_agent_main_does_not_pull_openai():
    """`agent.main` 本身只 import `llm` 包；`openai` 是 `LlmClient` 的实现细节。

    进程启动路径（`AgentMain.run`）里 import 它是允许的，但 import `agent.main` 模块
    本身不该把整个 openai 栈拉起来——它只在真正发请求时才需要。
    """
    out = subprocess.run(
        [sys.executable, "-c", "import sys, agent.main; print('openai' in sys.modules)"],
        capture_output=True, text=True, check=True,
        env={"PYTHONPATH": "/app", "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    assert out.stdout.strip() == "False"


def test_watchdog_heartbeat_import_still_holds():
    """watchdog 只依赖 heartbeat 模块，agent main 换真实实现后这条不能坏。"""
    out = subprocess.run(
        [sys.executable, "-c", "import sys, api.watchdog; print('ok')"],
        capture_output=True, text=True, check=True,
        env={"PYTHONPATH": "/app", "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    assert out.stdout.strip() == "ok"
