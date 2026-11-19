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
    # v0.2.71：shadow 观测期结束（≥3 个交易日，见 RUNBOOK §10），已切正式模式
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
    """预算必须够跑完**一个真实交易日**。

    这个断言被写过三次，前两次都栽在"凭直觉估"：先按"每 10 分钟一次心跳"
    算出 34 个回合（实际 68 次），又按"每回合一次请求"估额度（实际一个回合
    会跑多轮工具调用 + grounding 重写 + 重试）。

    所以现在**每个因子都从代码或配置里推导**，不写死任何一次生产读数——
    别人用自己的心跳节奏部署时，这条测试也能算出属于他的答案。
    """
    from agent.loop import MAX_TOKENS, _est_in
    from agent.schedule import next_heartbeat
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # ① 一天的回合数：直接问调度器，而不是背一个常数
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
    # 事件唤醒与闹钟：至少每天还有这些，取一个保守估计
    turns = beats + 35

    # ② 每回合的 attempt 数：一个回合可能跑多轮（工具结果回灌）+ grounding 重写
    #    + 失败重试。取 2 是保守下界。
    attempts_per_turn = 2
    total_attempts = turns * attempts_per_turn

    # ③ 每次 attempt 的输入预扣：**用真实的估算函数**，而不是背一个数。
    #    每次都要重发两样固定开销：渲染好的上下文（上限见配置）+ 工具 schema
    #    （实测 event 回合 19 个工具约 5600 字符，chat 回合 23 个约 7000）。
    context_chars = int(CFG.agent.context.max_chars)
    typical_schema_chars = 6000
    per_attempt = _est_in([
        {"role": "system", "content": "y" * typical_schema_chars},
        {"role": "user", "content": "x" * context_chars},
    ])
    assert per_attempt > context_chars, f"估算函数没有把上下文算进去：{per_attempt}"

    need_in = total_attempts * per_attempt

    limit_in, limit_out = CFG.llm.budget.agent
    assert limit_in >= need_in, (
        f"输入额度 {limit_in:,} 不够一天：{turns} 个回合 × {attempts_per_turn} 次 "
        f"attempt × 每次约 {per_attempt:,} = {need_in:,}")
    assert limit_out >= total_attempts * MAX_TOKENS, (
        f"输出额度 {limit_out:,} 不够 {total_attempts} 次 attempt 的 max_tokens 预留")
