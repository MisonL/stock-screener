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
    """预算必须够跑完**一个真实交易日**，口径全部来自生产实测。

    这是第三次修正同一件事，前两次都栽在"凭直觉估回合数"：
    300000（10 分钟打满）、1500000（4 小时打满）、3000000（14:20 打满）。
    实测 2026-11-18：127 次 attempt 预扣 2,924,888，外推全天约 145 次
    ≈ 3,340,000。所以这里钉住的是**attempt 真实均值 23031**，不是猜的数。
    """
    from agent.schedule import next_heartbeat
    from datetime import datetime
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

    # 实测：当日 60 个回合产生 127 次 attempt（每回合 2–4 轮 + 重写/重试）
    attempts_per_turn = 127 / 60
    per_attempt = 23031                      # 实测：2,924,888 / 127
    total_attempts = (beats + 35) * attempts_per_turn
    need = total_attempts * per_attempt

    limit_in, limit_out = CFG.llm.budget.agent
    assert limit_in >= need, (
        f"输入额度 {limit_in:,} 只够约 {limit_in/per_attempt:.0f} 次 attempt，"
        f"而一天需要约 {total_attempts:.0f} 次（约 {need:,.0f}）")
    assert limit_out >= total_attempts * CFG.llm.max_tokens.tools, (
        f"输出额度 {limit_out:,} 不够 {total_attempts:.0f} 次 attempt 的预留")
