"""当天没跑成的闹钟，盘后用**真实收盘数据**补答。

2026-11-27：14:32 预算断粮后 19 个闹钟停在 `pending`，问的几乎都是
"飞利信是否收在 3.9954 上方""涨停家数守没守住 300"——答案 15:05 就在库里。
原机制次日按日期作废，**永远没人回答**。用户只看到 agent 下午莫名其妙哑了。

补答**不调模型**：从日线与快照拼事实清单，零成本、可复核。
"""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from agent import review
from core.clock import FixedClock
from core.db import connect, migrate
from tests.helpers import SH


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "r.db")
    migrate(c)
    with c:
        c.execute("INSERT INTO stock_meta(code, name, market, industry, updated_at) VALUES ('sz300287','飞利信','cn_gem','x','2026-11-27')")
        c.execute("INSERT INTO stock_meta(code, name, market, industry, updated_at) VALUES ('sh600426','华鲁恒升','cn_main','x','2026-11-27')")
        c.execute("INSERT INTO daily_bars(code, date, open, high, low, close, volume, amount, source)"
                  " VALUES ('sz300287', 20261126, 3.7, 3.75, 3.68, 3.72, 1, 1, 't')")
        c.execute("INSERT INTO daily_bars(code, date, open, high, low, close, volume, amount, source)"
                  " VALUES ('sz300287', 20261127, 3.69, 4.05, 3.69, 4.01, 1, 1, 't')")
        c.execute("INSERT INTO daily_bars(code, date, open, high, low, close, volume, amount, source)"
                  " VALUES ('sh600426', 20261126, 18.0, 18.0, 17.9, 17.94, 1, 1, 't')")
        c.execute("INSERT INTO daily_bars(code, date, open, high, low, close, volume, amount, source)"
                  " VALUES ('sh600426', 20261127, 17.99, 18.0, 17.08, 17.94, 1, 1, 't')")
        c.execute("INSERT INTO snapshot_cache(scope, as_of, payload) VALUES ('market', '2026-11-27T15:05:15+08:00', ?)",
                  (json.dumps({"rows": [
                      {"code": "a", "price": 11.0, "prev_close": 10.0, "up": 11.0, "down": 9.0},
                      {"code": "b", "price": 9.0, "prev_close": 10.0, "up": 11.0, "down": 9.0}]}),))
    return c


def test_codes_are_extracted_from_notes():
    assert review.codes_in(["看 sh600426 下沿", "再看 sh600426", "sz300287 上沿"]) == \
        ["sh600426", "sz300287"]


def test_prices_are_not_mistaken_for_codes():
    """note 里全是 3.9954 这种价位，不能被当成代码。"""
    assert review.codes_in(["飞利信 3.9954 上沿、华鲁恒升 17.6128 下沿"]) == []


def test_names_are_resolved_to_codes(conn):
    got = review.named_codes(conn, ["看飞利信能否收回上沿，华鲁恒升是否跌破"])
    assert set(got) == {"sz300287", "sh600426"}


def test_close_prices_and_pct_are_reported(conn):
    body, codes = review.build(conn, ["看飞利信 3.9954 上沿、华鲁恒升 17.6128 下沿"], 20261127)
    assert "飞利信 收 4.01" in body
    assert "+7.80%" in body, f"涨跌幅算错：{body}"     # 3.72 -> 4.01
    assert "华鲁恒升 收 17.94 +0.00%" in body
    assert set(codes) == {"sz300287", "sh600426"}


def test_breadth_is_included(conn):
    body, _ = review.build(conn, ["涨停家数是否守住 300"], 20261127)
    assert "涨停 1" in body and "跌停 1" in body


def test_no_data_means_no_message(conn):
    """**绝不发空消息**：没有收盘数据时返回空串，调用方据此不推。"""
    body, _ = review.build(conn, ["随便一句话"], 20260101)
    assert body == ""


def test_targets_are_included_even_if_note_has_no_name(conn):
    body, codes = review.build(conn, ["收盘核对"], 20261127, targets=["sz300287"])
    assert "sz300287" in codes or "飞利信" in body


# ---- 接入 close_sweep ---------------------------------------------------

def _boss(conn, pushed):
    from agent.main import AgentMain

    class _Pusher:
        def agent_note(self, decision, grounded, code=None, target=None, **kw):
            pushed.append((decision.body, kw))
            return "seq-1"

    m = AgentMain.__new__(AgentMain)
    m.deps = SimpleNamespace(conn=conn, pusher=_Pusher())
    m.clock = FixedClock(datetime(2026, 11, 27, 15, 30, tzinfo=SH))
    return m


