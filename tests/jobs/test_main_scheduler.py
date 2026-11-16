"""调度器装配（B3 每秒 tick、D9 心跳、D26 自检）与启动恢复（D10）。"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from core.clock import FixedClock
from core.db import connect, migrate
from core.intraday.detector import EventDetector
from jobs.eod import EodReport
from jobs.intraday import IntradayTick
from jobs import main as jobs_main
from jobs.main import make_scheduler, maintenance_on, self_check, startup_recovery
from tests.helpers import SH, _trading_days_rev

T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)


class Disp:
    def __init__(self):
        self.alerts = []
        self.handled = []
        self.tick_calls = 0
        self.briefs = []
        self.close_digests = 0

    def alert(self, kind, text, level="P1"):
        self.alerts.append((kind, text, level))

    def handle(self, events):
        self.handled.append(list(events))

    def tick(self):
        self.tick_calls += 1

    def open_brief(self, *a, **kw):
        self.briefs.append((a, kw))

    def close_digest(self):
        self.close_digests += 1


class Sectors:
    """`refresh_sector_members` 只需要 fetch_sector_list / fetch_sector_members。"""

    def fetch_sector_list(self):
        return [("new_blhy", "银行")]

    def fetch_sector_members(self, sector):
        return ["sh600519", "sz000001"]


@pytest.fixture
def env(tmp_path, CFG):
    conn = connect(tmp_path / "m.db")
    migrate(conn)
    # 交易日历（§6.4：sh000300 的日期集合）：到 20261029，20261030 由工作日兜底判定。
    for day in _trading_days_rev(20261029, 40):
        conn.execute(
            "INSERT INTO daily_bars VALUES ('sh000300', ?, 3000, 3000, 3000, 3000, 0, 0, 'backfill')",
            (day,))
    conn.commit()
    clock = FixedClock(T0)
    det = EventDetector(conn, clock, CFG)
    det.seed_today()
    tick = IntradayTick(CFG, conn, det, Disp(), clock)
    disp = Disp()
    sched, state = make_scheduler(CFG, conn, clock, tick=tick, dispatcher=disp,
                                  reg=SimpleNamespace(sina=Sectors()), data_dir=str(tmp_path))
    return SimpleNamespace(conn=conn, clock=clock, cfg=CFG, sched=sched, state=state, tick=tick,
                           disp=disp, tmp=tmp_path)


def _jobs(sched):
    return {j.id: j for j in sched.get_jobs()}


def test_second_level_tick_scheduled(env):
    jobs = _jobs(env.sched)
    assert "tick" in jobs
    assert jobs["tick"].trigger.interval.total_seconds() == 1
    # APScheduler 3.x 默认 misfire_grace_time=1 秒：盘中 GIL 争用会让任务被标 missed
    assert jobs["tick"].misfire_grace_time == 300


def test_all_jobs_have_misfire_and_single_instance(env):
    for job in env.sched.get_jobs():
        assert job.misfire_grace_time >= 300, job.id
        assert job.coalesce is True and job.max_instances == 1


def test_backup_registered_at_2330_with_long_grace(env):
    job = _jobs(env.sched)["backup"]
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "23" and fields["minute"] == "30"
    assert job.misfire_grace_time == 3600        # 晚一小时开机也要补跑


def test_eod_cleanup_selfcheck_registered(env):
    jobs = _jobs(env.sched)
    assert {"eod", "cleanup", "self_check", "heartbeat"} <= set(jobs)
    assert str([f for f in jobs["eod"].trigger.fields if f.name == "hour"][0]) == "15"
    assert str([f for f in jobs["cleanup"].trigger.fields if f.name == "minute"][0]) == "0"


def test_heartbeat_writes_detail_with_loop_tick(env):
    env.state["loop_last_tick_at"] = 123.0
    job = _jobs(env.sched)["heartbeat"]
    job.func()
    row = env.conn.execute("SELECT detail FROM service_heartbeat WHERE service='jobs'").fetchone()
    detail = json.loads(row[0])
    assert detail["loop_last_tick_at"] == 123.0
    assert "calls_by_scope" in detail and detail["calls_by_scope"]["pool"] == 0


def test_self_check_keys_fixed(env):
    result = self_check(env.cfg, env.conn, env.clock, reg=None)
    assert set(result) == {"upstream", "ntfy", "pool", "llm"}
    assert result["pool"] is False               # 没有任何池子


def test_tick_job_clears_loop_marker_outside_session(env):
    """非交易时段必须把标记清成 None：留着昨天的值，次日 09:15 一开闸就会被判「循环未推进」。"""
    env.state["loop_last_tick_at"] = "2026-10-29T15:04:59+08:00"
    env.clock._now = env.clock._now.replace(hour=8, minute=0)
    _jobs(env.sched)["tick"].func()
    assert env.state["loop_last_tick_at"] is None


def test_tick_job_writes_iso_timestamp_in_session(env):
    _jobs(env.sched)["tick"].func()
    # tick 自己的时间戳是 tick_last_at；loop_last_tick_at 只反映**快照循环**的进度
    value = env.state["tick_last_at"]
    assert isinstance(value, str) and value.endswith("+08:00")
    assert env.tick.dispatcher.tick_calls == 1


def test_maintenance_flag_gate(env):
    assert maintenance_on(env.conn) is False
    env.conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES ('maintenance.flag','1')")
    env.conn.commit()
    assert maintenance_on(env.conn) is True


# ---- D10 启动恢复 --------------------------------------------------------

def test_startup_in_session_starts_loops(env):
    class Loops:
        def __init__(self):
            self.started = False
            # 真实的 `SnapshotLoops` 有 `.lock`（= tick.lock）；`startup_recovery`
            # 起循环后要拿它再补跑，桩缺了这个属性就测不出真实调用路径
            import threading as _t

            self.lock = _t.Lock()

        def start(self):
            self.started = True

    loops = Loops()
    out = startup_recovery(env.cfg, env.conn, env.clock, reg=None, tick=env.tick, loops=loops)
    assert out["loops_started"] and loops.started


def test_startup_refresh_actions_catchup(env):
    calls = []
    out = startup_recovery(env.cfg, env.conn, env.clock, reg=None,
                           refresh_actions=lambda: calls.append("actions") or 1,
                           refresh_suspensions=lambda: calls.append("susp") or 1)
    # **顺序是 susp 在前**：停牌 1 个请求、分红 100 多个，先做停牌才不会
    # 等分红打完再撞上游限频（见 jobs/main.py 里的注释）
    assert out["refresh_actions"] and calls == ["susp", "actions"]
    assert env.conn.execute(
        "SELECT value FROM job_state WHERE name='refresh_actions.last'").fetchone()[0] == "20261030"

    out2 = startup_recovery(env.cfg, env.conn, env.clock, reg=None,
                            refresh_actions=lambda: calls.append("again") or 1)
    assert out2["refresh_actions"] is False and calls == ["susp", "actions"]      # 7 天内不重复


def test_startup_after_seven_days_refreshes_again(env):
    env.conn.execute("INSERT INTO job_state(name, value) VALUES ('refresh_actions.last','20261001')")
    env.conn.commit()
    calls = []
    out = startup_recovery(env.cfg, env.conn, env.clock, reg=None,
                           refresh_actions=lambda: calls.append("actions") or 1)
    assert out["refresh_actions"] and calls == ["actions"]


def test_startup_scans_closed_day_gaps(env):
    cal_days = [r[0] for r in env.conn.execute("SELECT date FROM daily_bars WHERE code='sh000300'")]
    if not cal_days:
        from tests.helpers import _trading_days_rev

        for day in _trading_days_rev(20261029, 10):
            env.conn.execute("INSERT INTO daily_bars VALUES ('sh000300', ?, 3000,3000,3000,3000,0,0,'x')", (day,))
        env.conn.commit()
    out = startup_recovery(env.cfg, env.conn, env.clock, reg=None)
    assert out["eod_days"]                          # 有已收盘但没跑过 eod 的交易日
    assert all(d < 20261030 for d in out["eod_days"])


# ---- Codex 复核：cron 时区 / 交易日闸 / 七天判据 / 启动补跑 ------------------

def test_cron_triggers_use_shanghai_timezone(env):
    """容器是 UTC：CronTrigger 不显式给 timezone 时按本地（UTC）算，eod 会跑到北京 23:05。"""
    crons = [j for j in env.sched.get_jobs() if isinstance(j.trigger, CronTrigger)]
    assert {"eod", "cleanup", "backup", "self_check"} <= {j.id for j in crons}
    assert all(j.trigger.timezone == ZoneInfo("Asia/Shanghai") for j in crons), \
        {j.id: str(j.trigger.timezone) for j in crons}


def test_tick_job_skips_non_trading_day(env):
    """周六 10:00 落在 09:15–15:05 窗口内，但非交易日不得 tick。"""
    env.clock._now = datetime(2026, 10, 31, 10, 0, tzinfo=SH)      # 周六
    env.state["loop_last_tick_at"] = "2026-10-30T15:04:59+08:00"
    _jobs(env.sched)["tick"].func()
    assert env.state["loop_last_tick_at"] is None
    assert env.tick.dispatcher.tick_calls == 0


def test_refresh_actions_adjacent_day_across_month_boundary_does_not_rerun(env):
    """相邻两天（10-31 → 11-01）不能因为 YYYYMMDD 整数差 70 就触发。"""
    env.conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES ('refresh_actions.last','20261031')")
    env.conn.commit()
    env.clock._now = datetime(2026, 11, 1, 10, 0, tzinfo=SH)
    calls = []
    out = startup_recovery(env.cfg, env.conn, env.clock, reg=None,
                           refresh_actions=lambda: calls.append("actions") or 1)
    assert out["refresh_actions"] is False and calls == []


def test_refresh_actions_exactly_seven_days_triggers(env):
    env.conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES ('refresh_actions.last','20261025')")
    env.conn.commit()
    env.clock._now = datetime(2026, 11, 1, 10, 0, tzinfo=SH)
    calls = []
    out = startup_recovery(env.cfg, env.conn, env.clock, reg=None,
                           refresh_actions=lambda: calls.append("actions") or 1)
    assert out["refresh_actions"] and calls == ["actions"]


def test_startup_recovery_runs_eod_for_each_gap_day(env):
    """缺口不是只 append 到 out['eod_days']，而是逐日真跑 run_eod 并写 marker。"""
    calls = []

    def fake_run_eod(cfg, reg, conn, clock):
        calls.append(clock.today())
        return EodReport()

    out = startup_recovery(env.cfg, env.conn, env.clock, reg=None, run_eod=fake_run_eod)
    assert out["eod_days"] and len(calls) == len(out["eod_days"]) == 5
    assert calls == out["eod_days"]
    for day in calls:
        assert env.conn.execute(
            "SELECT value FROM job_state WHERE name=?", (f"eod.{day}",)).fetchone()[0] == "done"


def test_startup_recovery_stops_on_eod_failure(env):
    calls = []

    def boom(cfg, reg, conn, clock):
        calls.append(clock.today())
        raise RuntimeError("eod boom")

    out = startup_recovery(env.cfg, env.conn, env.clock, reg=None, run_eod=boom)
    assert len(calls) == 1
    assert out["eod_failed"] and "boom" in out["eod_failed"][0]["error"]
    assert not out["eod_ran"]
    assert env.conn.execute(
        "SELECT count(*) FROM job_state WHERE name LIKE 'eod.%'").fetchone()[0] == 0


# ---- 竞价 / 收盘统计 / 成分股刷新三个调度项 -------------------------------

def test_auction_close_digest_and_members_scheduled(env):
    jobs = _jobs(env.sched)
    assert {"auction", "close_digest", "members"} <= set(jobs)

    def fields(job):
        return {f.name: str(f) for f in job.trigger.fields}

    a = fields(jobs["auction"])
    assert (a["hour"], a["minute"], a["second"]) == ("9", "25", "30")
    c = fields(jobs["close_digest"])
    assert (c["hour"], c["minute"]) == ("15", "10")
    m = fields(jobs["members"])
    assert (m["hour"], m["minute"]) == ("8", "50")


def test_auction_job_reads_cache_and_passes_brief_rows(env):
    from notify.templates import BriefRow

    env.conn.execute(
        "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days, box_high_adj,"
        " box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean, pos_in_box)"
        " VALUES (20261029,'cn_main','sh600519',1,82.9,'{}',40,10.14,9.86,10.14,9.86,10.04,6e6,0.5)")
    body = json.dumps({"as_of": T0.isoformat(), "source": "tencent", "rows": [
        {"code": "sh600519", "name": "贵州茅台", "price": 1720.0, "prev_close": 1690.0,
         "volume": 5000.0, "amount": 8.6e6, "ts": T0.isoformat()}]}, ensure_ascii=False)
    env.conn.execute("INSERT INTO snapshot_cache(scope, as_of, payload) VALUES ('market',?,?)",
                     (T0.isoformat(), body))
    env.conn.commit()

    _jobs(env.sched)["auction"].func()

    assert env.conn.execute(
        "SELECT auction_vol FROM auction_daily WHERE code='sh600519' AND trade_date=20261030"
    ).fetchone()[0] == 5000.0
    rows = env.tick.dispatcher.briefs[-1][0][0]
    assert rows and all(isinstance(r, BriefRow) for r in rows)
    assert rows[0].code6 == "600519" and rows[0].score == 82.9


def test_close_digest_and_members_jobs_run(env):
    _jobs(env.sched)["close_digest"].func()
    assert env.disp.close_digests == 1
    _jobs(env.sched)["members"].func()
    assert env.conn.execute(
        "SELECT count(*) FROM sector_member WHERE sector='new_blhy'").fetchone()[0] == 2
    assert env.conn.execute(
        "SELECT value FROM job_state WHERE name='sector_members.20261030'").fetchone()[0] == "done"


def test_snapshot_loops_alert_wired_to_dispatcher(cfg_stub=None):
    """`on_alert` 必须接到 dispatcher.alert，否则「数据源连续失败 60s 发 P0」永不触发。"""
    import ast
    from pathlib import Path as _P

    src = _P("jobs/main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "SnapshotLoops"]
    assert calls, "main() 里必须构造 SnapshotLoops"
    kwargs = {k.arg for k in calls[0].keywords}
    assert "on_alert" in kwargs, "SnapshotLoops 构造缺 on_alert，数据源故障不会告警"


def test_eod_and_members_jobs_hold_write_lock():
    """eod/members 与 tick 共用同一连接，写库前必须进同一把锁。"""
    import ast
    from pathlib import Path as _P

    tree = ast.parse(_P("jobs/main.py").read_text(encoding="utf-8"))
    for name in ("eod_job", "members_job"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        holders = [n for n in ast.walk(fn) if isinstance(n, ast.With)
                   and any(isinstance(i.context_expr, ast.Call)
                           and getattr(i.context_expr.func, "id", "") == "_write_lock"
                           for i in n.items)]
        assert holders, f"{name} 没有在 _write_lock 里写库"


def test_eod_snapshot_records_eod_scope():
    """eod 的请求必须记成 eod scope；数据源默认是 pool，不显式传就永远记不到。"""
    from core.datasource.registry import SourceRegistry

    seen = {}

    class Src:
        def fetch_snapshot(self, codes, scope="pool"):
            seen["scope"] = scope
            return {}, "tencent"

    reg = SourceRegistry(Src(), Src())
    reg.snapshot(["sh600000"], scope="eod")
    assert seen["scope"] == "eod"


def test_loop_marker_reflects_snapshot_not_tick():
    """心跳里的 loop 标记必须来自快照循环：tick 每秒空转，用它当标记会掩盖循环卡死。"""
    import ast
    from pathlib import Path as _P

    src = _P("jobs/main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "tick_job")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "loops" in names, "tick_job 必须读 loops.last_tick_at 写 loop 标记"
    keys = {t.slice.value for a in ast.walk(fn) if isinstance(a, ast.Assign)
            for t in a.targets
            if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)}
    assert "loop_last_tick_at" in keys and "tick_last_at" in keys


# ---- 启动补跑不能空转 ----------------------------------------------------


def test_startup_recovery_skips_when_no_eod_marker_and_calendar_huge(env):
    """没有 `eod.*` 标记时，只补**最近几天**，不是把全部历史交易日都排进去。

    实际部署踩到：回填后 `job_state` 是空的，`_last_eod_date` 返回 None，
    于是 `closed = 日历里所有交易日`，`eod_days = 最后 5 天`——每次重启
    都会对 5 天各发 10 个快照请求，而且 `skipped` 的那天不写标记，
    下一次重启再跑一遍。50 次 eod 请求全是这么来的。
    """
    from jobs import main as jobs_main

    calls = []

    def _fake_eod(cfg, reg, conn, clock):
        from jobs.eod import EodReport

        calls.append(clock.today())
        return EodReport(skipped=True)

    out = jobs_main.startup_recovery(
        env.cfg, env.conn, env.clock, reg=None, run_eod=_fake_eod,
        refresh_actions=lambda: 1, refresh_suspensions=lambda: 1)
    # 只有一次「试跑」，跑完就按 skipped 停下，不继续把 5 天全试一遍
    assert len(calls) == 1, f"skipped 之后还在补跑：{calls}"
    assert out.get("eod_skipped")


def test_self_check_llm_reads_env_not_missing_file(env, monkeypatch):
    """LLM 自检要读**环境变量/yaml**，不能读一个根本不存在的 `config/llm.env`。

    生产密钥在 `~/.config/stock-screener/llm.env`，通过 compose `env_file` 注入成
    环境变量；容器里 `config/` 只有五个 yaml。旧实现读 `Path("config/llm.env")`
    永远读不到 url → `llm` 项恒为 False → **每天早上 08:55 误报 P1
    「自检失败：llm」**，长期会把真故障淹掉。
    """
    from jobs.main import self_check

    calls = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"healthy": True}

    def fake_post(url, **kw):
        calls.append(url)
        return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp())
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")

    out = self_check(env.cfg, env.conn, env.clock, reg=None)
    assert calls, "自检没有发出 LLM 探测请求（url 没读到）"
    # yaml 里有 base_url 时以 yaml 为准（环境变量是它的来源，两者应一致）
    assert calls[0].endswith("/chat/completions"), calls[0]
    assert out["llm"] is True


def test_logging_does_not_spew_httpx_urls():
    """httpx 的 INFO 会把每个请求的完整 URL 写进日志（含几百个股票代码）。

    实测 20 分钟的 `docker logs` 几乎全是这种行，排障时完全没法看。
    这里锁住「httpx 不低于 WARNING」这条约束。
    """
    import logging
    from jobs.main import _setup_logging

    _setup_logging()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
    assert logging.getLogger("apscheduler").level >= logging.WARNING


def test_llm_probe_sends_key_and_treats_401_as_down(monkeypatch):
    """自检必须**带密钥**发探测，并把 401/403 判为不健康。

    原实现不带 Authorization，且 `status_code < 500` 就算健康——密钥错/缺失时
    服务端返 401，自检照样报「llm 正常」，把最需要告警的故障判成健康。
    """
    import httpx
    from pathlib import Path

    import jobs.main as J
    from core.config import load_config

    cfg = load_config(Path("config"))
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv(cfg.llm.api_key_env, "sk-test")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["headers"] = kw.get("headers") or {}
        return httpx.Response(401, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post, raising=False)
    assert J._llm_probe(cfg) is False, "401 被判成了健康"
    assert seen["headers"].get("Authorization") == "Bearer sk-test", "探测请求没带密钥"

    # 200 才算健康
    monkeypatch.setattr(httpx, "post",
                        lambda url, **kw: httpx.Response(200, request=httpx.Request("POST", url)),
                        raising=False)
    assert J._llm_probe(cfg) is True


def test_llm_probe_prefers_env_url_over_yaml(monkeypatch):
    """探测的 URL 必须**环境变量优先**，与 `llm/client.py` 一致。

    演练用 `LLM_BASE_URL=http://blackhole:1` 把链路指走；探测若反着取（yaml 优先），
    演练期间自检仍然打真实地址并报绿，等于演练时自检说谎。
    """
    import httpx
    from pathlib import Path

    import jobs.main as J
    from core.config import load_config

    cfg = load_config(Path("config"))
    monkeypatch.setenv("LLM_BASE_URL", "http://blackhole:1/v1")

    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post, raising=False)
    assert J._llm_probe(cfg) is True
    assert seen["url"].startswith("http://blackhole:1"), f"没听环境变量：{seen['url']}"


def test_startup_recovers_same_day_eod_after_close(env):
    """**当天 EOD 错过也要补**：重启发生在 15:05 之后、当天 marker 没写时。

    启动补跑的候选原本只有 `d < today`（已收盘的历史交易日）。于是当天 EOD 一旦
    错过就**永远不补**：cron 的 `misfire_grace_time` 只有 5 分钟，进程只要恰好在
    15:05–15:10 之间不在（部署、重启、崩溃），当天就没有 bar，次日 EOD 的
    `prev_day` 会直接跨过这一天。

    这与我刚修的 westock 续期是同一类问题：**「每天只跑一次」的任务，
    cron 错过之后必须有人兜底**。
    """
    from tests.helpers import _trading_days_rev

    # 造出交易日历：**必须含当天**（20261030 是周五，交易日）——
    # 日历来自 sh000300 的 bar，不含当天就不会把它当交易日
    for day in [*_trading_days_rev(20261029, 10), 20261030]:
        env.conn.execute("INSERT OR REPLACE INTO daily_bars VALUES ('sh000300', ?, 3000,3000,3000,3000,0,0,'x')", (day,))
    env.conn.commit()
    # 历史日全部标记已跑，只留当天缺口
    for day in _trading_days_rev(20261029, 10):
        env.conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES (?, 'done')", (f"eod.{day}",))
    env.conn.commit()

    # 时钟：当天 15:30（收盘后，EOD 本该已跑完）
    env.clock._now = datetime(2026, 10, 30, 15, 30, tzinfo=SH)
    calls = []

    def fake_run_eod(cfg, reg, conn, clock):
        calls.append(clock.today())
        return EodReport()

    out = startup_recovery(env.cfg, env.conn, env.clock, reg=None, run_eod=fake_run_eod)
    assert 20261030 in calls, (
        f"当天（20261030）收盘后重启，EOD 没有补跑；实际跑了 {calls}。"
        "15:05 那次 cron 一旦错过，当天 bar 就永远缺失")


def test_startup_does_not_run_same_day_eod_before_close(env):
    """**收盘前不能补当天 EOD**：那会把未定盘的 bar 写进去。

    这条必须让「当天 marker 缺失」，否则 `marker is None` 这一关就挡住了，
    测不到收盘时间闸——第一版正是这样写成了假绿（去掉 `>= close+buffer` 判断
    测试照样全过）。
    """
    from tests.helpers import _trading_days_rev

    # 当天**不写** eod marker（这正是要被闸门挡住的情形）
    for day in _trading_days_rev(20261029, 10):
        env.conn.execute("INSERT OR REPLACE INTO daily_bars VALUES ('sh000300', ?, 3000,3000,3000,3000,0,0,'x')", (day,))
        env.conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES (?, 'done')", (f"eod.{day}",))
    env.conn.execute("INSERT OR REPLACE INTO daily_bars VALUES ('sh000300', 20261030, 3000,3000,3000,3000,0,0,'x')")
    env.conn.commit()

    calls = []

    def fake_run_eod(cfg, reg, conn, clock):
        calls.append(clock.today())
        return EodReport()

    # 盘中（09:30–14:57 内任一点，这里取 10:00）
    env.clock._now = datetime(2026, 10, 30, 10, 0, tzinfo=SH)
    startup_recovery(env.cfg, env.conn, env.clock, reg=None, run_eod=fake_run_eod)
    assert 20261030 not in calls, "盘中补跑了当天 EOD，会写入未定盘的 bar"

    # 收盘后但还没过 5 分钟缓冲区（14:57 + 5 = 15:02），仍不能跑
    calls.clear()
    env.clock._now = datetime(2026, 10, 30, 15, 0, tzinfo=SH)
    startup_recovery(env.cfg, env.conn, env.clock, reg=None, run_eod=fake_run_eod)
    assert 20261030 not in calls, "收盘缓冲区（close+5min）未到就补跑了当天 EOD"

    # 过了缓冲区就可以跑
    calls.clear()
    env.clock._now = datetime(2026, 10, 30, 15, 30, tzinfo=SH)
    startup_recovery(env.cfg, env.conn, env.clock, reg=None, run_eod=fake_run_eod)
    assert 20261030 in calls, "收盘后仍没补跑当天 EOD"


def test_eod_job_holds_write_lock_across_after_close():
    """EOD 的**整条写路径**（含 `after_close`）都必须在写锁内。

    注释写着「写库必须串行」，但 `after_close` 实际跑在 `with _write_lock(...)` **之外**——
    它同样写 `pool` 表。后果：心跳写库会撞上它，
    `busy_timeout`（5 秒）耗尽后抛 `database is locked`，apscheduler 记 ERROR。

    判活阈值 `STALE_S=120`，EOD 事务持锁约 3 分钟——连续多个心跳周期失败会让
    watchdog 误判 jobs 已死（P0 告警）。

    用 AST 而不是缩进字符串比较：第一版就是靠数缩进写的，`after_close` 明明在锁外
    测试却过了（假绿）。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "jobs" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "eod_job")

    def _under_write_lock(node) -> bool:
        for w in ast.walk(node):
            if not isinstance(w, ast.With):
                continue
            for item in w.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Name) \
                        and ctx.func.id == "_write_lock":
                    return True
        return False

    after_close_calls = [n for n in ast.walk(fn)
                         if isinstance(n, ast.Call)
                         and getattr(n.func, "id", getattr(n.func, "attr", None)) == "after_close"]
    assert after_close_calls, "eod_job 里找不到 after_close 调用（结构变了？请更新本测试）"

    # 对每个 after_close 调用，确认它外面套着一层带 _write_lock 的 with
    for call in after_close_calls:
        # 需要检查的是「包含该调用」的 With 语句，而不是 eod_job 内部的任意 With。
        # ast 没有父指针，这里重建：遍历所有 With，看它的 body 里是否含该调用。
        guarded = False
        for w in ast.walk(fn):
            if not isinstance(w, ast.With):
                continue
            is_write_lock = any(
                isinstance(i.context_expr, ast.Call)
                and isinstance(i.context_expr.func, ast.Name)
                and i.context_expr.func.id == "_write_lock"
                for i in w.items)
            if not is_write_lock:
                continue
            if any(node is call for stmt in w.body for node in ast.walk(stmt)):
                guarded = True
                break
        assert guarded, (
            "after_close 跑在写锁之外：它同样要写 pool 表，会与心跳/其他写者撞车。"
            "实测已导致 `database is locked`。")


