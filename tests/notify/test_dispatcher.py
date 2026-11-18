"""派发层规则（去重 / 熔断 / 上溢 / 合并）与落库契约。"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.clock import FixedClock
from core.config import load_config
from core.intraday.detector import NewEvent
from core.intraday.rules import Detection
from notify.dispatcher import Dispatcher, load_sig
from notify.templates import BriefRow, render_alert
from tests.helpers import SH, FakeNtfy, seed_sig_context

CFG = load_config(Path("config"))
T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)


def clock_at(h=10, m=0):
    return FixedClock(T0.replace(hour=h, minute=m))


def write_push_log(conn, *, code, kind, ts, level="P1", seq_id=None, status="accepted",
                   trade_date=20261030):
    conn.execute(
        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, status, trade_date)"
        " VALUES (?,?,?,?,?,1,?,?)",
        (ts, code, kind, level, seq_id or f"x-{ts}-{code}", status, trade_date))
    conn.commit()


def statuses(conn, kind=None):
    sql = "SELECT status FROM push_log" + (" WHERE kind=?" if kind else "") + " ORDER BY rowid"
    args = (kind,) if kind else ()
    return [r[0] for r in conn.execute(sql, args)]


@pytest.fixture
def env(tmp_db):
    ids = {}
    for code in ("sh600519", "sh600000", "sh600036", "sh600009", "sz000001"):
        ids[code] = seed_sig_context(tmp_db, code=code)
    ntfy = FakeNtfy()
    disp = Dispatcher(tmp_db, ntfy, CFG, clock_at())
    return type("E", (), dict(conn=tmp_db, disp=disp, ntfy=ntfy, ids=ids, cfg=CFG))


def ev(env, code, kind="breakout", level="P1"):
    """把某只票的 event_log/信号改成指定 level，再包成 NewEvent。"""
    det = Detection(kind, code, {"score": 86.0, "box_vol_multiple": 1.82})
    event_id = env.ids[code]
    return NewEvent(event_id, 1, det, 0.40, 1)


# ---- load_sig ------------------------------------------------------------

def test_load_sig_reads_six_sources(env):
    row = load_sig(env.conn, env.ids["sh600519"])
    assert row is not None
    assert row.code6 == "600519" and row.name == "贵州茅台" and row.industry == "白酒"
    assert row.trade_date == 20261029              # 池子是 T−1
    assert row.event_trade_date == 20261030
    assert row.b4 == pytest.approx(0.41)           # 来自 pool.factors 的 JSON
    assert row.up is not None                      # limit_prices 算出来的涨停价


def test_load_sig_none_when_snapshot_missing(env):
    env.conn.execute("DELETE FROM snapshot_cache")
    env.conn.commit()
    assert load_sig(env.conn, env.ids["sh600519"]) is None


def test_handle_skips_when_load_sig_fails(env):
    env.conn.execute("DELETE FROM snapshot_cache")
    env.conn.commit()
    env.disp.handle([ev(env, "sh600519")])
    assert env.ntfy.published == [] and env.conn.execute("SELECT count(*) FROM push_log").fetchone()[0] == 0


# ---- 落库契约 ------------------------------------------------------------

def test_push_log_row_complete_on_insert(env):
    env.disp.handle([ev(env, "sh600519")])
    row = env.conn.execute(
        "SELECT body, trade_date, event_id, signal_id, seq_id, segment, status, level, kind"
        " FROM push_log").fetchone()
    assert row["body"] and row["trade_date"] == 20261030
    assert row["event_id"] == env.ids["sh600519"] and row["signal_id"]
    # seq_id 用**事件日期**（20261030），不是池子日期（20261029）——
    # 否则今天的信号会记到昨天，池子没重建时跨日还会撞号
    assert row["seq_id"].startswith("sig-600519-20261030-") and row["segment"] == 1
    assert row["status"] == "accepted" and row["kind"] == "breakout"


def test_non_event_push_fills_today(env):
    rows = [BriefRow("600519", "茅台", 10.0, 90.0, 1.0)]
    env.disp.open_brief(rows, n=10)
    row = env.conn.execute(
        "SELECT event_id, signal_id, trade_date, body, level, kind FROM push_log"
        " WHERE kind='open_brief'").fetchone()
    assert row["event_id"] is None and row["signal_id"] is None
    assert row["trade_date"] == 20261030 and row["body"] and row["level"] == "P2"


def test_close_digest_written(env):
    env.disp.handle([ev(env, "sh600519")])
    env.disp.close_digest()
    row = env.conn.execute("SELECT kind, level, seq_id FROM push_log WHERE kind='close_digest'").fetchone()
    assert row["level"] == "P2" and row["seq_id"] == "close-20261030"


# ---- 去重 / 熔断 ---------------------------------------------------------

def test_same_code_same_kind_deduped_once(env):
    from notify.dispatcher import load_sig as _ls
    env.disp.handle([ev(env, "sh600519")])
    env.disp.handle([ev(env, "sh600519")])
    assert statuses(env.conn) == ["accepted", "suppressed"]
    assert len(env.ntfy.published) == 1


def test_breakdown_not_exempt_from_dedupe(env):
    env.disp.handle([ev(env, "sh600519", kind="breakdown")])
    env.disp.handle([ev(env, "sh600519", kind="breakdown")])
    assert statuses(env.conn) == ["accepted", "suppressed"]


def test_dedupe_expires_after_30min(tmp_db):
    seed_sig_context(tmp_db, code="sh600519")
    write_push_log(tmp_db, code="600519", kind="breakout",
                   ts=(T0 - timedelta(minutes=31)).isoformat())
    disp = Dispatcher(tmp_db, FakeNtfy(), CFG, clock_at())
    disp.handle([NewEvent(1, 1, Detection("breakout", "sh600519", {"score": 86.0}), 0.4, 1)])
    assert statuses(tmp_db)[-1] == "accepted"


def test_dedupe_survives_restart(tmp_db):
    seed_sig_context(tmp_db, code="sh600519")
    Dispatcher(tmp_db, FakeNtfy(), CFG, clock_at()).handle(
        [NewEvent(1, 1, Detection("breakout", "sh600519", {"score": 86.0}), 0.4, 1)])
    fresh = Dispatcher(tmp_db, FakeNtfy(), CFG, clock_at())     # 新实例 = 重启
    fresh.handle([NewEvent(1, 1, Detection("breakout", "sh600519", {"score": 86.0}), 0.4, 1)])
    assert statuses(tmp_db) == ["accepted", "suppressed"]


def test_daily_cap_lets_p0_through(env):
    env.conn.execute("UPDATE signals SET level='P1'")          # 先把种子信号的 P0 降下来
    env.conn.commit()
    for i in range(CFG.notify.daily_cap):
        write_push_log(env.conn, code=f"9{i:05d}", kind="alert", ts=T0.isoformat(), level="P2")
    env.disp.handle([ev(env, "sh600519")])                  # P1 → 压制
    env.disp.handle([ev(env, "sh600000")])                  # 也是 P1
    assert statuses(env.conn)[-2:] == ["suppressed", "suppressed"]
    # P0 仍放行：把 600036 的提升为 P0
    env.conn.execute("UPDATE signals SET level='P0' WHERE code='sh600036'")
    env.conn.commit()
    from notify.dispatcher import load_sig as _ls
    sig = _ls(env.conn, env.ids["sh600036"])
    env.disp._route(sig)
    assert statuses(env.conn)[-1] == "accepted"


def test_suppressed_not_counted_toward_cap(env):
    """suppressed 不算「已推」：算进去会形成越压越压的正反馈。"""
    for i in range(CFG.notify.daily_cap):
        write_push_log(env.conn, code=f"9{i:05d}", kind="alert", ts=T0.isoformat(), status="suppressed")
    env.disp.handle([ev(env, "sh600519")])
    assert statuses(env.conn)[-1] == "accepted"


# ---- 合并 ----------------------------------------------------------------

def test_third_p1_in_a_minute_merges(env):
    """前两条 P1 正常发；第 3 条起进合并缓冲，窗口到点后作为一条 P2 发出。

    种子票都在「白酒」行业，同一行业第 3 条会先命中**板块合并**（300 秒窗口）——
    这里先把行业拆开，测的才是 P1 的 60 秒合并。
    """
    codes = ("sh600519", "sh600000", "sh600036", "sh600009", "sz000001")
    for i, code in enumerate(codes):
        env.conn.execute("UPDATE stock_meta SET industry=? WHERE code=?", (f"行业{i}", code))
        env.conn.execute("UPDATE signals SET level='P1' WHERE code=?", (code,))
    env.conn.commit()
    for code in codes:
        env.disp.handle([ev(env, code)])
    assert statuses(env.conn) == ["accepted", "accepted"]     # 只发了两条 P1
    env.disp.tick()
    # **窗口未到不 flush**：每秒一 flush 会让缓冲区里永远只有 1 条，反洪峰规则失效
    assert env.conn.execute(
        "SELECT count(*) FROM push_log WHERE seq_id LIKE 'batch-%'").fetchone()[0] == 0
    env.disp.clock._now = T0 + timedelta(seconds=int(CFG.notify.p1_merge.window_s) + 1)
    env.disp.tick()
    batch = env.conn.execute(
        "SELECT level, body, seq_id FROM push_log WHERE seq_id LIKE 'batch-%'").fetchone()
    assert batch["level"] == "P2" and "3 只" in batch["seq_id"] + batch["body"]
    assert len([ln for ln in batch["body"].splitlines() if ln]) == 4      # 计数行 + 3 只


def test_p0_third_goes_sector_merge(env):
    """D7：同一行业 5 分钟内第 3 条 P0 并入板块级 P2。"""
    for code in ("sh600519", "sh600000", "sh600036"):
        env.conn.execute("UPDATE signals SET level='P0' WHERE code=?", (code,))
    env.conn.commit()
    from notify.dispatcher import load_sig as _ls
    for code in ("sh600519", "sh600000"):
        env.disp._route(_ls(env.conn, env.ids[code]))
    env.disp._route(_ls(env.conn, env.ids["sh600036"]))
    env.disp.clock._now = T0 + timedelta(seconds=int(CFG.notify.sector_merge.window_s) + 1)
    env.disp.tick()
    batch = env.conn.execute("SELECT level FROM push_log WHERE seq_id LIKE 'batch-%'").fetchone()
    assert batch is not None and batch["level"] == "P2"


def test_overflow_digest_when_pool_large(env):
    env.conn.execute("UPDATE signals SET level='P1'")
    env.conn.commit()
    n = int(CFG.strategy.universe.pool_overflow) + 1
    for i in range(n):
        env.conn.execute(
            "INSERT OR REPLACE INTO pool(trade_date, market, code, rank, score, factors, box_days,"
            " box_high_adj, box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean, pos_in_box)"
            " VALUES (20261029,'cn_main',?,?,50,'{}',40,10,9,10,9,9.5,1e6,0.5)",
            (f"sh8{i:05d}", i + 1))
    env.conn.commit()
    env.disp.handle([ev(env, "sh600519")])
    env.disp.clock._now = T0 + timedelta(seconds=int(CFG.notify.overflow_digest_s) + 1)
    env.disp.tick()
    assert env.conn.execute("SELECT count(*) FROM push_log WHERE seq_id LIKE 'batch-%'").fetchone()[0] == 1


# ---- D20 通道故障 --------------------------------------------------------

def test_push_channel_down_self_check(env):
    env.ntfy.fail = True
    for code in ("sh600519", "sh600000", "sh600036"):
        env.disp.handle([ev(env, code)])
    row = env.disp.conn.execute("SELECT * FROM push_log WHERE kind='push_channel_down'").fetchone()
    assert row is not None and row["status"] == "failed"
    assert statuses(env.conn, "breakout") == ["failed", "failed", "failed"]


def test_no_datetime_now_in_sql():
    """时间一律来自 `clock`：SQL 里用 `datetime('now')` 会跟容器的 UTC 走，
    与 `clock.today()`（上海）差 8 小时，去重窗口与日熔断都会错位。"""
    for path in ("notify/dispatcher.py", "notify/policy.py"):
        src = Path(path).read_text(encoding="utf-8")
        assert "datetime('now'" not in src, path
    # 窗口下界由调用方传（policy 的 `since` 来自 `now.timestamp()`）
    assert "clock_now.timestamp() - minutes * 60" in Path("notify/policy.py").read_text(
        encoding="utf-8")


def test_alert_level_is_passed_through(env):
    env.disp.alert("watchdog", "jobs 卡住", level="P0")
    row = env.conn.execute("SELECT level, kind, event_id, trade_date FROM push_log"
                           " WHERE kind='watchdog'").fetchone()
    assert row["level"] == "P0" and row["event_id"] is None and row["trade_date"] == 20261030


def test_suppressed_not_counted_for_merge_threshold(env):
    """suppressed 不是「发出去过」：算进来会让合并提前触发。"""
    for i in range(5):
        write_push_log(env.conn, code=f"9{i:05d}", kind="breakout", ts=T0.isoformat(),
                       level="P1", status="suppressed")
    env.disp.handle([ev(env, "sh600519")])
    assert env.conn.execute(
        "SELECT count(*) FROM push_log WHERE seq_id LIKE 'batch-%'").fetchone()[0] == 0


# ---- 合批成员落库 / 板块合并按 kind / 告警优先级 / 原子认领 ----------------

def test_batch_members_each_get_push_log_and_dedupe(env):
    """合批的每一只都写一行 push_log：否则同批第 2 只不进 30 分钟去重。"""
    codes = ("sh600519", "sh600000", "sh600036", "sh600009")
    for i, code in enumerate(codes):                     # 拆开行业，测 P1 合并而非板块合并
        env.conn.execute("UPDATE stock_meta SET industry=? WHERE code=?", (f"行业{i}", code))
        env.conn.execute("UPDATE signals SET level='P1' WHERE code=?", (code,))
    env.conn.commit()
    for code in codes:
        env.disp.handle([ev(env, code)])                  # 前两只单发，后两只进合并缓冲
    env.disp.clock._now = T0 + timedelta(seconds=int(CFG.notify.p1_merge.window_s) + 1)
    env.disp.tick()
    assert len(env.ntfy.published) == 3                   # 2 条单发 + 1 条合并
    batch_rows = env.conn.execute(
        "SELECT code, seq_id, level, segment, status FROM push_log"
        " WHERE seq_id LIKE 'batch-%' ORDER BY rowid").fetchall()
    assert [r["code"] for r in batch_rows] == ["600036", "600009"]
    assert len({r["seq_id"] for r in batch_rows}) == 1
    assert all(r["level"] == "P2" and r["segment"] == 1 and r["status"] == "accepted"
               for r in batch_rows)
    # 两只都必须参与去重
    for code in ("sh600036", "sh600009"):
        env.disp.handle([ev(env, code)])
    assert statuses(env.conn)[-2:] == ["suppressed", "suppressed"]
    assert len(env.ntfy.published) == 3


def test_batch_member_status_follows_publish_result(env):
    """发布失败时，批次内每一行都记 failed，不是只把首行改掉。"""
    from notify.dispatcher import load_sig as _ls

    env.ntfy.fail = True
    items = [_ls(env.conn, env.ids["sh600519"]), _ls(env.conn, env.ids["sh600000"])]
    env.disp._send_batch(items, level="P2")
    rows = env.conn.execute(
        "SELECT status FROM push_log WHERE seq_id LIKE 'batch-%' ORDER BY rowid").fetchall()
    assert [r["status"] for r in rows] == ["failed", "failed"]


def test_sector_merge_requires_same_industry_and_kind(env):
    """同行业但 kind 不同（跌破 vs 突破）不能并进同一批。"""
    from notify.dispatcher import load_sig as _ls

    for code in ("sh600519", "sh600000"):
        env.conn.execute("UPDATE stock_meta SET industry='白酒' WHERE code=?", (code,))
        env.conn.execute("UPDATE signals SET level='P1' WHERE code=?", (code,))
    env.conn.commit()
    for code in ("sh600519", "sh600000"):
        env.disp.handle([ev(env, code)])                  # 两条同行业突破，单发
    assert statuses(env.conn) == ["accepted", "accepted"]

    bd_id = seed_sig_context(env.conn, code="sz000001", kind="breakdown", name="平安银行",
                             industry="白酒")
    env.conn.execute("UPDATE signals SET level='P0' WHERE event_id=?", (bd_id,))
    env.conn.commit()
    env.disp.handle([NewEvent(bd_id, 1, Detection("breakdown", "sz000001", {"score": 70.0}), 0.4, 1)])
    env.disp.clock._now = T0 + timedelta(seconds=int(CFG.notify.sector_merge.window_s) + 1)
    env.disp.tick()
    assert statuses(env.conn) == ["accepted", "accepted", "accepted"]     # 跌破照常单发
    assert env.conn.execute(
        "SELECT count(*) FROM push_log WHERE seq_id LIKE 'batch-%'").fetchone()[0] == 0
    assert len(env.ntfy.published) == 3


def test_alert_message_priority_mapping(env):
    """P0 告警必须 urgent(5)：watchdog 发 P0 时消息曾是 4，不弹窗不响铃。"""
    env.disp.alert("watchdog", "agent 停止", level="P0")
    assert env.ntfy.published[-1].priority == 5
    env.disp.alert("datasource", "数据源失效", level="P1")
    assert env.ntfy.published[-1].priority == 4
    env.disp.alert("pool", "池子异常", level="P2")
    assert env.ntfy.published[-1].priority == 3


def test_two_dispatchers_claim_atomically(tmp_db):
    """两个进程的 Dispatcher 抢同一个 signal：写事务认领，只许发一次。"""
    from core.db import connect

    event_id = seed_sig_context(tmp_db, code="sh600519")
    path = tmp_db.execute("PRAGMA database_list").fetchone()[2]
    other = connect(Path(path))                            # 另一条连接的「第二个进程」
    a = Dispatcher(tmp_db, FakeNtfy(), CFG, clock_at())
    b = Dispatcher(other, FakeNtfy(), CFG, clock_at())
    sig = load_sig(tmp_db, event_id)
    a._send(sig, a._render(sig), sig.level)
    b._send(sig, b._render(sig), sig.level)                # 认领时发现已被 A 发过
    assert len(a.ntfy.published) == 1
    assert b.ntfy.published == []
    assert [r[0] for r in tmp_db.execute(
        "SELECT status FROM push_log WHERE code='600519' ORDER BY rowid")] == ["accepted"]


def test_batch_member_rows_do_not_inflate_message_count(env):
    """批次内 N 行是 1 条消息：日熔断与收盘统计按 seq_id 计数，不按行数。"""
    from notify.dispatcher import load_sig as _ls

    items = [_ls(env.conn, env.ids[c]) for c in ("sh600519", "sh600000", "sh600036")]
    env.disp._send_batch(items, level="P2")
    assert env.conn.execute("SELECT count(*) FROM push_log").fetchone()[0] == 3
    assert env.disp._capped() is False                          # 1 条消息，远不到 60
    env.disp.close_digest()
    row = env.conn.execute("SELECT body FROM push_log WHERE kind='close_digest'").fetchone()
    assert row["body"].startswith("推送 1 条")                    # 不是 3 条


def test_buffers_from_previous_day_are_dropped(env):
    """收盘后残留的合并项不能次日发出：trade_date/seq_id/文案全是昨天的。"""
    for code in ("sh600519", "sh600000", "sh600036"):
        env.conn.execute("UPDATE stock_meta SET industry=? WHERE code=?", (f"行业{code}", code))
        env.conn.execute("UPDATE signals SET level='P1' WHERE code=?", (code,))
    env.conn.commit()
    for code in ("sh600519", "sh600000", "sh600036"):
        env.disp.handle([ev(env, code)])
    assert env.disp._pending or env.disp._sector       # 有东西进了缓冲
    # 次日的 tick：缓冲必须先被丢弃，不能发出去
    from datetime import datetime as _dt
    env.disp.clock._now = _dt(2026, 11, 2, 9, 20, tzinfo=T0.tzinfo)
    env.disp.tick()
    assert not env.disp._pending and not env.disp._sector
    assert env.conn.execute(
        "SELECT count(*) FROM push_log WHERE seq_id LIKE 'batch-%'").fetchone()[0] == 0


def test_first_breakdown_same_day_not_swallowed_by_breakout(env):
    """同日同票首次突破与首次跌破都是 seq=1：seq_id 不带 kind 时后者会被认领键吞掉。"""
    from notify.dispatcher import load_sig
    env.disp.handle([ev(env, "sh600519")])                      # breakout
    # 把同一只票改成跌破事件（同一天、seq 仍为 1）
    env.conn.execute("UPDATE event_log SET kind='breakdown' WHERE id=?", (env.ids["sh600519"],))
    env.conn.commit()
    sig = load_sig(env.conn, env.ids["sh600519"])
    env.disp.handle([ev(env, "sh600519", kind="breakdown")])
    rows = env.conn.execute("SELECT kind, status FROM push_log ORDER BY rowid").fetchall()
    assert [r["kind"] for r in rows] == ["breakout", "breakdown"] if len(rows) == 2 else False
    seqs = [r[0] for r in env.conn.execute("SELECT seq_id FROM push_log")]
    assert len(seqs) == len(set(seqs))                          # 序列号不撞


def test_claim_is_atomic_across_processes(tmp_path):
    """两个连接同时认领必须是真原子：延迟事务下双方都会查到「没有」然后双发。"""
    import threading

    from core.db import connect as _connect, migrate as _migrate
    from notify.dispatcher import Dispatcher, load_sig

    path = tmp_path / "atom.db"
    base = _connect(path)
    _migrate(base)
    seed_sig_context(base, code="sh600519")
    base.commit()
    c1, c2 = _connect(path), _connect(path)
    ntfy = FakeNtfy()
    results = []
    barrier = threading.Barrier(2, timeout=10)

    def fire(c):
        disp = Dispatcher(c, ntfy, CFG, clock_at())
        sig = load_sig(c, 1)
        rows = [disp._row(sig, "breakout", "P0", seq_id="sig-600519-20261029-breakout-1")]
        barrier.wait()
        results.append(disp._push_rows(rows, disp._render(sig), claim=True))

    ts = [threading.Thread(target=fire, args=(c,)) for c in (c1, c2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == [False, True]        # 只有一边认领成功
    assert base.execute(
        "SELECT count(*) FROM push_log WHERE seq_id='sig-600519-20261029-breakout-1'"
    ).fetchone()[0] == 1
    assert len(ntfy.published) == 1


def test_sector_buffer_flush_groups_by_industry_and_kind(env):
    """同窗口混入不同行业/方向时必须分组发，标题不能是首只行业。"""
    from datetime import datetime as _dt
    from notify.dispatcher import load_sig as _ls

    env.conn.execute("UPDATE signals SET level='P0' WHERE code IN ('sh600519','sh600000','sh600036')")
    for code, ind in (("sh600519", "白酒"), ("sh600000", "银行"), ("sh600036", "白酒")):
        env.conn.execute("UPDATE stock_meta SET industry=? WHERE code=?", (ind, code))
    env.conn.commit()
    # 直接构造混合缓冲：两条白酒 + 一条银行落在同一窗口
    a = _ls(env.conn, env.ids["sh600519"])
    b = _ls(env.conn, env.ids["sh600036"])
    c = _ls(env.conn, env.ids["sh600000"])
    env.disp._sector = [(T0, a), (T0, b), (T0, c)]
    env.disp.clock._now = T0 + timedelta(seconds=int(CFG.notify.sector_merge.window_s) + 1)
    env.disp.tick()
    bodies = [r["body"] for r in env.conn.execute(
        "SELECT body FROM push_log WHERE seq_id LIKE 'batch-%'")]
    assert len(bodies) >= 2
    for b in bodies:
        if "白酒" in b:
            assert "银行" not in b and "600000" not in b
        if "银行" in b:
            assert "白酒" not in b


def test_batch_click_points_to_signals(env):
    """合并条点进去是列表页（/signals），不是单票页。"""
    from datetime import datetime as _dt
    from notify.dispatcher import load_sig as _ls
    for code in ("sh600519", "sh600000", "sh600036"):
        env.conn.execute("UPDATE signals SET level='P0' WHERE code=?", (code,))
        env.conn.execute("UPDATE stock_meta SET industry='白酒' WHERE code=?", (code,))
    env.conn.commit()
    for code in ("sh600519", "sh600000", "sh600036"):
        env.disp._route(_ls(env.conn, env.ids[code]))
    env.disp.clock._now = T0 + timedelta(seconds=int(CFG.notify.sector_merge.window_s) + 1)
    env.disp.tick()
    body = env.conn.execute("SELECT body FROM push_log WHERE seq_id LIKE 'batch-%'").fetchone()
    # click 不入库，只能信渲染函数；直接调渲染验证
    from notify.templates import render_batch
    import json as _json
    sig = _ls(env.conn, env.ids["sh600519"])
    msg = render_batch([sig] * 3, b"x" * 32, CFG)
    # 链接现在带免密票据（`?t=..&exp=..`），所以不能直接 endswith
    from urllib.parse import urlsplit

    assert urlsplit(msg.click).path == "/signals"


def test_confirm_result_audit_rows_do_not_consume_daily_cap(env):
    """确认审计行不是推送：它若计入日熔断，点满 60 次确认后 P1 全被压掉。"""
    cap = CFG.notify.daily_cap
    for i in range(cap):
        env.conn.execute(
            "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, trade_date, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (T0.isoformat(), "sh600519", "confirm_result", "P2", f"confirm-result-{i}", 1,
             "ok", 20261030, "accepted"))            # 与 env 里的 clock 同日
    env.conn.commit()
    assert env.disp._capped() is False
    # 同样的行数换成真推送 → 必须熔断
    env.conn.execute("UPDATE push_log SET kind='breakout'")
    env.conn.commit()
    assert env.disp._capped() is True


# ---- 演练闸必须覆盖所有外发出口（原先只有 AgentPusher 有）----------------


def test_drill_blocks_dispatcher_publish(env, monkeypatch):
    """`SS_DRILL=1` 时 Dispatcher **一次都不能发**，也不能写 accepted。

    原先只有 `agent/push.py` 有闸，jobs 的告警（通道 A/C）、开盘简报、
    收盘摘要走 Dispatcher 自己的 NtfyClient，演练时照发不误。
    """
    monkeypatch.setenv("SS_DRILL", "1")
    env.disp.alert("index_move", "测试告警", level="P1")
    assert env.ntfy.published == [], f"演练仍调用了 publish：{env.ntfy.published}"
    rows = env.conn.execute("SELECT status FROM push_log").fetchall()
    assert all(r[0] != "accepted" for r in rows), f"演练写了 accepted：{rows}"


def test_drill_off_still_publishes(env, monkeypatch):
    monkeypatch.delenv("SS_DRILL", raising=False)
    env.disp.alert("index_move", "测试告警", level="P1")
    assert len(env.ntfy.published) == 1


def test_batch_dedupes_same_stock(env):
    """同一批里同一只票只留一行——重复信号不该让「11 只同向异动」其实只有 4 只。

    曾经`batch-804` 里 300052 出现 9 次，正文写「11 只同向异动」，
    用户看到的是同一只票刷屏。
    """
    from notify.dispatcher import load_sig as _ls

    one = _ls(env.conn, env.ids["sh600519"])
    two = _ls(env.conn, env.ids["sh600000"])
    items = [one, one, one, two, two]
    env.disp._send_batch(items, level="P2")
    codes = [r[0] for r in env.conn.execute(
        "SELECT code FROM push_log WHERE seq_id LIKE 'batch-%' ORDER BY rowid").fetchall()]
    assert len(codes) == len(set(codes)), f"批次内有重复票：{codes}"
    assert len(codes) == 2, f"应只写两只：{codes}"


def test_seq_id_uses_event_date_not_pool_date(env):
    """`seq_id` 里的日期必须是**事件当天**，不是池子日期（T−1）。

    曾经当天所有信号都写成 `sig-300002-20261110-breakout-1`，
    `sig.trade_date` 取的是 `pool.trade_date`（昨天），而事件其实是今天发生的。
    后果：事后按 seq_id 对账会把今天的信号记到昨天；池子若某天没重建，
    连续两天会生成**同一个 seq_id**，ntfy 会把第二天的消息当成第一天的更新。
    """
    from notify.dispatcher import load_sig as _ls, _seq_id

    ev = env.conn.execute(
        "SELECT id FROM event_log WHERE kind IN ('breakout','breakdown')"
        " ORDER BY id DESC LIMIT 1").fetchone()
    sig = _ls(env.conn, ev[0])
    # **前提：两个日期确实不同**，否则后面两条断言在任何实现下都成立
    # （写成 `!= ... or True` 时恒真，等于没断言——这条曾经就是假绿）。
    assert sig.trade_date != sig.event_trade_date, (
        f"fixture 本身没构造出「池子日期≠事件日期」，本条测试无意义："
        f"trade_date={sig.trade_date} event_trade_date={sig.event_trade_date}")
    assert str(sig.event_trade_date) in _seq_id(sig), _seq_id(sig)
    assert str(sig.trade_date) not in _seq_id(sig), _seq_id(sig)


def test_batch_dedup_keeps_different_kinds(env):
    """同批去重按 `(code6, kind)`：同一只票先 breakout 后 breakdown **必须都保留**。

    只按 `code6` 去重会把「先涨破、又跌破」压成一条——那是两个相反的事实，
    合并掉等于漏报。
    """
    from notify.dispatcher import load_sig as _ls

    from dataclasses import replace

    one = _ls(env.conn, env.ids["sh600519"])
    other = replace(one, kind="breakdown", seq=9)
    env.disp._send_batch([one, other], level="P2")
    kinds = [r[0] for r in env.conn.execute(
        "SELECT kind FROM push_log WHERE seq_id LIKE 'batch-%' ORDER BY rowid").fetchall()]
    assert set(kinds) == {"breakout", "breakdown"}, f"同票不同 kind 被压掉了：{kinds}"


def test_pool_alerts_is_pushed(env):
    """池子告警必须真的推出去。

    `jobs/after_close.py` 用 `getattr(dispatcher, "pool_alerts", None)` 调它——
    `Dispatcher` 上**没有这个方法**，于是每次出池都只记一条 warning
    「告警未推送」，`pool_low` / `pool_overflow` 从来没到过手机。
    `getattr` 兜底让出池本身不失败，缺陷因此一直静默。

    实机证据：某天 `cn_star` 池子只有十几只（低于阈值 30），触发 `pool_low`，
    日志 `WARNING after_close: dispatcher 没有 pool_alerts，告警未推送: ['pool_low']`。
    """
    from core.strategy.pool import PoolReport

    report = PoolReport(market="cn_star", candidates=14, size=14, funnel={},
                        rejects={}, alerts=["pool_low"])
    assert hasattr(env.disp, "pool_alerts"), \
        "Dispatcher 没有 pool_alerts，池子告警永远推不出去"
    env.disp.pool_alerts(report)

    rows = env.conn.execute(
        "SELECT kind, level FROM push_log WHERE kind LIKE '%pool%'").fetchall()
    assert rows, "调用后 push_log 里没有任何池子告警记录"


def test_cap_is_enforced_inside_the_claim_transaction(env):
    """**额度判断必须在认领事务里**，否则两个服务会一起超限。

    原来 `_route` 在 `BEGIN IMMEDIATE` **之前**就查了一次 `_capped()`：
    jobs 的通道 A 与 agent 的通道 B 各自读到 59/60 然后都放行，实际发到 61。
    SQLite 的写串行化救不了"事务外已经做出的额度决定"。

    这里直接把额度填到 `cap - 1`，再在同一连接上模拟"另一个人刚提交了一条"，
    然后走真实的认领路径——事务里重新读到的额度必须是满的。
    """
    env.conn.execute("UPDATE signals SET level='P1'")
    env.conn.commit()
    cap = int(CFG.notify.daily_cap)
    for i in range(cap - 1):
        write_push_log(env.conn, code=f"8{i:05d}", kind="alert", ts=T0.isoformat(), level="P2")
    env.conn.commit()

    # 另一个人（另一个服务）抢在判断之前提交了第 cap 条
    write_push_log(env.conn, code="899999", kind="alert", ts=T0.isoformat(), level="P2")
    env.conn.commit()

    # 直接走 ： 前面还有去重/合并等分支，会掩盖额度这条路
    from notify.dispatcher import load_sig as _ls

    sig = _ls(env.conn, env.ids["sh600519"])

    from notify.policy import NON_PUSH_KINDS, pushed_today as _pt

    n = _pt(env.conn, exclude_segments=(2,), exclude_kinds=NON_PUSH_KINDS, day=20261030)
    assert n >= cap, f"前提不成立：额度只用到 {n}/{cap}"
    assert env.disp._capped() is True, "前提不成立：_capped() 没判超限"

    env.disp._route(sig)

    assert statuses(env.conn)[-1] == "suppressed", (
        "额度已经满了却仍然放行：说明额度是在写事务之外判断的")


def test_p0_alert_is_not_blocked_by_the_daily_cap(env):
    """**日额度打满时 P0 运维告警仍必须发出去**。

    v0.2.69 把额度判定从 `_route` 移进 `_push_rows` 事务时，默认值取成了 True，
    于是 `alert()` 这条从未被 `_route` 管过的路径也开始受额度限制——
    日额度一满，通道故障/磁盘告警会被静默 suppressed，用户永远收不到。
    v0.2.68 的 `_push_rows` 里压根没有额度检查。
    """
    cap = env.cfg.notify.daily_cap
    # 先把当日额度打满（用真实的信号推送路径）
    for i in range(cap):
        env.disp._push_log(row={"code": None, "kind": "breakout", "level": "P2",
                                "seq_id": f"fill-{i}", "event_id": None,
                                "signal_id": None, "segment": 1,
                                "trade_date": clock_at().today()},
                           msg=render_alert("fill", f"占位 {i}", env.cfg, level="P2"),
                           enforce_cap=True)
    assert env.disp._capped() is True, "前提不成立：额度没打满"

    before = len(env.ntfy.published)
    env.disp.alert("disk", "磁盘快满了", "P0")
    assert len(env.ntfy.published) > before, (
        "额度打满后 P0 告警被挡掉了：运维告警不该受行情推送额度约束")
    row = env.conn.execute(
        "SELECT status FROM push_log WHERE kind='disk' ORDER BY id DESC LIMIT 1").fetchone()
    assert row and row[0] == "accepted", f"P0 告警被抑制成 {row[0] if row else '无行'}"
