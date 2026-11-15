"""westock token 按需续期（先红后绿）。

Step 1 清单里属于本文件的条目：
① `renew_if_needed` 同一天第二次调用返回 `skip`、子进程只起了一次，
   且 `--out` 指向 `/state/westock.env`；
② 「每天至多一次」落在 `app_secret('westock_renew.<YYYYMMDD>')`，**成功失败都要写**；
③ 失败后允许 ≥5 分钟重试 1 次并写 `westock_renew_retry.<YYYYMMDD>`，再失败发 P1；
④ `westock_expires_at` 存 epoch 秒字符串（两处读者同格式比较）。

`scheduler` 装配用例放最后：08:40 续期 / 09:00 导入必须真的注册进 `jobs/main.py`，
否则「自动续期」只存在于纸面。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.clock import FixedClock
from jobs.main import make_scheduler
from jobs.westock_renew import renew_if_needed
from tests.helpers import SH

T0 = datetime(2026, 10, 30, 8, 40, tzinfo=SH)
DAY = 20261030
DAY_KEY = f"westock_renew.{DAY}"
RETRY_KEY = f"westock_renew_retry.{DAY}"
ENV_PATH = Path("/state/westock.env")


class Clock:
    """可推进的时钟（`FixedClock` 每次换一个新的即可，但用例里要原地推进）。"""

    def __init__(self, now):
        self.now_ = now

    def now(self):
        return self.now_

    def today(self):
        return int(self.now_.strftime("%Y%m%d"))

    def advance(self, seconds):
        self.now_ = self.now_ + timedelta(seconds=seconds)
        return self


class Runner:
    """`subprocess.run` 的替身：记命令，可选地「写出」env 文件。"""

    def __init__(self, ok=True, expires_at=None):
        self.ok = ok
        self.expires_at = expires_at
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        self.kwargs.append(dict(kw))
        if self.ok:
            target = Path(cmd[cmd.index("--out") + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            body = ["WESTOCK_MCP_URL=https://mcp.example/mcp", "WESTOCK_ACCESS_TOKEN=tok-new"]
            if self.expires_at is not None:
                body.append(f"WESTOCK_EXPIRES_AT={self.expires_at}")
            target.write_text("\n".join(body) + "\n", encoding="utf-8")
        return SimpleNamespace(returncode=0 if self.ok else 1, stdout="", stderr="boom")


class Disp:
    def __init__(self):
        self.alerts: list[tuple[str, str, str]] = []

    def alert(self, kind, text, level="P1"):
        self.alerts.append((kind, text, level))


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def runner():
    return Runner(ok=True, expires_at=str(int(T0.timestamp()) + 86400))


def _secret(conn, name):
    row = conn.execute("SELECT value FROM app_secret WHERE name = ?", (name,)).fetchone()
    return None if row is None else row[0]


def _put(conn, name, value):
    with conn:
        conn.execute("INSERT OR REPLACE INTO app_secret(name, value) VALUES (?, ?)", (name, value))


def test_expires_far_in_future_skips_without_subprocess(conn, clock, runner):
    _put(conn, "westock_expires_at", str(int(clock.now().timestamp()) + 48 * 3600))
    assert renew_if_needed(conn, clock, runner) == "skip"
    assert runner.calls == []


def test_renew_runs_script_and_stores_epoch_seconds(conn, clock, runner, tmp_path):
    result = renew_if_needed(conn, clock, runner, env_path=tmp_path / "westock.env")
    assert result == "renewed"
    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[0] == "node"
    assert any(c.endswith("wb_auth_headless.mjs") for c in cmd), cmd
    assert cmd[cmd.index("--out") + 1] == str(tmp_path / "westock.env")
    # 当天标记与过期时刻都要落库，且过期时刻是 epoch 秒字符串
    assert _secret(conn, DAY_KEY) is not None
    assert _secret(conn, "westock_expires_at") == str(runner.expires_at)
    assert _secret(conn, "westock_expires_at").isdigit()


def test_default_out_path_is_state_westock_env(conn, clock):
    """D13：容器内 `/state/westock.env` 是可写挂载点，写别处等于续期白跑。"""
    bad = Runner(ok=False)                    # 失败分支：不会真的去写 /state
    renew_if_needed(conn, clock, bad)
    cmd = bad.calls[0]
    assert cmd[cmd.index("--out") + 1] == str(ENV_PATH)


def test_second_call_same_day_skips_and_runs_once(conn, clock, runner):
    assert renew_if_needed(conn, clock, runner) == "renewed"
    assert renew_if_needed(conn, clock, runner) == "skip"
    assert len(runner.calls) == 1


def test_existing_day_key_skips_even_without_expiry(conn, clock, runner):
    _put(conn, DAY_KEY, "ok:0")
    assert renew_if_needed(conn, clock, runner) == "skip"
    assert runner.calls == []


def test_failure_writes_day_key_then_single_retry_then_alert(conn, clock):
    disp = Disp()
    bad = Runner(ok=False)
    assert renew_if_needed(conn, clock, bad, dispatcher=disp) == "failed"
    assert len(bad.calls) == 1
    assert _secret(conn, DAY_KEY) is not None          # 失败也要写：否则同一天会重复问腾讯
    assert disp.alerts == []                            # 第一次失败先不告警，等重试

    assert renew_if_needed(conn, clock, bad, dispatcher=disp) == "skip"
    clock.advance(240)                                  # 4 分钟后：还没到退避窗口
    assert renew_if_needed(conn, clock, bad, dispatcher=disp) == "skip"
    assert len(bad.calls) == 1

    clock.advance(120)                                  # 累计 6 分钟：允许唯一一次重试
    assert renew_if_needed(conn, clock, bad, dispatcher=disp) == "failed"
    assert len(bad.calls) == 2
    assert _secret(conn, RETRY_KEY) is not None
    assert len(disp.alerts) == 1 and "扫码" in disp.alerts[0][1]
    assert disp.alerts[0][2] == "P1"

    clock.advance(3600)                                 # 重试额度用完 → 当天不再问腾讯
    assert renew_if_needed(conn, clock, bad, dispatcher=disp) == "skip"
    assert len(bad.calls) == 2


def test_retry_success_updates_expires_and_does_not_alert(conn, clock):
    disp = Disp()
    bad = Runner(ok=False)
    assert renew_if_needed(conn, clock, bad, dispatcher=disp) == "failed"
    clock.advance(301)
    good = Runner(ok=True, expires_at=str(int(clock.now().timestamp()) + 86400))
    assert renew_if_needed(conn, clock, good, dispatcher=disp) == "renewed"
    assert _secret(conn, RETRY_KEY) is not None
    assert _secret(conn, "westock_expires_at") == str(good.expires_at)
    assert disp.alerts == []


def test_renew_failure_without_dispatcher_does_not_crash(conn, clock):
    bad = Runner(ok=False)
    assert renew_if_needed(conn, clock, bad) == "failed"
    clock.advance(301)
    assert renew_if_needed(conn, clock, bad) == "failed"      # 没有告警通道也要正常返回


def test_renewed_token_is_read_by_mcp_after_renewal(conn, clock, tmp_path):
    """续期后不用重启：`WestockMcp` 每次调用重读同一个 env 文件。"""
    from llm.tools import MCP_TOOL_SCHEMAS  # noqa: F401  （确保 mcp 包可导入）
    from mcp.westock import WestockMcp

    target = tmp_path / "westock.env"
    renew_if_needed(conn, clock, Runner(ok=True, expires_at=str(int(time.time()) + 3600)),
                    env_path=target)
    env = WestockMcp(env_path=target, http=SimpleNamespace(post=lambda *a, **k: None))
    parsed = env._read_env()
    assert parsed["WESTOCK_ACCESS_TOKEN"] == "tok-new"
    assert parsed["WESTOCK_EXPIRES_AT"].isdigit()


# ---------------------------------------------------------------- 调度装配


class FakeMcp:
    def read_watchlist(self, group=None, market=None):
        return {"ok": True, "data": [{"code": "sh600519"}], "as_of": "2026-10-30T08:40:00+08:00",
                "reason": None, "adjust": None}


def test_scheduler_registers_renew_and_import(conn, CFG, tmp_path):
    clock = FixedClock(T0)
    sched, _state = make_scheduler(CFG, conn, clock, data_dir=str(tmp_path))
    jobs = {j.id: j for j in sched.get_jobs()}
    assert "westock_renew" in jobs and "westock_import" in jobs
    renew_fields = {f.name: str(f) for f in jobs["westock_renew"].trigger.fields}
    import_fields = {f.name: str(f) for f in jobs["westock_import"].trigger.fields}
    assert renew_fields["hour"] == "8" and renew_fields["minute"] == "40"
    assert import_fields["hour"] == "9" and import_fields["minute"] == "0"


def test_import_job_writes_watch_stock(conn, CFG, tmp_path):
    clock = FixedClock(T0)
    sched, _state = make_scheduler(CFG, conn, clock, mcp=FakeMcp(), data_dir=str(tmp_path))
    job = {j.id: j for j in sched.get_jobs()}["westock_import"]
    job.func()
    rows = list(conn.execute("SELECT code, source FROM watch_stock"))
    assert [tuple(r) for r in rows] == [("sh600519", "westock")]


def test_renew_job_uses_injected_runner(conn, CFG, tmp_path, runner):
    clock = FixedClock(T0)
    sched, _state = make_scheduler(CFG, conn, clock, westock_runner=runner,
                                  data_dir=str(tmp_path))
    job = {j.id: j for j in sched.get_jobs()}["westock_renew"]
    job.func()
    assert len(runner.calls) == 1
    assert _secret(conn, DAY_KEY) is not None


def test_cache_paths_passed_from_env(monkeypatch, tmp_path, clock, conn):
    """扫码登录态的缓存路径要**显式**传给脚本。

    `wb_auth_headless.mjs` 默认放 `$HOME/.config/stock-screener/`，容器里的
    `$HOME` 不是宿主机的 → 找不到 `wb_session.json` → 每次续期都试图重新扫码
    （=永远失败，扫码只有人能做）。实测 11-10/11-11 两天定时续期都 fail，
    而宿主机上手动跑同一脚本能静默续期成功。
    """
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setenv("WB_CLIENT_CACHE", "/host-config/wb_client.json")
    monkeypatch.setenv("WB_SESSION_CACHE", "/host-config/wb_session.json")

    assert renew_if_needed(conn, clock, runner=fake_run,
                           env_path=tmp_path / "westock.env") == "renewed"

    cmd = captured["cmd"]
    assert "--client-cache" in cmd and "/host-config/wb_client.json" in cmd, cmd
    assert "--session-cache" in cmd and "/host-config/wb_session.json" in cmd, cmd


def test_no_env_vars_leaves_script_defaults(monkeypatch, tmp_path, clock, conn):
    """不设环境变量时**不要传**这两个参数：宿主机手动跑仍走 $HOME 默认。"""
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.delenv("WB_CLIENT_CACHE", raising=False)
    monkeypatch.delenv("WB_SESSION_CACHE", raising=False)

    renew_if_needed(conn, clock, runner=fake_run, env_path=tmp_path / "westock.env")

    cmd = captured["cmd"]
    assert "--client-cache" not in cmd and "--session-cache" not in cmd, cmd


def test_renew_runs_every_day_not_just_weekdays(conn, CFG, tmp_path):
    """续期必须**每天**跑，不能只挂 mon-fri。

    token 的有效期是腾讯给的，不认我们的交易日历。曾经（周日）13:49
    到期，而续期任务只挂 mon-fri：周六、周日的 08:40 都不跑，
    直到周一 08:40 才动——那时 token 已经过期 18.8 小时。
    周末虽然不交易，但「过期期间任何一次调用都失败」是不可接受的，
    而且周一开盘前的补救窗口本来就该留足。
    """
    clock = FixedClock(T0)
    sched, _state = make_scheduler(CFG, conn, clock, data_dir=str(tmp_path))
    jobs = {j.id: j for j in sched.get_jobs()}
    fields = {f.name: str(f) for f in jobs["westock_renew"].trigger.fields}
    assert fields["day_of_week"] == "*", (
        f"续期只挂在工作日（day_of_week={fields['day_of_week']}）。"
        "token 到期日由腾讯决定，可能是周末——那样会一直拖到周一才续，"
        "周末的调用全部失败。")


def test_renew_catches_up_on_startup(conn, CFG, tmp_path):
    """启动时补跑一次续期，兜住「容器恰好在 08:40–08:46 重启」的场景。

    cron 的 `misfire_grace_time` 只有 5 分钟：08:40 那一刻进程不在，这次就整个丢了。
    部署往往就在早上做，而 token 到期日可能是周日——错过当天，下一个机会是
    次日 08:40，那时大概率已过期。
    `renew_if_needed` 本身幂等（剩 ≥24h 直接 skip、当天已成功也 skip），
    所以补跑不会重复打腾讯。
    """
    clock = FixedClock(T0)
    sched, _state = make_scheduler(CFG, conn, clock, data_dir=str(tmp_path))
    jobs = {j.id: j for j in sched.get_jobs()}
    assert "westock_renew_startup" in jobs, (
        "没有启动补跑任务：容器在 08:40–08:46 之间重启就会丢掉当天的续期")
    # 必须是「一次性」的 date 触发，不能是循环——否则等于把频率调成每分钟
    assert jobs["westock_renew_startup"].trigger.__class__.__name__ == "DateTrigger"


def test_renew_decisions_are_logged(conn, clock, runner, caplog):
    """续期的每个决定都要留日志——包括「跳过」以及为什么跳过。

    这个模块原本一行日志都没有，只有 `app_secret` 里的标记。结果是
    「为什么三天没续期」只能靠反推 SQL，而最需要回答它的时刻恰好是
    快过期、人很急的时候。
    """
    import logging

    from jobs.westock_renew import EXPIRES_KEY, kv_set, renew_if_needed

    # 1) 剩余充足 → skip，日志要说出「还剩多久」
    kv_set(conn, EXPIRES_KEY, str(int(clock.now().timestamp()) + 3 * 86400))
    with caplog.at_level(logging.INFO, logger="westock_renew"):
        caplog.clear()
        assert renew_if_needed(conn, clock, runner) == "skip"
    msgs = [r.message for r in caplog.records if r.name == "westock_renew"]
    assert any("skip" in m for m in msgs), f"跳过时没有日志：{msgs}"
    assert any("剩" in m or "expires" in m for m in msgs), \
        f"日志没说清为什么跳过：{msgs}"

    # 2) 当天已成功 → skip，日志要说明是"今天已经续过"
    conn.execute("DELETE FROM app_secret WHERE name = ?", (EXPIRES_KEY,))
    conn.commit()
    day_key = f"westock_renew.{clock.today()}"
    kv_set(conn, day_key, f"ok:{int(clock.now().timestamp())}")
    with caplog.at_level(logging.INFO, logger="westock_renew"):
        caplog.clear()
        assert renew_if_needed(conn, clock, runner) == "skip"
    msgs = [r.message for r in caplog.records if r.name == "westock_renew"]
    assert any("already" in m or "已经" in m or "当天" in m for m in msgs), \
        f"日志没说清是当天已续过：{msgs}"

    # 3) 真的续期 → 要有结果日志
    conn.execute("DELETE FROM app_secret WHERE name = ?", (day_key,))
    conn.commit()
    with caplog.at_level(logging.INFO, logger="westock_renew"):
        caplog.clear()
        assert renew_if_needed(conn, clock, runner) == "renewed"
    msgs = [r.message for r in caplog.records if r.name == "westock_renew"]
    assert any("renew" in m.lower() for m in msgs), f"续期成功没有日志：{msgs}"