def test_heartbeat_survives_database_locked(env):
    """心跳撞上真实锁冲突时**安静跳过**，不把异常抛给 apscheduler。

    EOD 的事务会持锁数分钟，心跳的 `busy_timeout` 只有 5 秒。抛异常等于每分钟
    刷一条 ERROR（曾经 正是如此），还让真正的异常被淹没。
    跳过是安全的：30 秒后还有下一次，判活阈值是 120 秒。

    用**另一条真实连接 `BEGIN IMMEDIATE`** 造成锁冲突（而不是 mock execute——
    `sqlite3.Connection.execute` 是只读属性，打不了桩；只测 `_is_locked` 也测不到
    心跳的实际行为，变异掉 try/except 后测试照样绿）。
    """
    import sqlite3

    blocker = sqlite3.connect(env.tmp / "m.db", isolation_level=None, timeout=1)
    try:
        blocker.execute("BEGIN IMMEDIATE")          # 抢下写锁
        env.conn.execute("PRAGMA busy_timeout = 1")  # 让冲突立刻返回，不等 5 秒
        hb = _jobs(env.sched)["heartbeat"]
        hb.func()                                    # 不能抛异常
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        env.conn.execute("PRAGMA busy_timeout = 5000")


def test_same_day_eod_catchup_works_when_calendar_lacks_today(env):
    """**EOD 没跑过的当天，交易日历里就没有今天的 bar**——补跑仍要生效。

    日历来自 `sh000300` 的 bar：今天的 bar 正是 EOD 写进去的。
    所以「今天该不该跑 EOD」不能用 `today in cal.days` 判断——那正好是
    要补跑时最不可能成立的条件（鸡生蛋）。

    第一版实现就写成了 `if today_ymd in cal.days:`，实测在「EOD 从未跑过」
    的真实场景下分支永不进入，修复等于没生效。必须用 `is_trading_day()`
    （它按工作日 + 内置节假日兜底判定未来日期）。
    """
    from tests.helpers import _trading_days_rev

    # 日历到 20261029 为止，**不含**当天 20261030
    for day in _trading_days_rev(20261029, 10):
        env.conn.execute("INSERT OR REPLACE INTO daily_bars VALUES ('sh000300', ?, 3000,3000,3000,3000,0,0,'x')", (day,))
        env.conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES (?, 'done')", (f"eod.{day}",))
    env.conn.commit()

    env.clock._now = datetime(2026, 10, 30, 15, 30, tzinfo=SH)   # 周五，收盘后
    calls = []

    def fake_run_eod(cfg, reg, conn, clock):
        calls.append(clock.today())
        return EodReport()

    startup_recovery(env.cfg, env.conn, env.clock, reg=None, run_eod=fake_run_eod)
    assert 20261030 in calls, (
        "日历里没有今天的 bar（正是 EOD 没跑过的状态）时补跑没有触发；"
        "用 `today in cal.days` 判断会造成鸡生蛋")


