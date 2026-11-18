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
    # 预算是保守预扣（实测约 3.2 倍），值会随标定调整——这里只钉住结构
    assert len(tuple(CFG.llm.budget.agent)) == 2 and CFG.llm.budget.agent[0] > 0
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


def test_agent_budget_covers_a_full_trading_day():
    """预算必须够跑完**一个真实交易日**，按实测回合数算而不是凭直觉。

    历史：300000 只够 7 回合（当天 10:42 打满）；1500000 只够 75 回合，
    而全天光心跳就有 68 次，没有余量。这里用实测口径把它钉死。

    几个数都是**量出来的**：每回合约 20000（15 个真实回合计扣 299968）、
    全天心跳 67–68 次（`agent/schedule.py` 实测）、事件与闹钟保守取 35。
    """
    from agent.schedule import next_heartbeat
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    t = datetime(2026, 11, 18, 9, 15, tzinfo=tz)
    beats, seen = 0, set()
    while t.hour * 60 + t.minute <= 15 * 60:
        key = (t.hour, t.minute)
        if key in seen:
            break
        seen.add(key)
        beats += 1
        t = next_heartbeat(t)

    assert 60 <= beats <= 80, f"心跳次数与实测不符：{beats}"

    per_turn = 20000          # 实测：15 个真实回合计扣 299968
    need = (beats + 35) * per_turn       # 心跳 + 事件/闹钟
    limit_in, limit_out = CFG.llm.budget.agent
    assert limit_in >= need * 1.3, (
        f"输入额度 {limit_in:,} 只够 {limit_in/per_turn:.0f} 回合，"
        f"而一天需要约 {(beats+35)} 回合（至少 {need:,}）")
    assert limit_out > 0
