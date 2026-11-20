"""**字符→token 系数必须守住"永不低估"这条线**（v0.2.103）。

## 为什么单独一个文件

预算是硬上限：`llm/budget.py` 明确"不可退还、无退款路径"。所以估算
**偏高只是浪费额度，偏低就是真金白银超支**。这跟一般的准确度问题不是一个量级，
值得一份独立的数据钉。

## 数据从哪来

2026-11-20 生产 `token_audit` 的 247 条真实用量，`est_in = chars * 1.2`
所以 `chars = est_in / 1.2`，`real_in` 是上游返回的 `prompt_tokens`。
反推得到的 **token/字符** 分布：

    中位 0.383   P90 0.475   P99 0.511   最大 0.530

v0.2.102 之前用 1.2 —— 相当于系统性高估 **3.1 倍**，同样的额度只能覆盖
约三分之一的回合（11-20 就是这么在 13:27 用完的）。

现在用 0.7：对实测最大值 0.530 仍留 **32% 余量**。

## 这个文件钉住什么

1. 系数对**实测最坏值**留够余量（≥25%）
2. 系数对**实测中位值**不要高估太多（≤3 倍）——否则又回到浪费额度的老路
3. `MIN_TURN_EST_IN` 与系数**同步缩放**，否则"最小回合放得下"的判据会脱钩
"""
from __future__ import annotations

from pathlib import Path

import pytest

#: 2026-11-20 生产实测（247 条）。反推方式见模块文档。
MEASURED_TOKENS_PER_CHAR = {
    "median": 0.383,
    "p90": 0.475,
    "p99": 0.511,
    "max": 0.530,
}


def test_coefficient_never_underestimates_the_measured_max():
    """**系数必须 ≥ 实测最大值，且留 ≥25% 余量**。

    低估 = 真实花费突破额度上限。这一条不能松。
    """
    from agent.loop import CHARS_TO_TOKENS

    worst = MEASURED_TOKENS_PER_CHAR["max"]
    assert CHARS_TO_TOKENS >= worst, (
        f"系数 {CHARS_TO_TOKENS} 低于实测最大 {worst} token/字符："
        "预扣会低于真实用量，额度上限会被突破")
    assert CHARS_TO_TOKENS >= worst * 1.25, (
        f"系数 {CHARS_TO_TOKENS} 对实测最大 {worst} 只留 "
        f"{(CHARS_TO_TOKENS / worst - 1) * 100:.0f}% 余量："
        "样本外的请求（更密的 JSON、更长的数字串）会穿透")


def test_coefficient_is_not_wastefully_high():
    """**系数也不该对中位值高估 3 倍以上**。

    这是 11-20 额度 13:27 用完的根因：1.2 对中位 0.383 高估 3.13 倍，
    5.5M 名义额度实际只能跑 1.87M，暴跌日半天就没了。
    """
    from agent.loop import CHARS_TO_TOKENS

    median = MEASURED_TOKENS_PER_CHAR["median"]
    ratio = CHARS_TO_TOKENS / median
    assert ratio <= 3.0, (
        f"系数 {CHARS_TO_TOKENS} 对实测中位 {median} 高估 {ratio:.2f} 倍："
        "额度会被大量浪费，忙的日子会提前停摆")


def test_min_turn_estimate_tracks_the_coefficient(tmp_path):
    """**`MIN_TURN_EST_IN` 必须跟着系数缩放**，否则判据与估算口径脱钩。

    v0.2.102 时代系数是 1.2、`MIN_TURN_EST_IN = 12000`（最小回合 8911 + 35%）。
    改系数时若忘了同步，比例会跑到 2 倍以上或 0.4 以下：
    前者会让"还有额度"被误判成用完，后者会让"其实跑不动"被放行。
    """
    from agent.context import Rendered
    from agent.loop import (EMIT_DECISION_TOOL, MIN_TURN_EST_IN, Trigger,
                            _est_in, _messages)
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from llm.tools import ToolRegistry
    from tests.helpers import SH
    from datetime import datetime

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "cal.db")
    migrate(conn)
    clock = FixedClock(datetime(2026, 11, 20, 10, 0, tzinfo=SH))
    tools = ToolRegistry(conn, cfg, clock, mcp=None)

    trig = Trigger(wake_kind="heartbeat")
    schema = list(tools.schema(trig.wake_kind)) + [EMIT_DECISION_TOOL]
    smallest = _est_in(_messages(trig, Rendered("…", 1, False, {})), schema)

    ratio = smallest / MIN_TURN_EST_IN
    assert 0.55 <= ratio <= 1.0, (
        f"最小回合估算 {smallest} 与 MIN_TURN_EST_IN {MIN_TURN_EST_IN} 比例 {ratio:.2f}："
        "两者口径不一致（系数改了但 MIN_TURN_EST_IN 没同步缩放）")