def test_health_touch_is_not_blocked_by_write_lock(env, tmp_path):
    """健康标记（`/tmp/jobs_healthy`）必须**不经写锁**刷新。

    compose 的 jobs 健康检查是 `find /tmp/jobs_healthy -mmin -2`，每 60 秒一次、
    连续 3 次失败判 unhealthy。EOD（`run_eod` + `after_close`）持写锁约 4 分钟，
    而心跳原来把 touch 放在 `heartbeat_job` 里、跟着写锁一起被阻塞——
    那 4 分钟内标记不再刷新，容器会被判 unhealthy。

    （watchdog 的 P0 不受影响：它的窗口是 09:15–11:30 / 13:00–15:05，
    EOD 在 15:05 之后跑，基本落在窗口外。真正被影响的是容器健康检查。）
    """
    import threading

    lock = threading.RLock()
    lock.acquire()                       # 模拟「EOD 正持锁」
    try:
        # 独立的标记任务：不碰数据库、不碰这把锁，被调用时立即刷新文件
        touch = _jobs(env.sched).get("health_touch")
        assert touch is not None, "没有独立的健康标记任务，标记会被写锁饿死"
        marker = Path("/tmp/jobs_healthy")
        marker.unlink(missing_ok=True)
        touch.func()
        assert marker.exists(), "写锁被占用时健康标记没有刷新"
    finally:
        lock.release()
        Path("/tmp/jobs_healthy").unlink(missing_ok=True)