def test_missed_alarms_are_answered_after_close(conn):
    with conn:
        conn.execute("INSERT INTO agent_schedule(due_at, market, status, note, created_at)"
                     " VALUES ('2026-11-27T14:32:13','cn_main','pending','看飞利信能否收回 3.9954','x')")
        conn.execute("INSERT INTO agent_schedule(due_at, market, status, note, created_at)"
                     " VALUES ('2026-11-27T15:00:00','cn_main','pending','华鲁恒升 17.6128 守住没','x')")
    pushed = []
    _boss(conn, pushed)._answer_missed_alarms(
        datetime(2026, 11, 27, 15, 30, tzinfo=SH), 20261127)
    assert len(pushed) == 1, "没有推补答"
    body, kw = pushed[0]
    assert "2 条回访没跑成" in body
    assert "飞利信 收 4.01" in body
    assert kw.get("exempt_from_total_cap") is True, "收尾通知不该挤占行情额度"


def test_answer_is_idempotent(conn):
    with conn:
        conn.execute("INSERT INTO agent_schedule(due_at, market, status, note, created_at)"
                     " VALUES ('2026-11-27T14:32:13','cn_main','pending','看飞利信','x')")
    pushed = []
    boss = _boss(conn, pushed)
    now = datetime(2026, 11, 27, 15, 30, tzinfo=SH)
    boss._answer_missed_alarms(now, 20261127)
    boss._answer_missed_alarms(now, 20261127)
    assert len(pushed) == 1, "重复调用推了两次"


def test_only_todays_alarms_are_answered(conn):
    with conn:
        conn.execute("INSERT INTO agent_schedule(due_at, market, status, note, created_at)"
                     " VALUES ('2026-11-26T14:32:13','cn_main','pending','昨天的','x')")
    pushed = []
    _boss(conn, pushed)._answer_missed_alarms(
        datetime(2026, 11, 27, 15, 30, tzinfo=SH), 20261127)
    assert pushed == [], "把昨天（已过时）的闹钟也算进来了"


def test_stale_snapshot_is_not_reported_as_today(conn):
    """**快照日期必须是当天**：`snapshot_cache` 只有一行"最后一个快照"，
    不校验的话盘后补答会把昨天的涨停数报成今天的。"""
    with conn:
        conn.execute("UPDATE snapshot_cache SET as_of = ? WHERE scope = 'market'",
                     ("2026-11-26T15:05:00+08:00",))
    body, _ = review.build(conn, ["涨停家数守住没"], 20261127)
    assert "涨停" not in body, f"拿昨天的广度冒充今天：{body}"


def test_stocks_are_prioritised_over_indexes(conn):
    """**个股优先**：2026-11-27 实测 19 条闹钟里 4 条挂在指数上，把 6 个名额
    占满，真正被反复问的「飞利信」「史丹利」一个都没出现。

    指数涨多少用户自己看得见；个股守没守住某条线才是 agent 答应回访的事。
    """
    with conn:
        conn.execute("INSERT INTO daily_bars(code, date, open, high, low, close, volume,"
                     " amount, source) VALUES ('sh000001', 20261127, 3500, 3610, 3467, 3606, 1, 1, 't')")
        conn.execute("INSERT INTO daily_bars(code, date, open, high, low, close, volume,"
                     " amount, source) VALUES ('sz399006', 20261127, 2200, 2444, 2199, 2443, 1, 1, 't')")
    body, codes = review.build(
        conn, ["看飞利信", "看华鲁恒升", "创业板指回撤"], 20261127,
        targets=["sh000001", "sz399006", "sz300287", "sh600426"])
    assert codes[:2] == ["sz300287", "sh600426"], f"指数占了前面的名额：{codes}"
    assert any(c.startswith(("sh000", "sz399")) for c in codes[2:]), "指数被整个丢掉了"


def test_index_only_still_reports(conn):
    """只有指数时也要能出内容，不能因为"个股优先"就整条不推。"""
    with conn:
        conn.execute("INSERT INTO daily_bars(code, date, open, high, low, close, volume,"
                     " amount, source) VALUES ('sh000001', 20261127, 3500, 3610, 3467, 3606, 1, 1, 't')")
    body, codes = review.build(conn, ["看大盘"], 20261127, targets=["sh000001"])
    assert "sh000001 收 3606" in body


def test_most_asked_stock_wins_a_slot(conn):
    """**按被问次数排**，不是按名字长短。

    11-27 的 19 条闹钟里「飞利信」出现在 12 条上，是绝对主角；按长度排序时
    它和别的 3 字名一样长，被挤出了 6 个名额（实测）。
    """
    with conn:
        for code, name in (("sh688373", "盟科药业"), ("sz300003", "乐普医疗"),
                           ("sh601818", "中粮资本")):
            conn.execute("INSERT INTO stock_meta(code, name, market, industry, updated_at)"
                         " VALUES (?,?,'cn_main','x','2026-11-27')", (code, name))
            conn.execute("INSERT INTO daily_bars(code, date, open, high, low, close, volume,"
                         " amount, source) VALUES (?, 20261127, 10, 11, 9, 10, 1, 1, 't')", (code,))
    notes = ["看飞利信能否收回上沿"] * 12 + [
        "盟科药业是否推近上沿", "乐普医疗量比", "中粮资本上沿"]
    codes = review.named_codes(conn, notes, limit=2)
    assert codes[0] == "sz300287", f"最常被问的票没排第一：{codes}"
