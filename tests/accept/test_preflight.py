"""上线前自检的口径：每一条都对着一个「切正式才暴露」的问题。"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

from core.config import load_config
from core.db import connect, migrate
from tests.helpers import SH

CFG = load_config(Path("config"))
T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)


def _load():
    spec = importlib.util.spec_from_file_location("preflight", Path("scripts/accept/preflight.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _load()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "p.db")
    migrate(c)
    return c


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("NTFY_TOKEN", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.setenv(k, "x")
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)


def _cfg(shadow: bool):
    from types import SimpleNamespace

    return SimpleNamespace(agent=SimpleNamespace(
        attention=SimpleNamespace(shadow_mode=shadow)))


def _turn(conn, decision, tin=None, tout=None):
    conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback,"
        " market, tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?,?,?)",
        (T0.isoformat(), "event", "d", 1, decision, 0, "cn_main", tin, tout))
    conn.commit()


def _beat(conn):
    conn.execute("INSERT OR REPLACE INTO service_heartbeat(service, last_beat_at, detail)"
                 " VALUES ('agent', ?, '{}')", (T0.isoformat(),))
    conn.commit()


# ---- 环境 ---------------------------------------------------------------

def test_missing_env_reported(conn, monkeypatch):
    """`NTFY_TOKEN` 缺了必须报（这条与 LLM 变量名无关）。"""
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    assert any("NTFY_TOKEN" in p for p in P.check_env())


def test_proxy_reported(conn, monkeypatch):
    """容器里有代理会连不上中转站，而报错是「连接超时」，很难查。"""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
    assert any("http_proxy" in p for p in P.check_proxy())


# ---- fail-closed 标志 ---------------------------------------------------

def test_budget_write_failed_flag_blocks_deploy(conn):
    """fail-closed 标志按池生效：切正式前两个池都必须是空的。"""
    conn.execute("INSERT INTO job_state(name, value)"
                 " VALUES ('budget.write_failed:agent', ?)", (T0.isoformat(),))
    conn.commit()
    out = P.check_budget_flag(conn)
    assert out and "budget.write_failed:agent" in out[0]
    assert "DELETE FROM job_state" in out[0], "要给出清除方法，否则会永久锁死"


def test_chat_pool_failure_is_reported_independently(conn):
    """chat 池的故障不能只在 agent 上查（否则 chat 被锁死却显示自检通过）。"""
    conn.execute("INSERT INTO job_state(name, value)"
                 " VALUES ('budget.write_failed:chat', ?)", (T0.isoformat(),))
    conn.commit()
    out = P.check_budget_flag(conn)
    assert any("chat" in o for o in out), "chat 池的故障没被报出来"


# ---- shadow 一致性 ------------------------------------------------------

def test_shadow_ready_requires_shadow_rows(conn):
    _beat(conn)
    assert any("shadow" in p for p in P.check_shadow(_cfg(True), conn, for_shadow=True))


def test_shadow_period_must_not_have_real_turns(conn):
    _turn(conn, "shadow")
    _turn(conn, "speak")
    out = P.check_shadow(_cfg(True), conn, for_shadow=True)
    assert any("shadow 不应跑 LLM" in p for p in out)


def test_formal_requires_shadow_off(conn):
    _turn(conn, "shadow")
    assert any("shadow_mode" in p for p in P.check_shadow(_cfg(True), conn, for_shadow=False))


def test_formal_detects_config_changed_without_rebuild(conn):
    """改了 yaml 但没重建容器：库里仍然只有 shadow 行。"""
    _turn(conn, "shadow")
    out = P.check_shadow(_cfg(False), conn, for_shadow=False)
    assert any("没重建容器" in p for p in out)


# ---- 记账与心跳 ---------------------------------------------------------

def test_accounting_detects_budget_without_audit(conn):
    """预算表扣了、`token_audit` 一条都没有 —— 审计出口坏了。"""
    conn.execute("INSERT INTO llm_budget(day, pool, tokens_in, tokens_out) VALUES (?,?,?,?)",
                 (20261030, "agent", 5000, 300))
    conn.commit()
    _turn(conn, "silent")
    out = P.check_accounting(conn)
    assert any("审计" in p for p in out)


def test_accounting_ok_when_audit_present(conn):
    """有扣减、有审计就干净——**不要求两边数值相等**（保守估算天然大于真实用量）。"""
    conn.execute("INSERT INTO llm_budget(day, pool, tokens_in, tokens_out) VALUES (?,?,?,?)",
                 (20261030, "agent", 5000, 300))
    conn.execute("INSERT INTO token_audit(ts, pool, real_in, real_out, estimated,"
                 " reserve_in, reserve_out, attempt_seq)"
                 " VALUES ('2026-10-30T10:00:00', 'agent', 5000, 300, 0, 5000, 300, 1)")
    conn.commit()
    _turn(conn, "silent", tin=5000, tout=300)
    assert P.check_accounting(conn) == []


def test_missing_heartbeat_reported(conn):
    assert any("心跳" in p for p in P.check_heartbeat(conn))
    _beat(conn)
    assert P.check_heartbeat(conn) == []


def test_reserve_check_is_retired(conn):
    """`reserve`/`settle` 模型已删除，这条检查必须**不再误报**。

    旧口径拿「预算表有量」对「`agent_turn.tokens_in`」，但新模型预算记估算、
    真实用量进 `token_audit`，chat 池的花费根本不写 `agent_turn`——
    纯 chat 消费会被误报成「预留未对账」。
    """
    conn.execute("INSERT INTO llm_budget(day, pool, tokens_in, tokens_out) VALUES (?,?,?,?)",
                 (20261030, "agent", 90000, 300))
    _turn(conn, "silent")
    conn.commit()
    assert P.check_reserve(conn) == []


def test_accounting_partial_audit_reported(conn):
    """回合很多但审计行寥寥 → 多数 attempt 没被记到，要报。"""
    conn.execute("INSERT INTO llm_budget(day, pool, tokens_in, tokens_out) VALUES (?,?,?,?)",
                 (20261030, "agent", 150, 60))
    for i in range(4):
        conn.execute(
            "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
            " fallback, market) VALUES (?, 'event', 'd', 1, 'silent', 0, 'cn_main')",
            (f"2026-10-30T10:0{i}:00",))
    conn.execute("INSERT INTO token_audit(ts, pool, real_in, real_out, estimated,"
                 " reserve_in, reserve_out, attempt_seq)"
                 " VALUES ('2026-10-30T10:00:00', 'agent', 100, 50, 0, 150, 60, 1)")
    conn.commit()
    assert any("审计" in p for p in P.check_accounting(conn))


def test_clean_state_passes(conn):
    _beat(conn)
    _turn(conn, "shadow")
    assert P.run(conn, _cfg(True), for_shadow=True) == []


# ---- 记账校验要匹配「保守扣减」模型（第十轮 P2-2）------------------------


def test_conservative_overestimate_is_not_an_error(conn):
    """预算表记的是估算、日志记的是真实用量，**不相等是设计**，不能报错。

    旧校验把两者当"应该相等"，健康部署会被判成「漏账」直接挡住上线。
    """
    with conn:
        conn.execute("INSERT INTO llm_budget(day, pool, tokens_in, tokens_out)"
                     " VALUES (20261110, 'agent', 1000, 2000)")
        _turn(conn, "speak", tin=100, tout=100)
        conn.execute("INSERT INTO token_audit(ts, pool, real_in, real_out, estimated,"
                     " reserve_in, reserve_out, attempt_seq)"
                     " VALUES ('2026-11-10T10:00:00', 'agent', 100, 100, 0, 1000, 2000, 1)")
    assert P.check_accounting(conn) == [], "把保守估算超过真实用量当成了错误"


def test_no_audit_rows_but_budget_spent_is_an_error(conn):
    """预算花了但**一条审计都没有** → 真的漏账，要报。"""
    with conn:
        conn.execute("INSERT INTO llm_budget(day, pool, tokens_in, tokens_out)"
                     " VALUES (20261110, 'agent', 1000, 2000)")
    out = P.check_accounting(conn)
    assert out and "审计" in out[0], f"预算花了却没有审计记录，未报出：{out}"


def test_shadow_turns_do_not_inflate_audit_denominator(conn):
    """影子回合不产生审计，也**不能**算进审计分母。

    影子期 `decision='shadow'`、`tokens_*` 为 NULL。把它们计入分母会让自检
    在影子期直接误报「102 个回合只有 2 条审计」，而实际一条审计也没漏。
    """
    with conn:
        conn.execute("INSERT INTO llm_budget(day, pool, tokens_in, tokens_out)"
                     " VALUES (20261110, 'agent', 2000, 4000)")
        _turn(conn, "silent", tin=1000, tout=2000)         # 正式回合
        conn.execute("INSERT INTO token_audit(ts, pool, real_in, real_out, estimated,"
                     " reserve_in, reserve_out, attempt_seq)"
                     " VALUES ('2026-11-10T10:00:00','agent',1000,2000,0,1000,2000,1)")
        # 100 条影子回合（tokens 为 NULL、不产生审计）
        for i in range(100):
            conn.execute(
                "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
                " fallback, market, tokens_in, tokens_out)"
                " VALUES (?, 'heartbeat','d',0,'shadow',0,'cn_main',NULL,NULL)",
                (f"2026-11-10T09:{i:02d}:00",))
    assert P.check_accounting(conn) == [], "影子回合把审计分母撑大导致误报"


def test_check_env_uses_configured_key_name(monkeypatch):
    """环境变量检查要认 `llm.yaml` 里的 `api_key_env`，不能写死 `LLM_API_KEY`。

    生产用的是 `DEEPSEEK_API_KEY`（yaml 里 `api_key_env: DEEPSEEK_API_KEY`），
    写死名字会让自检**永久误报**「缺少环境变量 LLM_API_KEY」——
    真正的缺失反而被这条噪音盖住。
    """
    monkeypatch.setenv("NTFY_TOKEN", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "y")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg = load_config(Path("config"))
    assert P.check_env(cfg) == [], P.check_env(cfg)


def test_check_env_reports_actually_missing_key(monkeypatch):
    """配的变量名真的没设时，必须报出来（别修成永不报错）。"""
    monkeypatch.setenv("NTFY_TOKEN", "x")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = load_config(Path("config"))
    problems = P.check_env(cfg)
    assert any("DEEPSEEK_API_KEY" in p for p in problems), problems