def test_cleanup_job_holds_write_lock(env):
    """`cleanup_job` 必须与其它写库任务共用 `loops.lock`。

    它现在直接 `cleanup(conn, ...)`，没有任何锁。而 cleanup **写** 6 张表
    （push_log / event_log / pool / chat_* / agent_schedule / upstream_calls），
    还新增了 `PRAGMA optimize`（它会读全库统计）。同进程的 tick（每秒）、心跳（30 秒）、
    `after_close` 选池共用**同一个** `conn`——线程交错时对方的 `with conn` 会把
    cleanup 的中间状态提前提交，删除失去原子性。

    这是本文件里唯一没加锁的写库任务：`members` / `archive` / `self_check` /
    `eod` 都已经在锁里了（各自注释还专门解释了为什么必须加）。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "jobs" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cleanup_job")

    def _wrapped(node) -> bool:
        for w in ast.walk(node):
            if not isinstance(w, ast.With):
                continue
            if any(isinstance(i.context_expr, ast.Call)
                   and isinstance(i.context_expr.func, ast.Name)
                   and i.context_expr.func.id == "_write_lock"
                   for i in w.items):
                return True
        return False

    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "cleanup"]
    assert calls, "cleanup_job 里没有 cleanup 调用（结构变了？）"
    assert _wrapped(fn), (
        "cleanup_job 没有取写锁：它写 6 张表且与 tick/心跳共用同一个 conn，"
        "线程交错会让删除失去原子性")


def test_every_db_writing_job_holds_the_shared_write_lock():
    """**通用守卫**：每一处写库**调用点**都必须在写锁内。

    粒度必须是「调用点」，不能是「函数里某处有锁」。第一版就是按函数判的：
    只要函数体里出现过 `with _write_lock(...)` 就放过——于是
    `members_job` / `archive_job` / `backup_job` / `self_check_job` 里那些
    **锁外的 `dispatcher.alert(...)`**（它经 `_push_log` 写 `push_log`）全部漏检。
    Codex 复核把 `close_digest_job` 的 `with tick.lock:` 删掉，守卫依然全绿，
    这才暴露出来。

    锁的等价写法有三种（`loops.lock` **就是** `tick.lock`，见 `main()` 里
    构造 `SnapshotLoops` 时传的 `lock=tick.lock`）：
    `_write_lock(...)`、`tick.lock`、`loops.lock`。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "jobs" / "main.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "make_scheduler")

    # 会**写源数据库**的调用（新增写库调用时请加进来）
    WRITERS = {
        "cleanup", "run_eod", "after_close", "refresh_sector_members", "archive_run",
        "renew_if_needed", "import_watchlist",
        # dispatcher 的这四个方法都经 `_push_log` 写 `push_log`（BEGIN IMMEDIATE）
        "close_digest", "alert", "open_brief", "_send",
        # 这两个内部自带 `tick.lock`，而它就是 `loops.lock`
        "tick", "capture_auction",
    }
    SELF_LOCKING = {"tick", "capture_auction"}
    # 名字像写库、实际不写源库，明确排除并说明原因（不要靠注释蒙混）
    NOT_SOURCE_WRITERS = {
        # conn.backup(dest)：读源库、写目标文件
        "backup_db",
        # 只 SELECT，不写
        "self_check",
    }

    def _is_write_lock_expr(expr) -> bool:
        if isinstance(expr, ast.Call) and getattr(expr.func, "id", None) == "_write_lock":
            return True
        if isinstance(expr, ast.Attribute) and expr.attr == "lock":
            return getattr(expr.value, "id", None) in {"tick", "loops"}
        return False

    def _guarded_call_ids(node) -> set[int]:
        """收集所有位于写锁 `with` 体内（含多上下文写法）的节点 id。"""
        inside: set[int] = set()
        for w in ast.walk(node):
            if isinstance(w, ast.With) and any(_is_write_lock_expr(i.context_expr) for i in w.items):
                for stmt in w.body:
                    for n in ast.walk(stmt):
                        inside.add(id(n))
        return inside

    import re

    WRITE_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)

    def _writes_sql(call) -> bool:
        """直接写 SQL 的调用（`conn.execute("INSERT ...")`）。

        只按函数名白名单查是不够的：`eod_job` / `members_job` 里写 `job_state` marker
        用的是裸 `conn.execute(...)`，名字不在任何白名单里，于是漏检（Codex 复核指出）。
        """
        for a in call.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str) and WRITE_SQL.search(a.value):
                return True
            if isinstance(a, ast.JoinedStr) and WRITE_SQL.search(ast.unparse(a)):
                return True
        return False

    offenders = []
    for node in fn.body:
        if not (isinstance(node, ast.FunctionDef) and node.name.endswith("_job")):
            continue
        inside = _guarded_call_ids(node)
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            named = name is not None and name in WRITERS - SELF_LOCKING \
                and name not in NOT_SOURCE_WRITERS
            if not (named or _writes_sql(call)):
                continue
            if id(call) not in inside:
                label = f"{name}()" if named else f"SQL {ast.unparse(call)[:40]}"
                offenders.append(f"{node.name}:{call.lineno} {label}")

    assert not offenders, (
        "以下写库调用不在 loops.lock 内（会与每秒的 tick 在同一个连接上交错，"
        "`with conn` 可能被对方提前提交）：" + "；".join(offenders))


