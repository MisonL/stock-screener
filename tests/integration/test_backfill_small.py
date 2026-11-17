"""首次全量回填（假上游，不碰网）。"""
from jobs.backfill import backfill
from tests.helpers import seed_fetch_log


def test_backfill_writes_bars_meta_and_adj(tmp_env):
    rep = backfill(tmp_env.cfg, tmp_env.http, tmp_env.conn, codes=["sh600519", "sz000001"])
    assert (rep.done, rep.failed) == (2, 0)
    assert tmp_env.conn.execute("SELECT count(*) FROM daily_bars").fetchone()[0] == 2 * 1700
    assert tmp_env.conn.execute("SELECT count(*) FROM daily_bars_adj").fetchone()[0] == 2 * 1700


def test_backfill_placeholder_bar_rejected(tmp_env):    # Review Focus #5（回填侧）
    rep = backfill(tmp_env.cfg, tmp_env.http, tmp_env.conn, codes=["sh600519"], count=20)
    assert tmp_env.count("daily_bars", code="sh600519", date=20261030) == 0      # 占位 bar 被拒
    assert tmp_env.count("daily_bars", code="sh600519", date=20260812) == 1      # 历史一字跌停保留
    assert rep.bars_written == 19


def test_backfill_retries_then_records_failure(tmp_env):
    tmp_env.transport.failing.add("sh600519")
    rep = backfill(tmp_env.cfg, tmp_env.http, tmp_env.conn, codes=["sh600519", "sz000001"])
    assert rep.failed == 1 and rep.done == 1
    assert tmp_env.one("fetch_log", code="sh600519", purpose="backfill")["status"] == "error"
    assert tmp_env.http.call_count_for("sh600519") == 3          # 三次重试后放弃


def test_backfill_codes_from_sina_universe(tmp_env):   # D12
    rep = backfill(tmp_env.cfg, tmp_env.http, tmp_env.conn)      # codes=None
    codes = {r[0] for r in tmp_env.conn.execute("SELECT DISTINCT code FROM daily_bars")}
    # 交易日历 = sh000300 的日期集合；指数不入 universe 清单，回填必须显式追加
    assert {"sh000300", "sh000001", "sz399001", "sz399006", "sh000688"} <= codes
    assert rep.empty == 0
    assert tmp_env.one("stock_meta", code="sh600519")["name"] == "贵州茅台"


def test_backfill_resumes_and_skips_done(tmp_env):             # 断点续跑：已 ok 的不重拉
    seed_fetch_log(tmp_env.conn, "sh600519", n=1700, status="ok")
    seed_fetch_log(tmp_env.conn, "sz000001", n=0, status="empty")
    rep = backfill(tmp_env.cfg, tmp_env.http, tmp_env.conn, codes=["sh600519", "sz000001"], count=1700)
    assert rep.skipped == 1 and rep.done == 1
    assert tmp_env.http.call_count_for("sh600519") == 0        # 已完成的不发请求
    rep2 = backfill(tmp_env.cfg, tmp_env.http, tmp_env.conn, codes=["sh600519", "sz000001"], resume=False)
    assert rep2.skipped == 0                                   # --no-resume 强制全量重拉


# ---- 熔断与退避（调研：上游全挂时会打出 3×N 次请求）---------------------


def test_backfill_stops_when_upstream_is_down(tmp_env):
    """上游整片挂掉时**立即停止本轮**，不能把剩下的几百只各试 3 次。

    spec:2481 记录了 WAF 封禁「不是 60 秒能恢复的」。上游全挂时继续逐只
    重试 3 次，等于对已封禁的 IP 连打——5581 只 × 3 = 16743 次请求、
    83 分钟纯 sleep，而且这些请求全部失败。
    """
    codes = [f"sh6000{i:02d}" for i in range(30)]
    for c in codes:
        tmp_env.transport.failing.add(c)
    rep = backfill(tmp_env.cfg, tmp_env.http, tmp_env.conn, codes=codes, count=20)
    attempted = sum(tmp_env.http.call_count_for(c) for c in codes)
    # 熔断后不该把 30 只全试一遍（3 次/只 = 90）
    assert attempted < 90, f"上游全挂还在逐只重试：{attempted} 次"
    assert rep.aborted is True, "没有标记本轮已中止"


def test_backfill_gives_up_immediately_on_unrecoverable(tmp_env):
    """`ForbiddenHost` 是配置错误，重试 3 次纯浪费——应立即放弃这只票。"""
    from core.http import ForbiddenHost

    class _Boom:
        name = "tencent"

        def fetch_kline_daily(self, code, count=1700, scope="backfill"):
            raise ForbiddenHost("web.ifzq.gtimg.cn 已封禁")

    from jobs.backfill import _fetch

    assert _fetch(_Boom(), "sh600519", 20) is None


def test_backfill_picks_up_pending_gaps(tmp_env):
    """`gap_pending` 里的票即使 `fetch_log(backfill)=ok` 也要**重拉**。

    默认 resume 只看「曾经过」就跳过，而缺口票正是「曾经 ok、但缺了几天」——
    不认待补清单的话，历史空洞永远补不回来。
    """
    seed_fetch_log(tmp_env.conn, "sh600519", n=1700, status="ok")
    with tmp_env.conn:
        tmp_env.conn.execute(
            "INSERT INTO gap_pending(code, missing_through, first_seen, attempts)"
            " VALUES ('sh600519', 20261028, '2026-10-30T15:05:00+08:00', 0)")

    rep = backfill(tmp_env.cfg, tmp_env.http, tmp_env.conn, codes=["sh600519"], count=1700)
    assert rep.skipped == 0, "缺口票被 resume 跳过了，历史空洞永远补不回来"
    assert rep.done == 1
    left = tmp_env.conn.execute("SELECT count(*) FROM gap_pending").fetchone()[0]
    assert left == 0, "补完没有清掉待补记录"


def test_backfill_keeps_pending_when_target_day_still_missing(tmp_env, monkeypatch):
    """人工 backfill 收到**非空但没覆盖目标日**的返回时，不能清掉 pending。

    上游"成功"却只回更早的旧日线时，按 code 直接删会让缺口失去续跑入口；
    这次又写了 `fetch_log(ok)`，下次续跑会直接跳过它。
    """
    env = tmp_env
    code = "sh600519"

    # 让上游只回一根旧日线（20261028），不含目标日 20261029
    from core.models import Bar
    original = env.transport._bars
    monkeypatch.setattr(
        env.transport, "_bars",
        lambda c, n: [Bar(c, 20261028, 10, 10, 10, 10, 1000, 10000, "tencent")])

    with env.conn:
        env.conn.execute(
            "INSERT INTO gap_pending(code, missing_through, first_seen, attempts)"
            " VALUES (?, 20261029, '2026-10-30T15:05:00+08:00', 0)", (code,))

    rep = backfill(env.cfg, env.http, env.conn, codes=[code], count=30,
                   sleep_s=0.0, resume=True)

    assert rep.bars_written > 0, "前提不成立：上游没返回任何 bar"
    assert env.count("daily_bars", code=code, date=20261029) == 0, "前提不成立：目标日被补上了"
    assert rep.gap_unfilled >= 1, "没走到「目标日未补齐」分支（测试前提不成立）"
    assert env.count("gap_pending", code=code) == 1, (
        "目标日 20261029 仍缺，pending 却被删了：这只票再也不会被补到")
