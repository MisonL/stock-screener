"""`memory` 三键必须彻底移除，且不能因此让旧站点起不来。

v0.2.152 核实：`agent/memory.py::write()` **全仓没有调用点**，
生产 `agent_memory` 表 0 行，`recall()` 恒返回空 → 上下文的【相关记忆】块
永远不出现。三个键（`judgment_days` / `per_target` / `ignored_focus`）
连"调参的对象"都不存在，留着只会让人以为改了有用。
"""

import shutil
from pathlib import Path

from core.config import load_config


def test_memory_keys_are_gone_from_the_template():
    text = Path("config/agent.yaml").read_text(encoding="utf-8")
    # 允许出现在说明文字里（如"已删除"），但不允许再是配置项
    for key in ("judgment_days:", "per_target:", "ignored_focus:"):
        assert key not in text, f"{key} 仍然是配置项"


def test_memory_is_no_longer_required():
    from core.config import _EMPTY_AGENT, _REQUIRED_AGENT

    assert "memory" not in _REQUIRED_AGENT, "memory 还在必填清单里"
    assert "memory" not in _EMPTY_AGENT, "memory 还有默认值"


def test_an_old_site_config_with_memory_keys_still_loads(tmp_path):
    """**旧站点的 agent.yaml 里还留着这三行**——必须照常加载。

    这是本次改动的真正风险：配置文件是站点保留的，删键不能变成"起不来"。
    """
    for name in ("strategy.yaml", "markets.yaml", "notify.yaml", "agent.yaml", "llm.yaml"):
        shutil.copy(Path("config") / name, tmp_path / name)
    old = tmp_path / "agent.yaml"
    text = old.read_text(encoding="utf-8")
    old.write_text(
        text + "\nmemory:\n  judgment_days: 5\n  per_target: 3\n  ignored_focus: 0.7\n",
        encoding="utf-8")
    cfg = load_config(tmp_path)                    # **不抛 ConfigError 即通过**
    # 多余键仍会出现在只读视图里（`_NS` 只是把 YAML 包一层），但**没有任何读取方**，
    # 所以留着无害。这里不断言它不存在——那是过严的假设，会挡住真正的兼容性目标。
    assert cfg.agent.channel_b_daily_cap == 15


def test_fresh_config_loads():
    cfg = load_config(Path("config"))
    assert not hasattr(cfg.agent, "memory")