def test_startup_renew_run_date_is_recomputed_after_recovery(env):
    """启动补跑的时刻必须在 `startup_recovery` 之后重算。

    执行顺序是 `make_scheduler()` → `startup_recovery()` → `sched.start()`，
    而 `startup_recovery` 会补跑最多 5 天 EOD（每只票都发请求），耗时可达数十分钟。
    若 `run_date` 在 `make_scheduler` 里就定成 `now+60s`，等到 `sched.start()` 时
    早已过期；超过 `misfire_grace_time`(300s) 后被 APScheduler **静默丢弃**——
    启动补跑一次都不跑，且不报任何错，而它正是兜住「重启撞上续期窗口」的保险。

    注意：`modify_job(next_run_time=...)` 只改 `next_run_time`，**不改**
    `trigger.run_date`（DateTrigger 的 run_date 是构造时固定的）。所以这里断言的是
    重算调用本身，而不是 trigger 内部字段。
    """
    import jobs.main as M

    clock = env.clock
    sched, state = M.make_scheduler(env.cfg, env.conn, clock, data_dir=str(env.tmp))
    assert "arm_startup_renew" in state, "make_scheduler 没有暴露重算入口"

    seen: list[tuple] = []
    real_modify = sched.modify_job

    def spy(job_id, **kw):
        seen.append((job_id, kw.get("next_run_time")))
        return real_modify(job_id, **kw)

    sched.modify_job = spy          # 只替换实例属性，测试结束即失效
    try:
        # 模拟 startup_recovery 耗时 10 分钟（远超 misfire_grace_time 的 5 分钟）
        clock._now = clock.now() + timedelta(minutes=10)
        state["arm_startup_renew"]()
    finally:
        del sched.modify_job

    assert seen, "arm_startup_renew 没有重算启动补跑的时刻"
    job_id, run_at = seen[0]
    assert job_id == "westock_renew_startup"
    assert run_at is not None, "重算时没有给 next_run_time"
    delta = (run_at - clock.now()).total_seconds()
    assert 0 < delta <= M.STARTUP_RENEW_DELAY_S + 1, (
        f"重算后的时刻距今 {delta:.0f}s，应在 {M.STARTUP_RENEW_DELAY_S}s 左右")


