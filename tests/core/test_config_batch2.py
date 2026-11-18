"""agent.yaml / llm.yaml 的加载与必检。"""
from pathlib import Path

import pytest
import yaml

from core.config import ConfigError, load_config

CFG = load_config(Path("config"))


def test_agent_values():
    assert CFG.agent.event.deadline_s == 25
    assert CFG.agent.event.rounds == 3
    assert CFG.agent.heartbeat.max_min == 10
    assert CFG.agent.heartbeat.tight[0][0] == "09:15"
    assert CFG.agent.context.max_chars == 6000
    assert CFG.agent.channel_b_daily_cap == 15
    # v0.2.71：shadow 观测期结束（11-10~11-18 共 7 个交易日），已切正式模式
    assert CFG.agent.attention.shadow_mode is False


def test_llm_and_retry_are_root_level_siblings():
    """`retry` 是 llm.yaml 的根级键：读 cfg.retry.max_attempts，不是 cfg.llm.retry。"""
    assert CFG.llm.model == "deepseek-flash"
    assert CFG.retry.max_attempts == 3
    assert tuple(CFG.retry.backoff) == (0.5, 1.0)
    assert "502" in [str(x) for x in CFG.retry.retry_on] or 502 in CFG.retry.retry_on
    assert tuple(CFG.llm.budget.agent) == (300000, 40000)
    assert CFG.llm.pricing.is_estimate is True


def test_agent_config_missing_key_raises(tmp_path):
    for name in ("strategy.yaml", "markets.yaml", "notify.yaml"):
        (tmp_path / name).write_text(Path("config", name).read_text(encoding="utf-8"), encoding="utf-8")
    agent = yaml.safe_load(Path("config/agent.yaml").read_text(encoding="utf-8"))
    del agent["event"]["deadline_s"]
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(agent, allow_unicode=True), encoding="utf-8")
    (tmp_path / "llm.yaml").write_text(Path("config/llm.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    assert "agent.yaml" in str(exc.value) and "deadline_s" in str(exc.value)


def test_config_without_batch2_files_still_loads(tmp_path):
    """批1 只读三份 YAML 的场景仍要能加载（占位默认值兜住）。"""
    for name in ("strategy.yaml", "markets.yaml", "notify.yaml"):
        (tmp_path / name).write_text(Path("config", name).read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.agent.channel_b_daily_cap == 15 and cfg.retry.max_attempts == 3
