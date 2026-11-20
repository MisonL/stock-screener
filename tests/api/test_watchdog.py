"""watchdog 与 agent 空壳。"""
import json
import subprocess
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from api.watchdog import check, in_watch_window
from core.clock import FixedClock, SH
from core.db import connect, migrate
from tests.helpers import SH as _SH


def iso(dt):
    return dt.isoformat()


class Disp:
    def __init__(self):
        self.alerts = []

    def alert(self, kind, text, level="P1"):
        self.alerts.append((kind, text, level))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "w.db")
    migrate(c)
    for d in (20261028, 20261029, 20261030, 20261102):
        c.execute("INSERT OR IGNORE INTO daily_bars VALUES ('sh000300', ?, 3000,3000,3000,3000,0,0,'x')", (d,))
    c.commit()
    return c


def set_beat(conn, service, ago_s, detail=None, clock=None):
    now = (clock or FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH))).now()
    conn.execute("INSERT OR REPLACE INTO service_heartbeat(service, last_beat_at, detail) VALUES (?,?,?)",
                 (service, iso(now - timedelta(seconds=ago_s)),
                  json.dumps(detail) if detail else None))
    conn.commit()


def run(conn, clock):
    disp = Disp()
    fired = check(conn, clock, disp)
    return fired, disp


# ---- watchdog ------------------------------------------------------------

def test_agent_stale_in_session_alerts(conn):
    fired, disp = run(conn, FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH)))
    assert fired == []
    set_beat(conn, "agent", 181)
    fired, disp = run(conn, FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH)))
    assert fired == ["agent_down"] and disp.alerts[0][2] == "P0"


def test_jobs_stale_in_session_alerts(conn):
    set_beat(conn, "jobs", 181)
    fired, _ = run(conn, FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH)))
    assert fired == ["jobs_down"]


def test_jobs_loop_stalled_alerts(conn):
    """心跳新鲜但 loop_last_tick_at 停了 200 秒 → D9 的「循环未推进」。"""
    now = FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH))
    set_beat(conn, "jobs", 10, detail={"loop_last_tick_at": (now.now() - timedelta(seconds=200)).timestamp()})
    fired, disp = run(conn, now)
    assert fired == ["jobs_down"] and "循环未推进" in disp.alerts[0][1]


def test_both_stale_returns_two(conn):
    set_beat(conn, "agent", 181)
    set_beat(conn, "jobs", 181)
    fired, _ = run(conn, FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH)))
    assert set(fired) == {"agent_down", "jobs_down"}


def test_no_alert_at_night(conn):
    set_beat(conn, "jobs", 9999)
    set_beat(conn, "agent", 9999)
    fired, _ = run(conn, FixedClock(datetime(2026, 10, 30, 23, 0, tzinfo=SH)))
    assert fired == []


def test_no_alert_outside_watch_window(conn):
    set_beat(conn, "jobs", 9999)
    fired, _ = run(conn, FixedClock(datetime(2026, 10, 30, 12, 0, tzinfo=SH)))   # 午休
    assert fired == []


def test_no_alert_on_non_trading_day(conn):
    set_beat(conn, "jobs", 9999)
    fired, _ = run(conn, FixedClock(datetime(2026, 10, 31, 10, 0, tzinfo=SH)))   # 周六
    assert fired == []


def test_maintenance_mode_exits_first(conn):
    conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES ('maintenance.flag','1')")
    conn.commit()
    set_beat(conn, "jobs", 9999)
    set_beat(conn, "agent", 9999)
    fired, disp = run(conn, FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH)))
    assert fired == [] and disp.alerts == []


def test_no_repeat_within_30min(conn):
    now = FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH))
    set_beat(conn, "jobs", 181)
    conn.execute("INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
                 " VALUES (?, NULL, 'jobs_down', 'P0', 'x', 1, 20261030, 'accepted')",
                 (iso(now.now() - timedelta(minutes=5)),))
    conn.commit()
    fired, disp = run(conn, now)
    assert fired == [] and disp.alerts == []          # 30 分钟内已成功告警过