def test_startup_recovery_takes_write_lock_after_starting_loops(env):
    """`startup_recovery` 起循环之后必须拿写锁再补跑。

    我原先在 `main()` 里写过一句注释：「它在 `sched.start()` 之前执行，此时
    tick/心跳都没起来，本进程内没有并发写者」——**这句是错的**。
    `startup_recovery` 内部第 3 步就是 `loops.start()`（jobs/main.py:432-434），
    它会立刻拉起 6 个快照线程（jobs/snapshot.py:257-262），那些线程经
    `_write_cache` 写共享连接。而随后的 EOD 补跑、`job_state` marker、
    `refresh_actions` 都在**没有锁**的情况下写同一个连接。

    Codex 用 LockSpy 观测到：loops_started=True、lock_enters=0、补跑期间快照线程已启动。
    """
    import threading

    lock = threading.Lock()

    class LockSpy:
        def __init__(self, real):
            self._real = real
            self.enters = 0

        def __enter__(self):
            self.enters += 1
            return self._real.__enter__()

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

        def acquire(self, *a, **k):
            return self._real.acquire(*a, **k)

        def release(self):
            return self._real.release()

    spy = LockSpy(lock)
    loops = SimpleNamespace(lock=spy, start=lambda: None)

    calls = []

    def fake_run_eod(cfg, reg, conn, clock):
        calls.append(clock.today())
        return EodReport()

    startup_recovery(env.cfg, env.conn, env.clock, reg=None, loops=loops, run_eod=fake_run_eod)
    assert calls, "没有补跑，测试没测到目标路径"
    assert spy.enters > 0, (
        "startup_recovery 起了快照线程后仍无锁写库：那些线程会与本函数的 EOD 补跑、"
        "job_state marker 在同一个 sqlite 连接上交错")


def test_startup_recovery_writes_are_all_inside_the_write_lock():
    """`startup_recovery` 里**每一处**写库都要在 `loops.lock` 内。

    它第 3 步就 `loops.start()`（拉起快照线程，那些线程经 `_write_cache` 写共享连接），
    之后才做 EOD 补跑 / `job_state` marker / `refresh_actions`（+ 除权刷新）的写入。
    上一版只给 EOD 那段加了锁，`refresh_actions` 与它的 marker 仍在锁外
    ——所以这里按「调用点」逐个查，而不是「函数里出现过锁就算」。
    """
    import ast
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "jobs" / "main.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "startup_recovery")

    WRITE_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
    WRITER_NAMES = {"refresh_actions", "refresh_suspensions", "run_eod", "recover_fn"}

    def _is_wl(expr) -> bool:
        if isinstance(expr, ast.Call) and getattr(expr.func, "id", None) == "_write_lock":
            return True
        # startup_recovery 里用的是局部变量 `write_lock`
        if isinstance(expr, ast.Name) and expr.id == "write_lock":
            return True
        if isinstance(expr, ast.Attribute) and expr.attr == "lock":
            return getattr(expr.value, "id", None) in {"tick", "loops"}
        return False

    inside: set[int] = set()
    for w in ast.walk(fn):
        if isinstance(w, ast.With) and any(_is_wl(i.context_expr) for i in w.items):
            for stmt in w.body:
                for n in ast.walk(stmt):
                    inside.add(id(n))

    bad = []
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        writes = name in WRITER_NAMES
        if not writes:
            for a in call.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                        and WRITE_SQL.search(a.value):
                    writes = True
                    break
        if writes and id(call) not in inside:
            bad.append(f"{fn.name}:{call.lineno} {name or ast.unparse(call)[:30]}")

    assert not bad, (
        "startup_recovery 里这些写库调用不在写锁内（快照线程此时已在写同一个连接）："
        + "；".join(bad))