def test_repr_inflation_cancels_out_in_the_reverse_derivation(tmp_path):
    """**`str()` 膨胀在「反推」与「估算」两端抵消，不是安全余量的损耗**。

    第七轮评审（Codex）提出一条"严重风险"：`_est_in` 用 `len(str(c))` 抓字符，
    而 `str()` 对 dict/list 比紧凑 JSON 长约 1.9~8.8%，所以 0.530 这个实测最大值
    按真实 JSON 字符修正后应变成 0.54~0.58，于是 0.7 只剩 21~30% 余量。

    **这个推论把两个分母混用了**：

        反推：ratio = real_in / (est_old / 1.2)
               而 est_old = chars_str * 1.2
               ⇒ ratio = real_in / chars_str        ← 分母是 str() 字符数

        估算：est_new = chars_str * 0.7              ← 分母还是 str() 字符数

    两端用的是**同一个** `chars_str`，`str()` 膨胀同比例出现在分子分母的换算里，
    完全抵消。把它换成 `chars_json` 去比，才会凭空多出 8.8% 的"亏损"。

    这条测试用真实 `ToolRegistry` 造一个最小回合，量出两种字符口径的比值，
    再验证：即便按最悲观的口径（`chars_json`）折算，
    `real / est_new` 仍然 < 1——也就是说**两种口径下都不低估**。
    """
    from agent.context import Rendered
    from agent.loop import (CHARS_TO_TOKENS, EMIT_DECISION_TOOL, Trigger,
                            _est_in, _messages)
    from core.clock import FixedClock
    from core.config import load_config
    from core.db import connect, migrate
    from llm.tools import ToolRegistry
    from tests.helpers import SH
    from datetime import datetime
    import json as _json

    cfg = load_config(Path("config"))
    conn = connect(tmp_path / "repr.db")
    migrate(conn)
    clock = FixedClock(datetime(2026, 11, 20, 10, 0, tzinfo=SH))
    tools = ToolRegistry(conn, cfg, clock, mcp=None)

    trig = Trigger(wake_kind="heartbeat")
    schema = list(tools.schema(trig.wake_kind)) + [EMIT_DECISION_TOOL]

    # 两种字符口径
    chars_str = len(str(schema))
    chars_json = len(_json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    inflation = chars_str / chars_json
    assert inflation >= 1.0, "前提不成立：str() 不该比紧凑 JSON 更短"

    # 若把实测最大值 real/chars_str = 0.530 换算到 real/chars_json
    measured_on_str = MEASURED_TOKENS_PER_CHAR["max"]
    measured_on_json = measured_on_str * inflation

    # **估算仍然基于 chars_str**，所以真正决定低估与否的仍是 measured_on_str
    assert measured_on_str < CHARS_TO_TOKENS, (
        f"实测最大 {measured_on_str} ≥ 系数 {CHARS_TO_TOKENS}：会低估")

    # 即便按 Codex 的悲观口径：est_new 在 chars_json 口径下等于 coef*inflation，
    # 而 real/chars_json = measured_on_json，两者相除必须 < 1
    ratio_under_codex_view = measured_on_json / (CHARS_TO_TOKENS * inflation)
    assert ratio_under_codex_view < 1.0, (
        f"按 chars_json 口径折算后 real/est = {ratio_under_codex_view:.3f} ≥ 1，"
        "那才是真的低估；若 <1，说明两种口径下都安全")
    # 而且两种口径算出来的比值其实完全相同（这正是抵消的含义）
    assert ratio_under_codex_view == pytest.approx(measured_on_str / CHARS_TO_TOKENS)