def test_failed_alert_does_not_block_retry(conn):
    """ntfy 故障期间的 failed 告警行不算「已告警」，重试必须照发。"""
    now = FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH))
    set_beat(conn, "jobs", 181)
    conn.execute("INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
                 " VALUES (?, NULL, 'jobs_down', 'P0', 'x', 1, 20261030, 'failed')",
                 (iso(now.now() - timedelta(minutes=5)),))
    conn.commit()
    fired, disp = run(conn, now)
    assert fired == ["jobs_down"] and disp.alerts


def test_watch_window_excludes_lunch():
    assert in_watch_window(datetime(2026, 10, 30, 9, 15, tzinfo=SH))
    assert not in_watch_window(datetime(2026, 10, 30, 12, 0, tzinfo=SH))
    assert in_watch_window(datetime(2026, 10, 30, 15, 4, tzinfo=SH))
    assert not in_watch_window(datetime(2026, 10, 30, 15, 6, tzinfo=SH))


# ---- agent 空壳 ----------------------------------------------------------

def test_agent_main_imports_llm_in_subprocess():
    """F-30 的批2 版：子进程 import，避免同进程更早的 import 污染 sys.modules。

    批1 断言 agent **不** import llm；Task 11 换真实主循环后必须改写——agent 要调
    `LlmClient`，import 不到 llm 才是有问题的。
    """
    out = subprocess.run(
        [sys.executable, "-c", "import sys, agent.main; print('llm' in sys.modules)"],
        capture_output=True, text=True, check=True,
        env={"PYTHONPATH": "/app", "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    assert out.stdout.strip() == "True"


def test_agent_main_does_not_pull_openai_stack():
    """openai 只在真正发请求时才需要：import agent.main 不该把它拉起来。"""
    out = subprocess.run(
        [sys.executable, "-c", "import sys, agent.main; print('openai' in sys.modules)"],
        capture_output=True, text=True, check=True,
        env={"PYTHONPATH": "/app", "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    assert out.stdout.strip() == "False"


def test_agent_stub_sleeps_on_non_trading_day(conn):
    from agent.main import should_wake

    assert should_wake(conn, datetime(2026, 10, 31, 10, 0, tzinfo=SH)) is False   # 周六
    assert should_wake(conn, datetime(2026, 10, 30, 10, 0, tzinfo=SH)) is True


def test_next_session_start():
    from agent.main import next_session_start

    now = datetime(2026, 10, 30, 16, 0, tzinfo=SH)
    nxt = next_session_start(now)
    assert nxt.strftime("%m%d%H%M") == "10310915"
    same_day = next_session_start(datetime(2026, 10, 30, 8, 0, tzinfo=SH))
    assert same_day.strftime("%d%H%M") == "300915"


def test_heartbeat_thread_writes_every_30s(tmp_path):
    from agent.heartbeat import Heartbeat

    conn = connect(tmp_path / "h.db")
    migrate(conn)
    hb = Heartbeat(lambda: conn, service="agent", every_s=1,
                   detail_provider=lambda: {"loop_last_tick_at": 1.0})
    hb.beat()
    hb.beat()
    row = conn.execute("SELECT last_beat_at, detail FROM service_heartbeat WHERE service='agent'").fetchone()
    assert row is not None and json.loads(row[1]) == {"loop_last_tick_at": 1.0}


def test_heartbeat_detail_provider_error_keeps_beating(tmp_path):
    from agent.heartbeat import Heartbeat

    conn = connect(tmp_path / "h.db")
    migrate(conn)

    def bad():
        raise RuntimeError("provider 炸了")

    Heartbeat(lambda: conn, service="agent", every_s=1, detail_provider=bad).beat()
    row = conn.execute("SELECT detail FROM service_heartbeat WHERE service='agent'").fetchone()
    assert json.loads(row[0]) == {}


def test_heartbeat_start_actually_writes_in_background(tmp_path):
    """`start()` 必须真的起线程写心跳：把 start 改成空函数时这条要变红。"""
    import threading
    import time as _time

    from agent.heartbeat import Heartbeat

    conn = connect(tmp_path / "h2.db")
    migrate(conn)
    hb = Heartbeat(lambda: conn, service="agent", every_s=1, detail_provider=lambda: {"n": 1})
    hb.start()
    try:
        _time.sleep(1.5)
        row = conn.execute(
            "SELECT last_beat_at FROM service_heartbeat WHERE service='agent'").fetchone()
        assert row is not None
        threads = [t for t in threading.enumerate() if t is not threading.main_thread()]
        assert any(t.daemon for t in threads)
    finally:
        hb.stop()


def test_missing_heartbeat_rows_are_judged_dead(conn):
    """新库/首次部署时 agent 与 jobs 都没有心跳行：必须告警，不能静默。"""
    from datetime import datetime

    from api.watchdog import check
    from tests.helpers import SH
    from core.clock import FixedClock

    clock = FixedClock(datetime(2026, 10, 30, 10, 30, tzinfo=SH))
    # 第一次检查只记宽限起点（否则每次重启都会误报两条 P0）
    assert check(conn, clock, Disp()) == []
    clock._now = datetime(2026, 10, 30, 10, 35, tzinfo=SH)      # 5 分钟后仍无心跳
    fired = check(conn, clock, Disp())
    assert "agent_down" in fired and "jobs_down" in fired


# ---- 时段起点基线（午休/隔夜不该算「卡死」）------------------------------


def _seed_turn(conn, ts):
    conn.execute(
        "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
        " fallback, market) VALUES (?, 'heartbeat', 'd', 1, 'silent', 0, 'cn_main')", (ts,))
    conn.commit()


def test_afternoon_open_does_not_flag_healthy_agent(conn):
    """13:00 开盘首轮不能因为「上午最后一回合是 11:20」就报 P1。

    agent 在 11:30–13:00 休眠（`in_session` 排除午休），所以 13:00 那一刻
    「距最后一回合 100 分钟」是**正常的**。用全表 `max(ts)` 算龄会让每个交易日
    午后开盘都误报一次 P1，而误报的告警会被用户直接忽略。
    """
    _seed_turn(conn, "2026-10-30T11:20:00+08:00")
    clock = FixedClock(datetime(2026, 10, 30, 13, 0, tzinfo=SH))
    set_beat(conn, "agent", 20, clock=clock)
    fired, disp = run(conn, clock)
    assert "agent_stalled" not in fired, \
        f"13:00 开盘就报 agent_stalled（上午的回合被当成午后卡死）：{disp.alerts}"

    # 但确实卡住时仍要报：13:40 还停在 11:20 就是真的没跑
    clock2 = FixedClock(datetime(2026, 10, 30, 13, 40, tzinfo=SH))
    set_beat(conn, "agent", 20, clock=clock2)
    fired2, _ = run(conn, clock2)
    assert "agent_stalled" in fired2, "午后真卡住却不再报警了"


def test_turn_churn_detects_runaway_loop(conn):
    """**回合数暴增必须报警**——2026-11-19 的空转死循环没有任何规则能发现。

    现场：`release_alarm()` 把失败闹钟退回 `pending`，`_once()` 每 2 秒轮询，
    一条过期闹钟陷入「到期→失败→退回→2 秒后又到期」，一直转到收盘，
    全天写了 17998 行 `agent_turn`（正常日 78–131 行）。

    原有四条规则一条都没报：
      · `agent_down` —— 心跳正常（进程活着）
      · `agent_stalled` —— 恰恰相反，回合多得离谱
      · `llm_down` —— 11:24 报过一次后进入 30 分钟去重，而且"连续失败"
        升级成"疯狂失败"在数量上没有任何阈值
      · `jobs_down` —— jobs 完全正常

    这里复刻事故量级（每分钟数条），断言 `agent_churn` 触发。
   """
    from api.watchdog import AGENT_TURN_CHURN_PER_HOUR

    clock = FixedClock(datetime(2026, 10, 30, 10, 0, tzinfo=SH))
    now = clock.now()
    n = AGENT_TURN_CHURN_PER_HOUR + 50
    # 均匀铺在最近 50 分钟内（铺满 1 小时的话最早几条会滑出窗口）
    step = 3000 / n
    for i in range(n):
        conn.execute(
            "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
            " fallback, market) VALUES (?,?,?,?,?,?,?)",
            ((now - timedelta(seconds=i * step)).isoformat(), "alarm", "空转", 0,
             "error", 0, "cn_main"))
    conn.commit()

    fired, _disp = run(conn, clock)
    assert "agent_churn" in fired, (
        f"一小时内 {n} 个回合没有报警（阈值 {AGENT_TURN_CHURN_PER_HOUR}）：{fired}")


def test_normal_turn_volume_does_not_alarm(conn):
    """**正常日的回合量不能报**——误报会把真故障淹掉。

    数值必须来自生产实测，不能拍脑袋。这个阈值错过两次，两次都是**样本选错了**：

    · 写 50 时，对着"正常日 11-18 午后滚动一小时正好 50 个"就会误报；
    · 改 200 后，只量了正常日（峰值 50，看着有 4 倍余量）。但 **11-20 是暴跌日**，
      模型每 2~6 分钟排一条回访闹钟，45 分钟建了 96 条，实测滚动一小时 **169 个**，
      离 200 只剩 16% 余量。**暴跌日恰恰是模型最活跃、最该被监控的一天。**

    这里复刻那个 169（暴跌日的合法峰值），并额外覆盖"窗口之外的行不能算进来"：
    SQL 少了 `ts <= now` 上界时，回放过去某个时刻会把之后发生的所有行都算成
    "最近一小时"（实测干跑 11-18 14:30 得到 18066，正确值是 45）。
    """
    clock = FixedClock(datetime(2026, 11, 20, 10, 0, tzinfo=SH))
    now = clock.now()
    for i in range(169):                    # 暴跌日的合法峰值（11-20 实测）
        conn.execute(
            "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
            " fallback, market) VALUES (?,?,?,?,?,?,?)",
            ((now - timedelta(seconds=int(i * 3550 / 169))).isoformat(), "alarm", "暴跌日",
             2, "silent", 0, "cn_main"))
    for i in range(1200):                   # 窗口之外（未来）：没有上界就会被算进来
        conn.execute(
            "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
            " fallback, market) VALUES (?,?,?,?,?,?,?)",
            ((now + timedelta(minutes=10 + i)).isoformat(), "alarm", "未来", 2,
             "silent", 0, "cn_main"))
    conn.commit()

    fired, _disp = run(conn, clock)
    assert "agent_churn" not in fired, (
        f"正常回合量被误报为空转：{fired}（可能把窗口之外的行也算进来了）")


def test_every_watchdog_kind_has_a_human_title():
    """**每种 watchdog 告警都要有中文标题**，新增 kind 忘了配也拦得住。

    `render_alert` 在查不到标题时会回落到 `kind` 本身——那是个静默降级：
    告警照发，但手机上显示 "agent_churn"，用户不知道发生了什么。
    这条测试把 `AlertKind` 和 `_ALERT_TITLE` 绑在一起，加新规则时强制补标题。
    """
    import typing

    from notify.templates import _ALERT_TITLE
    from api.watchdog import AlertKind

    missing = [k for k in typing.get_args(AlertKind) if k not in _ALERT_TITLE]
    assert not missing, f"这些告警 kind 没有中文标题：{missing}"


def test_churn_still_alerts_when_budget_exhausted(conn):
    """**预算耗尽时空转更要报**，不能因为 `budget_exhausted` 而静音。

    另外两条规则（`agent_stalled`、`llm_down`）都带 `not _budget_exhausted(...)`：
    预算耗尽确实能解释"没有回合"和"没有成功回合"，那是预期停止，不该报。

    但 `agent_churn` 正好相反——2026-11-19 那次事故的**根因就是预算耗尽**：
    闹钟回合在预检里立刻返回 error（rounds=0、0 毫秒）→ 记账 → 闹钟退回 →
    2 秒后再来。全天 17998 行。给这条规则加上同样的 gate，等于把
    "最该报的那种空转"永久静音。
    """
    from api.watchdog import AGENT_TURN_CHURN_PER_HOUR

    clock = FixedClock(datetime(2026, 11, 19, 14, 30, tzinfo=SH))
    now = clock.now()
    n = AGENT_TURN_CHURN_PER_HOUR + 50
    step = 3000 / n
    for i in range(n):
        conn.execute(
            "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
            " fallback, market) VALUES (?,?,?,?,?,?,?)",
            ((now - timedelta(seconds=i * step)).isoformat(), "alarm", "空转", 0,
             "error", 0, "cn_main"))
    conn.commit()
    # 心跳声明预算已耗尽（这正是事故当天的状态）
    set_beat(conn, "agent", 0, detail={"budget_exhausted": True}, clock=clock)

    fired, _disp = run(conn, clock)
    assert "agent_churn" in fired, (
        f"预算耗尽时把空转告警静音了：{fired}——而这正是事故当天的状态")


def test_churn_threshold_stays_in_the_measured_corridor():
    """**阈值必须落在实测走廊里**，不能跟着别的测试一起涨价（第四轮自查）。

    另外两条规则测试写的是 `AGENT_TURN_CHURN_PER_HOUR + 50`——它们**跟着常量走**，
    所以把阈值改成 100000 时，它们会老老实实插 100050 行、然后照样报绿。
    也就是说「阈值被误设成天文数字」这个真实故障，现有测试**一条都抓不到**。

    这个阈值错过两次（50 → 200 → 1000），两次都是**样本选错**，所以这里把
    它钉在实测走廊内：
      · 下界 300：暴跌日（11-20）滚动一小时实测 169，需要明显余量；
      · 上界 5000：事故日 17998/小时，要能在几分钟内报出来。
    走廊外就说明又有人拍脑袋了。
    """
    from api.watchdog import AGENT_TURN_CHURN_PER_HOUR

    assert 300 <= AGENT_TURN_CHURN_PER_HOUR <= 5000, (
        f"阈值 {AGENT_TURN_CHURN_PER_HOUR} 跑出实测走廊 [300, 5000]："
        "低于 300 会在暴跌日误报（实测 169），高于 5000 会让事故报得太晚")


def test_churn_fires_within_minutes_of_a_real_incident(conn):
    """**事故要在几分钟内被抓住，而不是等一小时**（第四轮自查）。

    滚动一小时的判据有个隐含延迟：事故刚起时窗口里还是空的，要等累积。
    11-19 事故是 14:05 起每分钟约 300 个回合 —— 到 1000 需要约 3.3 分钟。
    这个延迟可以接受（事故本身持续了一小时），但如果有人把阈值调到 5000，
    延迟就变成 16 分钟，而 watchdog 的 `AGENT_TURN_STALE_S = 900`（15 分钟）
    反而会先以「回合陈旧」的名义报——诊断方向就歪了。

    这里直接按 300/分钟 复刻事故，断言既能在 25 分钟后报出来，
    又不会在只有 3 分钟的量（900 个）时就误报。
    """
    from api.watchdog import AGENT_TURN_CHURN_PER_HOUR
    from tests.helpers import SH as _SH

    def _burst(c, end, minutes):
        for m in range(minutes):
            for _ in range(300):
                c.execute(
                    "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds,"
                    " decision, fallback, market) VALUES (?,?,?,?,?,?,?)",
                    ((end - timedelta(minutes=m)).isoformat(), "alarm", "事故", 2,
                     "silent", 0, "cn_main"))
        c.commit()

    # 25 分钟的量：必须报
    clock = FixedClock(datetime(2026, 11, 19, 14, 30, tzinfo=SH))
    set_beat(conn, "agent", 10, clock=clock)
    set_beat(conn, "jobs", 10, clock=clock)
    _burst(conn, clock.now(), 25)
    d = Disp()
    check(conn, clock, d)
    assert [a for a in d.alerts if a[0] == "agent_churn"], (
        f"每分钟 300 个回合持续 25 分钟没报（阈值 {AGENT_TURN_CHURN_PER_HOUR}）")

    # 3 分钟的量（900 个）：还不该报——证明判据不是"随便就报"
    c2 = connect(":memory:")
    migrate(c2)
    clock2 = FixedClock(datetime(2026, 11, 19, 14, 8, tzinfo=SH))
    set_beat(c2, "agent", 10, clock=clock2)
    set_beat(c2, "jobs", 10, clock=clock2)
    _burst(c2, clock2.now(), 3)
    d2 = Disp()
    check(c2, clock2, d2)
    assert not [a for a in d2.alerts if a[0] == "agent_churn"], (
        "只有 900 个回合（3 分钟）就报了：阈值调得太低，暴跌日会误报")


def test_slow_runaway_is_caught_by_the_short_window(conn):
    """**低速 runaway 要能在几分钟内报出来，不能等一小时累计**（第四轮评审）。

    滚动一小时是累计量：每分钟 20 个（≈4800/日，正常日 131 行的 36 倍）
    要跑满 48 分钟才越过 1000 线；`_alert` 还有 `REPEAT_S=1800` 去重，
    真推出去可能已经一小时后。而 5 分钟窗口 3~4 分钟就能抓到。

    这里复刻"每分钟 20 个、已经持续 6 分钟"（120 个），
    它**不超过** 1000 的一小时线（120 < 1000），但必须超过 5 分钟线。
    """
    from api.watchdog import AGENT_TURN_CHURN_PER_5MIN

    clock = FixedClock(datetime(2026, 11, 20, 10, 30, tzinfo=SH))
    now = clock.now()
    set_beat(conn, "agent", 10, clock=clock)
    set_beat(conn, "jobs", 10, clock=clock)

    # 7 分钟 × 每分钟 20 个 = 140 个；5 分钟窗口内 100 个 > 阈值 120？不，
    # 所以压到每分钟 25 个：5 分钟窗口 125 > 120，一小时累计 150 << 1000
    for m in range(6):
        for _ in range(25):
            conn.execute(
                "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds,"
                " decision, fallback, market) VALUES (?,?,?,?,?,?,?)",
                ((now - timedelta(seconds=m * 60 + 1)).isoformat(), "alarm", "低速",
                 2, "silent", 0, "cn_main"))
    conn.commit()

    hour_n = conn.execute("SELECT count(*) FROM agent_turn WHERE ts > ? AND ts <= ?",
                          ((now - timedelta(hours=1)).isoformat(),
                           now.isoformat())).fetchone()[0]
    five_min_n = conn.execute(
        "SELECT count(*) FROM agent_turn WHERE ts > ? AND ts <= ?",
        ((now - timedelta(minutes=5)).isoformat(), now.isoformat())).fetchone()[0]
    assert hour_n < 1000, f"前提不成立：一小时线本该报不出来（{hour_n} < 1000）"
    assert five_min_n > AGENT_TURN_CHURN_PER_5MIN, (
        f"前提不成立：5 分钟窗口 {five_min_n} 应超过阈值 {AGENT_TURN_CHURN_PER_5MIN}")

    d = Disp()
    check(conn, clock, d)
    assert [a for a in d.alerts if a[0] == "agent_churn"], (
        f"每分钟 25 个回合持续 6 分钟（5 分钟窗口 {five_min_n} 个）没报："
        "低速 runaway 只能等一小时累计，发现太晚")


def test_short_window_does_not_fire_on_a_normal_busy_day(conn):
    """**5 分钟快线不能在暴跌日误报**（与上一条配套）。

    11-20 暴跌日实测每 5 分钟窗口峰值约 20 个回合。阈值必须高到不误报。
    这里放 3 个窗口、每窗口 20 个（共 60 个），全部低于阈值。
    """
    clock = FixedClock(datetime(2026, 11, 20, 10, 30, tzinfo=SH))
    now = clock.now()
    set_beat(conn, "agent", 10, clock=clock)
    set_beat(conn, "jobs", 10, clock=clock)

    for w in range(3):
        for _ in range(20):
            conn.execute(
                "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds,"
                " decision, fallback, market) VALUES (?,?,?,?,?,?,?)",
                ((now - timedelta(seconds=w * 5 * 60 + 1)).isoformat(), "alarm", "暴跌日",
                 2, "silent", 0, "cn_main"))
    conn.commit()

    d = Disp()
    check(conn, clock, d)
    assert not [a for a in d.alerts if a[0] == "agent_churn"], (
        "暴跌日正常的每 5 分钟 20 个回合被误报成 churn")


def _clock_103():
    from core.clock import FixedClock
    from tests.helpers import SH
    from datetime import datetime
    return FixedClock(datetime(2026, 11, 20, 10, 30, tzinfo=SH))


def test_budget_margin_probe_fires_when_reserve_is_too_thin(conn):
    """**预扣余量不足要报警**（v0.2.103）。

    `CHARS_TO_TOKENS` 是按 11-20 的实测校准的（0.7，对实测最大 0.530
    token/字符留 32% 余量）。样本之外永远可能更糟（中文更密集的段落），
    而预算低估 = 真实花费突破额度上限、且扣减没有退款路径。

    这条规则不会自动改系数——钱的事不自动决定——只负责在余量被吃掉时
    喊一声，别让它悄悄漂。
    """
    import api.watchdog as w

    clock = _clock_103()
    # 40 次调用：真实用量已经达到预扣的 95% —— 余量几乎没有了
    for _ in range(40):
        conn.execute(
            "INSERT INTO token_audit(ts, pool, real_in, real_out, estimated,"
            " reserve_in, reserve_out, attempt_seq) VALUES (?,?,?,?,?,?,?,?)",
            (clock.now().isoformat(), "agent", 9500, 200, 0, 10000, 2000, 1))
    conn.commit()

    fired = w.check(conn, clock, Disp())
    assert "budget_under_reserved" in fired, (
        f"真实用量已达预扣的 95% 却没报警：预算会悄悄低估，真实花费突破上限（{fired}）")


def test_budget_margin_probe_stays_quiet_with_healthy_margin(conn):
    """**余量健康时不该报警**——这条规则只在真出问题时响。

    系数 0.7 下，11-20 实测的 `real_in / reserve_in` 是
    `0.383 / 0.7 = 0.547`（预扣约为真实的 1.83 倍），远低于 0.85 阈值。
    如果正常情况就报警，规则会被静音，等于没有。

    **注意这个基线跟系数绑死**：系数改了这里要跟着改，否则测的是过期基线。
    """
    import api.watchdog as w

    clock = _clock_103()
    for _ in range(40):
        conn.execute(
            "INSERT INTO token_audit(ts, pool, real_in, real_out, estimated,"
            " reserve_in, reserve_out, attempt_seq) VALUES (?,?,?,?,?,?,?,?)",
            (clock.now().isoformat(), "agent", 5470, 200, 0, 10000, 2000, 1))
    conn.commit()

    fired = w.check(conn, clock, Disp())
    assert "budget_under_reserved" not in fired, (
        f"预扣是真实的 1.8 倍却报警：规则会在正常日就响，等于没有（{fired}）")


def test_margin_probe_ignores_the_chat_pool(conn):
    """**chat 池的行不能稀释 agent 池的中位值**（第六轮评审 S1）。

    探针是这次"钱"改动**唯一**的运行时守卫。如果把 chat 的行也算进来，
    agent 真低估时可能报不出来——chat 池的正文占比更高、历史更长，
    `real/reserve` 与 agent 池不同。

    这里造 60 条 chat 行（比值极低）+ 26 条 agent 行（比值 0.95），
    探针必须只看 agent 那一半。
    """
    import api.watchdog as w

    clock = _clock_103()
    # **顺序很重要**：探针取的是 `ORDER BY id DESC LIMIT 50`。
    # 先插 agent、再插 chat，窗口里就全是 chat 行——不限定 pool 时中位值是 0.1，
    # 探针沉默。反过来插的话两种实现都会响，测了个寂寞（第一版就是）。
    for _ in range(26):
        conn.execute(
            "INSERT INTO token_audit(ts, pool, real_in, real_out, estimated,"
            " reserve_in, reserve_out, attempt_seq) VALUES (?,?,?,?,?,?,?,?)",
            (clock.now().isoformat(), "agent", 9500, 200, 0, 10000, 2000, 1))
    for _ in range(60):
        conn.execute(
            "INSERT INTO token_audit(ts, pool, real_in, real_out, estimated,"
            " reserve_in, reserve_out, attempt_seq) VALUES (?,?,?,?,?,?,?,?)",
            (clock.now().isoformat(), "chat", 1000, 100, 0, 10000, 2000, 1))
    conn.commit()

    fired = w.check(conn, clock, Disp())
    assert "budget_under_reserved" in fired, (
        f"agent 池 26 条都是 0.95 却被 chat 池的 0.1 稀释掉了：探针会永久沉默（{fired}）")
