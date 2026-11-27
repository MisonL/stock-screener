"""预算配速：**把额度摊到全天**，别在上午烧完。

2026-11-27 实测：`llm.budget.agent: [5500000, 700000]` 只够约 400 次模型调用，
而 agent 每小时想做 180+ 次。结果是每天 10 点左右就把预算烧光，之后全天闭嘴：

    11-26 首次「预算已用完」10:07     11-25 最后成功回合 10:08
    11-24 10:47                      11-23 10:09      11-20 09:53

也就是说"下午最猛的行情收不到"，主因不是通道上限，是 **agent 上午就死了**。
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from agent.main import (PACE_FLOOR_DEFAULT, PACE_TOLERANCE_DEFAULT,
                        session_elapsed_fraction)
from tests.helpers import SH


def _t(h, m):
    return datetime(2026, 11, 27, h, m, tzinfo=SH)


# ---- 交易时段进度 -------------------------------------------------------

@pytest.mark.parametrize("hm,expected", [
    ((9, 0), 0.0),        # 盘前
    ((9, 15), 0.0),       # 开盘
    ((10, 15), (60 / 260)),   # 上午一小时
    ((11, 30), 135 / 260),    # 上午收盘
    ((12, 15), 135 / 260),    # 午休不动
    ((13, 0), 135 / 260),     # 下午开盘
    ((15, 5), 1.0),           # 收盘
    ((16, 0), 1.0),           # 盘后
])
def test_session_elapsed_fraction(hm, expected):
    assert session_elapsed_fraction(_t(*hm)) == pytest.approx(expected, abs=1e-6)


# ---- 配速判定 -----------------------------------------------------------

def _main(used_frac, *, enabled=True, tol=None, floor=None):
    from agent.main import AgentMain

    class _Budget:
        limit_in = 1000
        limit_out = 1000

        def _used(self):
            return (int(1000 * used_frac), 0)

    cfg = SimpleNamespace(agent=SimpleNamespace(budget_pace=SimpleNamespace(
        enabled=enabled,
        tolerance=PACE_TOLERANCE_DEFAULT if tol is None else tol,
        floor=PACE_FLOOR_DEFAULT if floor is None else floor)))
    m = AgentMain.__new__(AgentMain)                 # 不跑 __init__（它要整套 deps）
    m.deps = SimpleNamespace(cfg=cfg, budget=_Budget())
    return m


def test_heartbeat_yields_when_spending_far_ahead_of_the_clock():
    """10:15 就花掉 60%（时间进度才 23%）→ 心跳该让路。"""
    assert _main(0.60)._paced_out(_t(10, 15)) is True


def test_heartbeat_runs_when_spending_is_on_schedule():
    assert _main(0.25)._paced_out(_t(10, 15)) is False


def test_opening_window_is_not_paced():
    """**开盘不能一上来就被禁言**：09:20 时时间进度约 2%，但允许用到 floor+tol。"""
    assert _main(0.30)._paced_out(_t(9, 20)) is False
    assert _main(0.60)._paced_out(_t(9, 20)) is True


def test_no_pacing_near_the_close():
    """临近收盘时时间进度接近 1，怎么花都不该被拦。"""
    assert _main(0.95)._paced_out(_t(14, 50)) is False


def test_pacing_can_be_disabled():
    assert _main(0.99, enabled=False)._paced_out(_t(9, 30)) is False


def test_unreadable_budget_never_blocks():
    """预算读不出来时**放行**：配速是优化，不该变成新的停机原因。"""
    from agent.main import AgentMain

    class Boom:
        def _used(self):
            raise RuntimeError("db locked")

    m = AgentMain.__new__(AgentMain)
    m.deps = SimpleNamespace(cfg=SimpleNamespace(agent=SimpleNamespace(budget_pace=None)),
                             budget=Boom())
    assert m._paced_out(_t(10, 0)) is False


def test_missing_config_uses_defaults():
    """旧站点的 agent.yaml 没有 `budget_pace` 段 → 用默认值，功能默认开启。"""
    from agent.main import AgentMain

    m = AgentMain.__new__(AgentMain)
    m.deps = SimpleNamespace(cfg=SimpleNamespace(agent=SimpleNamespace()), budget=None)
    enabled, tol, floor = m._pace_config()
    assert enabled is True
    assert (tol, floor) == (PACE_TOLERANCE_DEFAULT, PACE_FLOOR_DEFAULT)


def test_event_turns_are_never_paced():
    """**事件回合永远放行**——源码级确认：配速只出现在心跳分支里。"""
    import inspect
    from agent.main import AgentMain

    src = inspect.getsource(AgentMain._once)
    line = [l for l in src.splitlines() if "_paced_out(" in l]
    assert line, "心跳分支里没有配速判断"
    assert all("heartbeat" in l or "retry" in l or "record_paced_skip" in l for l in line), (
        f"配速跑到了心跳以外的地方：{line}")