def test_refresh_hooks_warn_when_client_lacks_capability(env, caplog):
    """客户端缺 `batch_dividend`/`suspension` 时**必须喊出来**，不能静默不跑。

    这两个周刷新从来没真正跑过：`startup_recovery` 的分支在，但 `main()` 没传回调；
    而它需要的 `batch_dividend`/`suspension` 在 `WestockMcp` 上根本不存在
    （测试用的是 `FakeWestockCli`，那个 CLI 客户端没实现）。
    生产库证据：`corporate_actions` 27 行全是 `snapshot_implied`、
    `suspension_calendar` 0 行、`refresh_actions.last` 从未写入。
    """
    import logging

    from jobs.main import make_refresh_hooks

    class NoCapability:
        pass

    with caplog.at_level(logging.WARNING, logger="jobs.main"):
        acts, susp = make_refresh_hooks(NoCapability(), env.cfg, env.conn)
    assert (acts, susp) == (None, None), "能力缺失时应返回 None"
    msgs = [r.message for r in caplog.records if r.name == "jobs.main"]
    assert any("未接线" in m or "batch_dividend" in m for m in msgs), \
        f"客户端不支持时没有告警，缺口会一直静默：{msgs}"


def test_refresh_hooks_wire_when_capability_present(env):
    """能力齐备时要真的接上（不能因为缺能力就永远返回 None）。"""
    from jobs.main import make_refresh_hooks

    class Fake:
        def batch_dividend(self, codes, years=10, max_requests=None, **kw):
            return {}

        def suspension(self, start, end):
            return []

    acts, susp = make_refresh_hooks(Fake(), env.cfg, env.conn)
    assert callable(acts) and callable(susp), "有能力却没接线"
    # codes 取自 stock_meta，不能崩
    assert acts() == 0 and susp() == 0


def test_members_job_does_not_fetch_inside_the_lock(env):
    """`members_job` 的网络请求必须在写锁**外**。

    抓几十个板块要好几分钟；把请求放进 `loops.lock` 里，每秒的 tick 与 30 秒的
    心跳会全程堵着。曾经
    `heartbeat_job ... skipped: maximum number of running instances reached (1)`
    ——心跳等锁超过 30 秒，下一轮触发时上一轮还没结束。

    这是同一类问题第三次出现（`backup_db` 已按「只包告警不包备份」处理），
    所以用 AST 断言：取锁之前必须已经拿到 `sectors`（数据）。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "jobs" / "main.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "members_job")

    # 找出 `with _write_lock(loops):` 所在行号
    lock_line = None
    for w in ast.walk(fn):
        if isinstance(w, ast.With) and any(
                isinstance(i.context_expr, ast.Call)
                and getattr(i.context_expr.func, "id", None) == "_write_lock"
                for i in w.items):
            lock_line = w.lineno
            break
    assert lock_line is not None, "members_job 没有取写锁"

    # 任何网络抓取调用都必须出现在取锁**之前**
    FETCH = {"fetch_sector_members", "fetch_sector_list", "refresh_sector_members"}
    bad = [f"{c.func.id}@{c.lineno}" for c in ast.walk(fn)
           if isinstance(c, ast.Call) and getattr(c.func, "id", None) in FETCH
           and c.lineno > lock_line]
    assert not bad, (
        f"网络抓取仍在写锁内（取锁在第 {lock_line} 行）：{bad}。"
        "抓板块要几分钟，会把 tick 和心跳全堵住")


def test_docstring_does_not_claim_a_nonexistent_schedule():
    """模块注释不能声称存在一个不存在的定时任务。

    `jobs/main.py` 曾写「换成 BackgroundScheduler 后 …… 周日 20:00 除权刷新 ……
    全都永远跑不到」，但调度器里**没有**周日 20:00 的任务：除权刷新只有
    `startup_recovery` 里的启动补跑，且能力缺失时回调为 `None` 直接跳过。

    注释是运维判断的依据——照着它去查「为什么周日没跑」，会白查一场。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "jobs" / "main.py").read_text(encoding="utf-8")
    doc = src.split('"""')[1]

    # 注释里提到的每一次「HH:MM 做某事」，都必须能在 add_job 的 CronTrigger 里找到
    claimed = set(re.findall(r"(\d{2}):(\d{2})", doc))
    triggers = set(re.findall(r"CronTrigger\(hour=(\d+), minute=(\d+)", src))
    triggers = {(f"{int(h):02d}", f"{int(m):02d}") for h, m in triggers}
    bogus = claimed - triggers
    assert not bogus, (
        f"模块注释声称的时刻 {sorted(bogus)} 在调度器里不存在"
        f"（实际 CronTrigger：{sorted(triggers)}）")


