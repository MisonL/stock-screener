"""双池日预算。"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from core.clock import FixedClock, SH
from core.db import connect, migrate
from llm.budget import Budget

T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)


class Usage:
    def __init__(self, pin, pout):
        self.prompt_tokens = pin
        self.completion_tokens = pout


@pytest.fixture
def clock():
    return FixedClock(T0)


@pytest.fixture
def budget(conn, clock):
    return Budget(conn, "agent", (1000, 200), clock)


def test_allow_respects_both_dimensions(budget):
    assert budget.allow(900, 100)
    assert not budget.allow(1001, 0)
    assert not budget.allow(0, 201)


def test_deduct_accumulates_and_shrinks_remaining(budget):
    """预算表记的是**保守已扣**：每发一次请求扣「输入估算 + max_tokens」。"""
    assert budget.deduct(300, 50) is True
    assert budget.deduct(200, 30) is True
    assert budget.remaining() == (500, 120)


def test_audit_with_no_usage_does_not_move_budget(budget):
    """SDK 没给 usage 时只写一条 `estimated=1` 审计，**预算表不动**——
    额度在发请求前就已经按估算扣过了，不存在"退款"这一步。"""
    budget.audit(None, unknown=True)
    assert budget.remaining() == (1000, 200)


def test_agent_pool_exhausted_does_not_touch_chat(conn, clock):
    agent = Budget(conn, "agent", (100, 10), clock)
    chat = Budget(conn, "chat", (1000, 100), clock)
    agent.deduct(100, 10)
    assert agent.allow(1, 0) is False
    assert chat.allow(500, 50) is True                 # 两个池互不影响


def test_day_is_natural_day_not_trading_day(conn, clock):
    b = Budget(conn, "agent", (10, 10), clock)
    b.deduct(5, 5)
    sat = FixedClock(datetime(2026, 10, 31, 12, 0, tzinfo=SH))   # 周六
    other = Budget(conn, "agent", (10, 10), sat)
    assert other.remaining() == (10, 10)               # 新的一天，另外算
    back = Budget(conn, "agent", (10, 10), clock)
    assert back.remaining() == (5, 5)


def test_unknown_pool_rejected(conn):
    with pytest.raises(ValueError):
        Budget(conn, "other", (1, 1))


def test_day_uses_shanghai_even_without_injected_clock(conn):
    """生产容器 TZ=UTC：不注入 clock 时也必须按上海自然日，否则 00:00–08:00 的用量记到前一天。"""
    from core.clock import SystemClock

    b = Budget(conn, "agent", (10, 10))                 # 不注入 clock
    assert b.day == int(SystemClock().now().strftime("%Y%m%d"))
    assert b.clock.now().tzinfo is not None            # SH 时钟必须带时区


def test_allow_rejects_when_already_over_limit(conn, clock):
    """用量已超限时 allow(0, 小量) 必须为 False：截零后用剩余量比较会让额度形同虚设。"""
    b = Budget(conn, "agent", (100, 10), clock)
    b.deduct(100, 10)
    assert b.allow(0, 1) is False and b.allow(1, 0) is False
    b.deduct(50, 50)                                   # 扣不动：不动账面
    assert b._used() == (100, 10)
    assert b.allow(0, 0) is True                      # 正好用满仍可判真
    assert b.allow(1, 0) is False


def test_bad_limits_raise_valueerror(conn):
    with pytest.raises(ValueError):
        Budget(conn, "agent", (10,))


# ---- 保守扣减模型（第九轮 P1 简化方案）-------------------------------------
# 旧模型：reserve 扣估计 -> settle 退款 + 记实际。任一环节算错就能绕过上限。
# 新模型：每个网络请求发出前**不可退还地**扣掉「输入估算 + max_tokens」；
# 真实 usage 只进审计表，不回写预算。预算表是"保守已扣"，永不少记。


def test_deduct_is_not_refundable(tmp_path):
    from llm.budget import Budget

    conn = connect(tmp_path / "b.db")
    migrate(conn)
    b = Budget(conn, "agent", (10000, 2000))
    assert b.deduct(1000, 2000) is True
    # 调用结束后**没有任何退款 API**：账面就停在这里
    assert b._used() == (1000, 2000)


def test_deduct_refuses_when_insufficient(tmp_path):
    from llm.budget import Budget

    conn = connect(tmp_path / "b.db")
    migrate(conn)
    b = Budget(conn, "agent", (10000, 2000))
    assert b.deduct(1000, 2000) is True
    assert b.deduct(1000, 2000) is False, "输出池已满还能再扣"
    assert b._used() == (1000, 2000), "拒绝时不能动账面"


def test_audit_records_real_usage_without_touching_budget(tmp_path):
    from llm.budget import Budget

    conn = connect(tmp_path / "b.db")
    migrate(conn)
    b = Budget(conn, "agent", (10000, 2000))
    assert b.deduct(1000, 2000) is True
    b.audit(SimpleNamespace(prompt_tokens=800, completion_tokens=1500, total_tokens=2300),
            unknown=False)
    # 预算表不变（保守已扣），真实用量进审计
    assert b._used() == (1000, 2000)
    row = conn.execute("SELECT real_in, real_out, estimated FROM token_audit").fetchone()
    assert row[0] == 800 and row[1] == 1500 and row[2] == 0


def test_audit_marks_unknown_usage_as_estimated(tmp_path):
    from llm.budget import Budget

    conn = connect(tmp_path / "b.db")
    migrate(conn)
    b = Budget(conn, "agent", (10000, 2000))
    b.deduct(1000, 2000)
    b.audit(None, unknown=True)
    row = conn.execute("SELECT real_in, real_out, estimated FROM token_audit").fetchone()
    assert tuple(row) == (None, None, 1), "未知用量必须标成估算，不能记 0"


def test_write_failure_is_per_pool(tmp_path):
    """agent 记账失败不能把 chat 池一起锁死（第九轮 P1-3）。"""
    from llm.budget import Budget

    conn = connect(tmp_path / "b.db")
    migrate(conn)
    a = Budget(conn, "agent", (10000, 2000))
    c = Budget(conn, "chat", (10000, 2000))
    with conn:
        conn.execute("INSERT INTO job_state(name, value) VALUES ('budget.write_failed:agent', 'x')")
    assert a.deduct(100, 100) is False, "agent 失败后仍放行"
    assert c.deduct(100, 100) is True, "chat 被 agent 的故障连坐"


def test_deduct_returns_false_when_write_locked(tmp_path):
    """SQLite 写锁时 `deduct` 必须**返回 False**，不能抛异常。

    第十轮 P2-3：另一个连接持有 `BEGIN IMMEDIATE` 时，`deduct` 的 UPDATE 被拒，
    抛出 `OperationalError`；调用方把它当成"LLM 调用异常"，于是一次**根本没发出去**
    的请求被记成一轮（F-34 口径被污染），而且 fail-closed 变成了崩溃。
    """
    import sqlite3

    conn = connect(tmp_path / "lock.db")
    migrate(conn)
    b = Budget(conn, "agent", (10000, 1000))
    # 另一个连接拿住写锁
    other = sqlite3.connect(str(tmp_path / "lock.db"), timeout=0)
    other.execute("BEGIN IMMEDIATE")
    try:
        assert b.deduct(100, 100) is False, "写锁时没有 fail-closed"
    finally:
        other.rollback()
        other.close()
    # 锁放开后恢复正常
    assert b.deduct(100, 100) is True


def test_attribute_error_inside_deduct_does_not_allow():
    """`deduct` **内部**抛 `AttributeError` 不能被当成"老接口"放行。

    第十轮 P2-4：`except AttributeError` 包住了整个调用，实现里的编程错误
    （比如写错属性名）会被当成"这个预算对象没有 deduct 方法"，然后退回
    `allow()` 放行——**绕过不可退还的扣减**，硬上限失效。
    """
    from agent.loop import _deduct

    class _Buggy:
        def allow(self, est_in, est_out):
            return True

        def deduct(self, est_in, est_out):
            raise AttributeError("internal programming bug")

    d = _deduct(SimpleNamespace(budget=_Buggy()))
    with pytest.raises(AttributeError):
        d(100, 100)


def test_budget_without_deduct_falls_back_to_allow():
    """只有 `allow` 的老测试桩仍要能跑（接口不存在，不是实现出错）。"""

    class _Old:
        def allow(self, est_in, est_out):
            return True

    from agent.loop import _deduct

    assert _deduct(SimpleNamespace(budget=_Old()))(100, 100) is True


def test_write_failure_sets_pool_flag(tmp_path):
    """预算表写不进去时要留下**按池标志**，让后续请求快速拒绝、也能被 preflight 查到。

    旧实现 `_write_failed()` 只有读方、没有写方，「记账故障 → 本池后续请求全拒」
    这条只存在于注释里。

    注意不能用「另开连接持写锁」来构造：锁住时 `job_state` 也写不进去，
    标志属于**尽力而为**。这里用一个真实可区分的故障（预算表被删、job_state 正常）。
    """
    conn = connect(tmp_path / "f.db")
    migrate(conn)
    b = Budget(conn, "agent", (10000, 1000))

    conn.execute("DROP TABLE llm_budget")
    conn.commit()
    assert b.deduct(100, 100) is False, "预算表没了还放行"

    assert b._write_failed() is True, "写失败没有留下标志，后续请求不会被快速拒绝"

    b.clear_write_failed()
    assert b._write_failed() is False


def test_concurrent_deduct_never_exceeds_limit(tmp_path):
    """8 个线程同时扣，总扣减**不能超过硬上限**（不能双花）。

    实现靠单条 `UPDATE ... WHERE tokens_in + ? <= limit` 的原子性，
    但之前没有任何并发用例守着它。
    """
    import threading

    path = str(tmp_path / "con.db")
    base = connect(path)
    migrate(base)
    b0 = Budget(base, "agent", (100, 100))
    b0.deduct(0, 0)                       # 确保当天那行存在，排除 INSERT 竞态干扰

    results = []
    barrier = threading.Barrier(8)

    def worker():
        c = connect(path)
        b = Budget(c, "agent", (100, 100))
        barrier.wait()
        results.append(b.deduct(30, 30))
        c.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 3, f"应只放行 3 次（30×3=90），实际 {sum(results)} 次"
    row = base.execute("SELECT tokens_in, tokens_out FROM llm_budget WHERE pool='agent'").fetchone()
    assert row[0] == 90 and row[1] == 90, f"账面被双花：{tuple(row)}"
