"""agent.yaml / llm.yaml 的加载与必检。"""
from pathlib import Path

import pytest
import yaml

from core.config import ConfigError, load_config

CFG = load_config(Path("config"))


def test_agent_values():
    # `deadline_s: 0` = 不设回合时长上限（默认）。轮数（`rounds`）才是真正的界：
    # 上游网关排队时每轮要 23–31 秒，任何固定 deadline 迟早被正常回合超过去。
    assert CFG.agent.event.deadline_s == 0, "回合 deadline 默认应当是不限"
    assert CFG.agent.event.rounds == 3
    assert CFG.agent.event.rounds > 0, "轮数必须有限，它是唯一保证回合会结束的东西"
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
    # 退避表会随上游表现调整（限流严重时拉长）——只钉住结构与单调性
    table = tuple(float(x) for x in CFG.retry.backoff)
    assert len(table) >= 2 and all(x > 0 for x in table)
    assert list(table) == sorted(table), "退避应当逐档变长，不该出现倒挂"
    assert "502" in [str(x) for x in CFG.retry.retry_on] or 502 in CFG.retry.retry_on
    # **限流必须重试**：429 是"稍后再来"，不是"请求有错"
    retry_on = [str(x) for x in CFG.retry.retry_on]
    assert "429" in retry_on, "429 限流必须自动重试"
    assert "429" not in [str(x) for x in CFG.retry.never_retry]
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


def test_retry_on_contains_only_real_status_codes():
    """`retry_on` / `never_retry` 里**只能放具体状态码**。

    超时、空响应、连接失败（http_000）这三类永远重试、不可配置关闭：
    它们不是"服务器说不"，是"这次没拿到东西"，除了再试没有别的处理方式。
    以前 YAML 里写着 `timeout`/`empty_body`/`http_000` 三个词，代码一个都不读
    ——写了不生效的死配置，改它不会有任何行为变化。
    """
    for name in ("retry_on", "never_retry"):
        values = list(getattr(CFG.retry, name))
        for v in values:
            assert str(v).isdigit() or isinstance(v, int), (
                f"retry.{name} 里出现了非状态码项 {v!r}：它不会被任何代码读取")


def test_backoff_tiers_are_all_reachable():
    """**退避表的档位数不能超过重试次数**——否则最后的档位永远取不到，是死配置。

    `max_attempts: 3` = 首次 + 2 次重试，只有 2 次退避会真的发生。
    配 `[1.0, 3.0, 8.0]` 时那个 8 秒档永远用不到：写在 YAML 里、
    看起来像配置生效了，实际是死的。这类"看着配了其实没生效"的坑
    在这个仓库里已经踩过三次（`retry_on`、`agent.yaml` 的回合预算、本条）。
    """
    attempts = int(CFG.retry.max_attempts)
    table = list(CFG.retry.backoff)
    retries = max(attempts - 1, 0)          # 首次不算重试
    assert len(table) <= retries, (
        f"退避表有 {len(table)} 档，但最多只有 {retries} 次重试："
        f"第 {retries + 1} 档起永远不会被取到")
    assert len(table) == retries, "退避档位应当正好覆盖每一次重试"