def test_architecture_doc_schedule_matches_code():
    """`docs/architecture.md` 的时刻表必须与代码里的 CronTrigger 一致。

    文档说「周日 20:00 除权刷新」之类的话一旦失真，运维会照着去查一个不存在的东西。
    这里只比对**固定时刻**（tick 是 interval 任务，不在表里）。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    src = (root / "jobs" / "main.py").read_text(encoding="utf-8")
    triggers = {(f"{int(h):02d}:{int(m):02d}")
                for h, m in re.findall(r"CronTrigger\(hour=(\d+), minute=(\d+)", src)}

    doc = (root / "docs" / "architecture.md").read_text(encoding="utf-8")
    # 只抽表格首列的固定时刻（`| HH:MM |`），区间写法（HH:MM–HH:MM）不算
    claimed = set(re.findall(r"^\|\s*(\d{2}:\d{2})\s*\|", doc, re.M))
    bogus = claimed - triggers
    assert not bogus, (
        f"docs/architecture.md 写了 {sorted(bogus)}，但代码里没有对应的 CronTrigger"
        f"（实际：{sorted(triggers)}）")


def test_bind_mounted_session_file_being_a_directory_is_reported(tmp_path, caplog, monkeypatch):
    """单文件 bind mount 被 docker 建成目录时必须 ERROR，不能悄悄 EISDIR。

    宿主机上 `~/.config/stock-screener/wb_client.json` 不存在时，docker 会建一个
    同名**空目录**顶替；容器里读写它直接 EISDIR，报错出现在容器日志深处。
    这个坑只在别人机器上出现（作者本机文件早就在），所以启动时必须喊出来。
    """
    import logging

    d = tmp_path / "wb_client.json"
    d.mkdir()                                    # 模拟 docker 建出来的目录
    monkeypatch.setattr("jobs.main.Path",
                        lambda p: d if p.endswith("wb_client.json") else tmp_path / "missing.json")
    with caplog.at_level(logging.ERROR):
        jobs_main._warn_if_placeholder_config(SimpleNamespace(
            notify=SimpleNamespace(public_host="stock.example.com")))
    assert any("是**目录**而不是文件" in r.message for r in caplog.records), \
        f"没有报出目录陷阱：{[r.message for r in caplog.records]}"


def test_refresh_does_not_stamp_when_the_fetch_failed(tmp_path):
    """取数**失败**（回调返回 `None`）时不许盖 `refresh_actions.last`。

    盖了会按「距上次 ≥7 天」把重试推到下周，而这类失败通常不抛异常：
    0.2.33 上线时正是如此——时间戳落下、`corporate_actions` 的官方行一直是 0，
    而整条链路不报错。不盖的话每次启动都会重试。

    **注意 `0` 和 `None` 是两回事**：停牌返回 0 行是正常结果（今天没人停牌），
    不能因为 0 就永远不盖戳、每次启动都全量重拉。
    """
    from core.db import connect, migrate

    conn = connect(tmp_path / "t.db")
    migrate(conn)

    called = {"actions": 0, "susp": 0}

    def actions():
        called["actions"] += 1
        return None

    def susp():
        called["susp"] += 1
        return None

    out = jobs_main.startup_recovery(
        cfg=SimpleNamespace(), conn=conn, clock=FixedClock(datetime(2026, 11, 15, 20, 0)),
        reg=None, tick=None, dispatcher=None, loops=None,
        refresh_actions=actions, refresh_suspensions=susp)

    assert called["actions"] == 1, "没有调用刷新"
    assert out["refresh_actions"] is False, "取数失败却把 refresh_actions 标成已完成"
    row = conn.execute("SELECT value FROM job_state WHERE name='refresh_actions.last'").fetchone()
    assert row is None, f"取数失败却盖了时间戳：{row}"


def test_refresh_stamps_after_writing_rows(tmp_path):
    """写进去一行就必须盖时间戳，否则每次启动都全量重拉。"""
    from core.db import connect, migrate

    conn = connect(tmp_path / "t.db")
    migrate(conn)

    out = jobs_main.startup_recovery(
        cfg=SimpleNamespace(), conn=conn, clock=FixedClock(datetime(2026, 11, 15, 20, 0)),
        reg=None, tick=None, dispatcher=None, loops=None,
        refresh_actions=lambda: 5, refresh_suspensions=lambda: 2)

    assert out["refresh_actions"] is True
    row = conn.execute("SELECT value FROM job_state WHERE name='refresh_actions.last'").fetchone()
    assert row and row[0] == "20261115"


def test_refresh_stamps_when_it_wrote_zero_rows_but_succeeded(tmp_path):
    """查到了但确实是 0 行（例如今天没有票停牌）**必须盖戳**。

    否则每次启动都会全量重拉一遍——对停牌这种"经常是 0 条"的数据，
    不盖戳等于把「7 天一次」变成「每次启动一次」。
    """
    from core.db import connect, migrate

    conn = connect(tmp_path / "t.db")
    migrate(conn)

    out = jobs_main.startup_recovery(
        cfg=SimpleNamespace(), conn=conn, clock=FixedClock(datetime(2026, 11, 15, 20, 0)),
        reg=None, tick=None, dispatcher=None, loops=None,
        refresh_actions=lambda: 0, refresh_suspensions=lambda: 0)

    assert out["refresh_actions"] is True, "0 行但没失败，应当算完成"
    row = conn.execute("SELECT value FROM job_state WHERE name='refresh_actions.last'").fetchone()
    assert row and row[0] == "20261115"


def test_actions_refresh_job_fetches_outside_the_write_lock(env):
    """滚动刷新的**网络请求必须在写锁外**，只有写库才持锁。

    一次取数要发 6 个请求还带退避（撞限频时 30 秒起）。放进 `loops.lock` 里，
    每秒的 tick 和 30 秒的心跳会全程堵着——`test_members_job_does_not_fetch_inside_the_lock`
    记录的 `skipped: maximum number of running instances` 就是这么来的。
    """
    from jobs.main import make_scheduler

    class SpyLock:
        def __init__(self):
            self.depth = 0
            self.max_depth = 0

        def __enter__(self):
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
            return self

        def __exit__(self, *a):
            self.depth -= 1
            return False

    observed: list[int] = []

    class SpyMcp:
        """记录"取数时"的锁深度——**不能用 assert**：job 体里 `except Exception`
        会把断言吞掉，测试就永远绿（第一版就是这么写错的）。"""
        lock = None

        def batch_dividend(self, codes, years=10, max_requests=None, **kw):
            observed.append(SpyMcp.lock.depth if SpyMcp.lock is not None else -1)
            return {}

        def suspension(self, start, end):
            return []

    class Loops:
        def __init__(self):
            self.lock = SpyLock()

    loops = Loops()
    SpyMcp.lock = loops.lock
    sched, _ = make_scheduler(env.cfg, env.conn, env.clock, loops=loops, mcp=SpyMcp())
    job = sched.get_job("actions_refresh")
    assert job is not None, "滚动刷新任务没注册"

    # 直接调 job 的函数体，避免依赖 APScheduler 的触发时机
    job.func()

    assert observed, "取数根本没发生"
    assert observed[0] == 0, f"取数发生在写锁内（深度 {observed[0]}）：会堵住 tick"
    assert loops.lock.max_depth >= 1, "写库时没有持锁：会和 tick 抢同一个连接"


def test_actions_refresh_avoids_market_hours(env):
    """滚动刷新**不能落在盘中**（09:15–15:10）。

    那段是 tick / 快照的主链路，而除权数据不是实时数据、晚几小时无所谓。
    抢同一个上游限频额度会让行情取数变慢。
    """
    from datetime import datetime

    from zoneinfo import ZoneInfo

    from jobs.main import make_scheduler

    sched, _ = make_scheduler(env.cfg, env.conn, env.clock, mcp=None)
    tz = ZoneInfo("Asia/Shanghai")
    for job_id in ("actions_refresh", "actions_refresh_weekend",
                  "actions_refresh_premarket", "actions_refresh_postclose",
                  "actions_refresh_weekend_am", "actions_refresh_weekend_pm"):
        job = sched.get_job(job_id)
        assert job is not None, f"{job_id} 没注册"
        # 取两天内的触发时刻，检查没有一个落在 09:15–15:10。
        # **必须 `prev = nxt + 1 秒`**：`get_next_fire_time` 对 `prev` 本身
        # 是"含"的语义，直接 `prev = nxt` 会永远返回同一个时刻——
        # 第一版就这么写错了，循环 200 次其实只检查了第一个时刻，恒定绿。
        prev = datetime(2026, 11, 16, 0, 0, tzinfo=tz)
        seen = []
        for _ in range(200):
            nxt = job.trigger.get_next_fire_time(None, prev)
            if nxt is None:
                break
            local = nxt.astimezone(tz)
            seen.append(local)
            minutes = local.hour * 60 + local.minute
            assert not (9 * 60 + 15 <= minutes <= 15 * 60 + 10), \
                f"{job_id} 在盘中触发：{local}"
            prev = nxt + timedelta(seconds=1)
        assert len(seen) >= 30, f"{job_id} 只取到 {len(seen)} 个触发时刻，覆盖不足"
