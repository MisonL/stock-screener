"""第二批 schema（agent 工作日志、记忆、闹钟、对话、日预算）。"""
import pytest

from core.db import migrate


TURN_COLS = {
    "id", "ts", "wake_kind", "trigger_id", "context_digest", "context_chars",
    "context_truncated", "tool_calls", "rounds", "decision", "why", "level",
    "push_seq", "schedule_next", "latency_ms", "tokens_in", "tokens_out", "fallback",
    "market", "pending_action", "pending_status",
}


def cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_agent_turn_has_all_columns(db):
    assert TURN_COLS <= cols(db, "agent_turn")


def test_agent_turn_accepts_pending_row(db):
    db.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback, market,"
        " pending_action, pending_status) VALUES"
        " ('2026-10-30T10:00:00+08:00','chat','想加自选',1,'speak',0,'cn_main',?, 'pending')",
        ('{"tool": "add_watch", "args": {"code": "sh600519"}}',))
    db.commit()
    row = db.execute("SELECT pending_status, pending_action FROM agent_turn").fetchone()
    assert row["pending_status"] == "pending" and '"add_watch"' in row["pending_action"]


def test_pending_status_check_constraint(db):
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, fallback, market,"
            " pending_status) VALUES ('t','chat','d',1,'speak',0,'cn_main','bogus')")


def test_archive_table_has_same_shape(db):
    assert TURN_COLS <= cols(db, "agent_turn_archive")


def test_batch1_data_survives_second_migration(db):
    db.execute("INSERT INTO stock_meta(code,name,market,updated_at) VALUES ('sh600519','x','cn_main','t')")
    db.commit()
    assert migrate(db) == []                       # 幂等
    assert db.execute("SELECT count(*) FROM stock_meta").fetchone()[0] == 1


def test_push_log_not_altered_by_batch2(db):
    """002 不许 ALTER push_log：body/event_id/signal_id/trade_date 已由 001 建好（B1/D2）。"""
    assert "body" in cols(db, "push_log") and "signal_id" in cols(db, "push_log")
    assert "pending_action" not in cols(db, "push_log")


def test_budget_and_chat_tables(db):
    assert cols(db, "llm_budget") == {"day", "pool", "tokens_in", "tokens_out"}
    assert cols(db, "chat_session") == {"id", "created_at", "title"}
    assert {"session_id", "grounded"} <= cols(db, "chat_message")
    assert {"expires_at", "superseded_by", "source_turn_id"} <= cols(db, "agent_memory")


def test_schedule_status_check(db):
    with pytest.raises(Exception):
        db.execute("INSERT INTO agent_schedule(created_at, due_at, market, note, status)"
                   " VALUES ('t','t','cn_main','n','bogus')")
