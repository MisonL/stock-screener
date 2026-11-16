"""每日增量（15:05 快照 append）、除权检测、ST 区间与停牌日历。"""
from datetime import datetime as dt

import pytest

from core.clock import FixedClock, TradingCalendar
from core.datasource.westock import CorpAction
from core.models import Quote
from jobs.eod import (
    refresh_actions,
    refresh_sector_members,
    refresh_suspensions,
    resume_dates,
    run_eod,
    st_status_of,
)
from tests.helpers import FakeWestockCli

TODAY = 20261030


def _run(env, reg, when):
    return run_eod(env.cfg, reg, env.conn, FixedClock(when))


def test_non_trading_day_skips(env):            # Review Focus #5：快照时间戳是 09-30，今天 10-05
    rep = _run(env, env.reg_holiday, dt(2026, 10, 5, 15, 5))
    assert rep.skipped
    assert env.count("daily_bars", code="sz000001", date=20261005) == 0


def test_eod_time_gate(env):                    # S-A13：当日 bar 只在 now ≥ close_time + 5min 才入库
    rep = _run(env, env.reg_today, dt(2026, 10, 30, 14, 58))   # close_time 14:57 + 5min = 15:02
    assert env.count("daily_bars", date=20261030) == 0 and rep.appended == 0


def test_placeholder_gates(env):                # Review Focus #5：盘前占位不入库，历史一字板保留
    env.conn.execute(
        "INSERT INTO daily_bars VALUES ('sz000001', 20261029, 11.9, 11.9, 11.9, 11.9, 1000, 10000, 'backfill')"
    )
    env.conn.commit()
    rep = _run(env, env.reg_placeholder, dt(2026, 10, 30, 15, 5))
    assert rep.placeholders == 1 and env.count("daily_bars", code="sz000001", date=20261030) == 0
    assert env.count("daily_bars", code="sh600080", date=20260812) == 1   # 四价相等但 volume>0


def test_appends_today_bar_and_adj(env):        # f[5]/f[33]/f[34]/f[3] 落成当日 bar，并立即进 adj
    rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    assert rep.appended == 3 and env.count("daily_bars", date=20261030) == 3
    row = env.one("daily_bars", code="sz000001", date=20261030)
    assert (row["open"], row["high"], row["low"], row["close"]) == (12.0, 12.12, 11.88, 12.0)
    assert row["volume"] == 2000 and row["amount"] == 24000 and row["source"] == "tencent"
    assert env.count("daily_bars_adj", code="sz000001", date=20261030) == 1     # Task 10：新增 bar 立刻进 adj


def test_exright_detected_writes_implied(env):  # f[4] 11.35 vs 库里 11.60
    env.conn.execute(
        "INSERT INTO daily_bars VALUES ('sz000001', 20260923, 11.6, 11.6, 11.6, 11.6, 1000, 10000, 'backfill')"
    )
    env.conn.commit()
    for source in (env.reg_today.primary, env.reg_today.fallback):
        source.ts = dt(2026, 9, 24, 15, 0)
    env.reg_today.primary.set("sz000001", prev_close=11.35, price=11.20, open=11.30, high=11.40, low=11.10)

    _run(env, env.reg_today, dt(2026, 9, 24, 15, 5))

    row = env.one("corporate_actions", code="sz000001", ex_date=20260924)
    assert row["source"] == "snapshot_implied"
    assert row["implied_factor"] == pytest.approx(11.60 / 11.35)


def test_exright_insert_is_idempotent(env):     # 与 Task 22 的盘中写者同键 → INSERT OR IGNORE，不撞 PK
    env.conn.execute(
        "INSERT INTO daily_bars VALUES ('sz000001', 20260923, 11.6, 11.6, 11.6, 11.6, 1000, 10000, 'backfill')"
    )
    env.conn.commit()
    for source in (env.reg_today.primary, env.reg_today.fallback):
        source.ts = dt(2026, 9, 24, 15, 0)
    env.reg_today.primary.set("sz000001", prev_close=11.35, price=11.20, open=11.30, high=11.40, low=11.10)

    _run(env, env.reg_today, dt(2026, 9, 24, 15, 5))
    rep = _run(env, env.reg_today, dt(2026, 9, 24, 15, 5))

    assert env.count("corporate_actions", code="sz000001", ex_date=20260924) == 1
    assert rep.exrights == 0                    # 第二次被 OR IGNORE 静默跳过


def test_exright_skipped_when_westock_dividend_exists(env):
    env.conn.execute(
        "INSERT INTO daily_bars VALUES ('sz000001', 20260923, 11.6, 11.6, 11.6, 11.6, 1000, 10000, 'backfill')"
    )
    env.conn.execute(
        "INSERT INTO corporate_actions(code, ex_date, source, cash_per_share) "
        "VALUES ('sz000001', 20260924, 'westock_dividend', 0.25)"
    )
    env.conn.commit()
    for source in (env.reg_today.primary, env.reg_today.fallback):
        source.ts = dt(2026, 9, 24, 15, 0)
    env.reg_today.primary.set("sz000001", prev_close=11.35, price=11.20, open=11.30, high=11.40, low=11.10)

    _run(env, env.reg_today, dt(2026, 9, 24, 15, 5))

    assert env.count("corporate_actions", code="sz000001", ex_date=20260924) == 1
    assert env.one("corporate_actions", code="sz000001", ex_date=20260924)["source"] == "westock_dividend"


def test_gap_backfills_but_suspension_does_not(env):   # 停牌股不触发补拉，真缺口触发一次 kline_daily(code, 30)
    env.conn.executemany(
        "INSERT INTO daily_bars VALUES (?, 20260901, 10, 10, 10, 10, 1000, 10000, 'backfill')",
        [("sh600519",), ("sz000001",)],
    )
    env.conn.commit()
    env.reg_today.primary.set("sh600519", volume=0)        # 当日量为 0 = 停牌
    env.reg_today.primary.pop("sz000001")                  # 快照缺行 + 历史停在 09-01 = 真缺口

    rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))

    # 缺口口径是「缺上一交易日那一根」：sz000001（历史停在 09-01）与 sh600080（历史只有 08-12）
    # 都算真缺口；sh600519 当日在停牌集合里，不补拉。
    assert rep.suspended == 1 and rep.gaps == 2
    calls = set(env.reg_today.primary.kline_calls)
    assert calls == {("sz000001", 30), ("sh600080", 30)}
    assert env.count("daily_bars", code="sh600519", date=20261030) == 0


def test_st_interval_opened_on_name_change(env):        # 「中国平安」→「ST平安」
    env.conn.execute(
        "INSERT INTO stock_status_history(code, start_date, end_date, st_status) "
        "VALUES ('sz000001', 19900101, NULL, 'normal')"
    )
    env.conn.commit()
    env.reg_today.primary.set("sz000001", name="ST平安")

    _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))

    old = env.conn.execute(
        "SELECT end_date FROM stock_status_history WHERE code='sz000001' AND start_date=19900101"
    ).fetchone()
    new = env.one("stock_status_history", code="sz000001", start_date=20261030)
    assert old[0] == 20261029                 # 旧区间关闭在上一交易日
    assert new["st_status"] == "ST" and new["end_date"] is None


def test_st_first_deploy_writes_19900101(env):          # 决定 #8：首次部署按当时状态写 19900101
    env.reg_today.primary.set("sz000001", name="ST平安")

    _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))

    row = env.one("stock_status_history", code="sz000001", start_date=19900101)
    assert row["st_status"] == "ST" and row["end_date"] is None


@pytest.mark.parametrize(
    "name,status",
    [
        ("中国平安", "normal"),
        ("ST平安", "ST"),
        ("*ST平安", "*ST"),
        ("S平安", "ST"),          # 未股改的 S 前缀（历史变体，数字 F12）
        ("SST天海", "ST"),        # 同时带 S 与 ST，仍按 ST 记
        ("退市平安", "delisting"),
    ],
)
def test_st_prefix_variants(name, status):
    assert st_status_of(name) == status


def test_refresh_suspensions_writes_calendar(env):      # D24：落 suspension_calendar
    """`end_date` 落的是**最后一个停牌日**，不是接口给的复牌日。

    下游两个消费者（`core/strategy/pool.py::_resume_dates` 用 `bisect_right`、
    `jobs/eod.py::resume_dates` 用 `end_date == 上一交易日`）都按这个契约解释。
    接口给 20260903 复牌，直接写 0903 会让策略晚一个交易日才认到复牌。
    没有交易日历时按「复牌日 - 1 天」近似（这里是 0902）。
    """
    assert refresh_suspensions(env.cfg, env.westock, env.conn, codes=["sh600519"]) == 1
    assert env.count("suspension_calendar", code="sh600519") == 1
    row = env.one("suspension_calendar", code="sh600519")
    assert (row["start_date"], row["end_date"]) == (20260901, 20260902)


def test_refresh_suspensions_filters_by_codes(env):
    env.westock.suspensions["sz000001"] = (20260910, 20260911)
    assert refresh_suspensions(env.cfg, env.westock, env.conn, codes=["sh600519"]) == 1
    assert env.count("suspension_calendar", code="sz000001") == 0


def test_resume_dates_derived_from_calendar(env):
    """复牌日 = `end_date` 之后的**第一个交易日**（与 `pool._resume_dates` 同口径）。

    早先这里要求 `end_date == 上一交易日` 严格相等：`end_date` 落在休市日时
    （旧版本按自然日 −1 写下的遗留数据）就永远匹配不到，同一个概念在两个模块
    里还有两套算法。
    """
    env.conn.executemany(
        "INSERT INTO suspension_calendar(code, start_date, end_date) VALUES (?, ?, ?)",
        [("sh600519", 20260901, 20261029),   # 1029 是上一交易日 → 今天(1030 不在日历)…
         ("sz000001", 20260901, 20261028)],
    )
    env.conn.commit()
    cal = TradingCalendar(env.conn)
    last_day = max(cal.days)

    # 用日历里最后一天当"今天"：1029 的 end_date 不指向它，1028 的才指向
    assert resume_dates(env.conn, cal, last_day) == {"sz000001"}

    # 今天不是交易日 → 没人"今天复牌"
    assert resume_dates(env.conn, cal, TODAY) == set()


def test_refresh_actions_overwrites_snapshot_implied(env):
    env.conn.execute(
        "INSERT INTO corporate_actions(code, ex_date, source, implied_factor) "
        "VALUES ('sz000001', 20260924, 'snapshot_implied', 1.02)"
    )
    env.conn.commit()
    westock = FakeWestockCli(dividends={"sz000001": [CorpAction("sz000001", 20260924, 0.25, 0.0, 0.0)]})

    assert refresh_actions(env.cfg, westock, env.conn, ["sz000001"]) == 1
    assert westock.calls == [("batch_dividend", ("sz000001",), 10)]

    row = env.one("corporate_actions", code="sz000001", ex_date=20260924)
    assert row["source"] == "westock_dividend" and row["cash_per_share"] == 0.25
    assert env.count("corporate_actions", code="sz000001", ex_date=20260924) == 1


class _FakeSina:
    """`refresh_sector_members` 只需要三个方法（参数是 SinaSource，不是 registry）。"""

    def __init__(self):
        self.member_calls = []

    def fetch_sector_list(self):
        return [("new_blhy", "玻璃行业"), ("new_dzxx", "电子信息")]

    def fetch_sector_members(self, sector):
        self.member_calls.append(sector)
        return ["sh600519", "sz000001"]


def test_refresh_sector_members(env):
    sina = _FakeSina()
    assert refresh_sector_members(sina, env.conn) == 4
    assert sina.member_calls == ["new_blhy", "new_dzxx"]
    assert env.count("sector_member", sector="new_blhy") == 2
    row = env.one("sector_meta", sector="new_blhy")
    assert row["name"] == "玻璃行业" and row["kind"] == "industry"


def test_snapshot_missing_code_is_logged(env):
    env.reg_today.primary.pop("sh600519")
    _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    assert env.one("fetch_log", code="sh600519", purpose="snapshot_eod")["status"] == "empty"


def test_quote_model_used_for_bar(env):
    """当日 bar 的四价来自快照 `f[5]/f[33]/f[34]/f[3]`（Quote 已按索引归位）。"""
    q = Quote("sz000001", "中国平安", 12.0, 11.9, 12.1, 12.2, 11.8, 2000, 24000, None, None, None,
              dt(2026, 10, 30, 15, 0), [])
    assert (q.open, q.high, q.low, q.price) == (12.1, 12.2, 11.8, 12.0)


def test_stale_snapshot_is_skipped_not_written(env):
    """15:05 跑 eod 拿到 10:00 的快照：量只累计了半天，绝不能当收盘 bar 写库。"""
    from datetime import datetime

    env.reg_today.primary.set("sh600519", ts=datetime(2026, 10, 30, 10, 0))
    rep = _run(env, env.reg_today, datetime(2026, 10, 30, 15, 5))

    assert rep.stale >= 1
    assert env.count("daily_bars", code="sh600519", date=20261030) == 0


def test_gap_backfill_does_not_overwrite_today_snapshot(env):
    """补拉只补今天之前：若把上游今天的盘中 K 线写回来，会覆盖刚验收的收盘 bar。"""
    from datetime import datetime

    env.conn.execute("INSERT INTO daily_bars VALUES ('sz000001', 20261028, 10,10,10,10, 1e6, 1e7, 'backfill')")
    env.conn.commit()
    # 快照前收 = 补回来的 10-29 收盘（10.0），现价 10.9：正常涨跌，不是除权
    from tests.conftest import quote_row

    env.reg_today.primary.rows["sz000001"] = quote_row("中国平安", 10.9, 10.0, 123456,
                                                       open=10.5, high=10.95, low=10.45)
    env.reg_today.primary.gap_bars = [
        ("sz000001", 20261029, 10.0, 10.0, 10.0, 10.0, 1e6, 1e7),
        ("sz000001", 20261030, 10.5, 10.5, 10.5, 10.5, 1.0, 1e5),   # 上游的盘中行：必须被丢弃
    ]
    _run(env, env.reg_today, datetime(2026, 10, 30, 15, 5))

    row = env.conn.execute(
        "SELECT close, volume FROM daily_bars WHERE code='sz000001' AND date=20261030").fetchone()
    assert row is not None and row["volume"] == 123456.0        # 是快照的值，不是补拉的
    assert env.count("daily_bars", code="sz000001", date=20261029) == 1      # 缺的那根补上了


def test_snapshot_one_minute_before_close_is_stale(env):
    """14:56 的快照不含收盘集合竞价量：不能宽限放行，必须判陈旧。"""
    from datetime import datetime

    env.reg_today.primary.ts = datetime(2026, 10, 30, 14, 56)
    rep = _run(env, env.reg_today, datetime(2026, 10, 30, 15, 5))
    assert rep.stale >= 1 and rep.appended == 0


# ---- 缺口补拉的上限与去重（调研发现的三处）-------------------------------
# 最坏情形：某天 eod 没跑成 → 次日 `last_date != prev_day` 对**全宇宙**成立，
# 5577 只逐只 kline = 28 分钟、5577 次请求，而且这段时间独占 jobs 唯一的
# 限速出口，三循环全部排队。上游被 WAF 封过（spec:2481），这是雪崩风险。


def test_gap_backfill_is_capped(env, monkeypatch):
    """缺口票数超过上限时**停止补拉并计数**，不能无限制地打上游。"""
    from jobs import eod as eod_mod

    monkeypatch.setattr(eod_mod, "GAP_MAX", 1)
    # 三只票都缺 T-1：sz000001 / sh600080 / sh600519
    rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    calls = env.reg_today.primary.kline_calls
    assert len(calls) <= 1, f"超过上限还在补拉：{calls}"
    assert rep.gaps_deferred >= 1, "被推迟的缺口没有计数（事后查不出来）"


def test_gap_backfill_does_not_retry_same_code(env):
    """同一只票在一次 eod 里只补一次。

    两遍循环都会命中「缺 T-1」的票，第一遍补完的必须进第二遍的排除集，
    否则同一只票当天被拉两次。
    """
    rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    calls = [c for c, _ in env.reg_today.primary.kline_calls]
    dupes = {c for c in calls if calls.count(c) > 1}
    assert not dupes, f"同一只票被补拉多次：{dupes}"


def test_gap_failure_does_not_clobber_ok_log(env):
    """缺口补拉失败不能把当天已成功的 `snapshot_eod` 行改成 0/error。

    那一行代表「这只票今天的快照已入库」，被补拉失败覆盖后，
    事后完全看不出它其实成功了。
    """
    from jobs import eod as eod_mod

    original = eod_mod._backfill_gap

    def _boom(reg, conn, code, now):
        # 走**真实**的错误分支：记 gap_backfill，而不是手写 snapshot_eod
        eod_mod._log(conn, code, 0, "error", now, purpose="gap_backfill")
        return False

    eod_mod._backfill_gap = _boom
    try:
        _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    finally:
        eod_mod._backfill_gap = original
    row = env.one("fetch_log", code="sz000001", purpose="snapshot_eod")
    assert row["status"] == "ok" and row["n"] == 1, f"补拉失败覆盖了成功记录：{dict(row)}"


# ---- 复用盘中 market 快照（调研：eod 又发 10 个请求拿同一份数据）---------


def test_eod_reuses_fresh_market_snapshot(env):
    """`snapshot_cache('market')` 是今天且不早于收盘 → **不发快照请求**。

    eod 的全宇宙快照与 market 盘中快照是同一个请求（同 universe、同 CHUNK、
    同 registry 出口）。15:05 market 循环刚写过一份，eod 却又发 10 个。
    """
    import json

    from jobs import eod as eod_mod

    # 造一份「今天、收盘后」的 market 缓存
    rows = []
    for code in ("sz000001", "sh600080", "sh600519"):
        rows.append({"code": code, "name": code, "price": 12.0, "prev_close": 12.0,
                     "open": 12.0, "high": 12.12, "low": 11.88, "volume": 2000,
                     "amount": 24000, "ts": "2026-10-30T15:04:00"})
    env.conn.execute(
        "INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES (?,?,?)",
        ("market", "2026-10-30T15:04:00", json.dumps({"as_of": "2026-10-30T15:04:00",
                                                      "rows": rows})))
    env.conn.commit()

    before = len(env.reg_today.primary.snapshot_calls) if hasattr(
        env.reg_today.primary, "snapshot_calls") else 0
    rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    # 复用了缓存 → 没有走快照抓取
    assert rep.reused_snapshot is True, "没有识别出可复用的 market 快照"


def test_eod_does_not_reuse_stale_market_snapshot(env):
    """缓存的 as_of 早于收盘时间 → 必须自己拉（否则把 10:00 的量当收盘量）。"""
    import json

    env.conn.execute(
        "INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES (?,?,?)",
        ("market", "2026-10-30T10:00:00",
         json.dumps({"as_of": "2026-10-30T10:00:00", "rows": []})))
    env.conn.commit()
    rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    assert rep.reused_snapshot is False, "复用了过期快照"


def test_eod_reuse_requires_close_buffer_not_just_close_time(env):
    """`close_time`(14:57) 之后的缓存**仍然可能早于收盘**。

    14:57 是集合竞价开始，真正的收盘价 15:00 才定。market 循环拉到 15:05，
    所以 14:57:05 那份缓存会让复用路径把「不含收盘竞价」的量写成收盘 bar
    ——正是本文件 `_is_stale` 声称要拦的情形。
    """
    import json

    env.conn.execute(
        "INSERT OR REPLACE INTO snapshot_cache(scope, as_of, payload) VALUES (?,?,?)",
        ("market", "2026-10-30T14:57:05",
         json.dumps({"as_of": "2026-10-30T14:57:05", "rows": [
             {"code": "sz000001", "name": "x", "price": 12.0, "prev_close": 12.0,
              "open": 12.0, "high": 12.12, "low": 11.88, "volume": 2000,
              "amount": 24000, "ts": "2026-10-30T14:57:05"}]})))
    env.conn.commit()
    rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    assert rep.reused_snapshot is False, "复用了 14:57:05（早于 15:00 收盘）的快照"


def test_gap_failure_must_not_fabricate_exrights(env):
    """补昨日失败时**不能**拿 T-2 的收盘去反推除权。

    缺 T-1 时 `last_close` 是 T-2 的收盘，跟今天的 `prev_close`（T-1 收盘）比
    必然有差——那是「正常涨跌 + 缺一根」，不是除权。写进 `snapshot_implied`
    会污染复权价（实测偏低约 1.96%），而它正是选股输入。
    """
    from jobs import eod as eod_mod

    # 造「有 T-2、缺 T-1」：T-2(20261028) 收盘 11.5，T-1(20261029) 缺失。
    # 今天快照 price=11.9 / prev_close=11.9 → 若拿 T-2 的 11.5 去比，
    # 会误判成除权 11.5/11.9 = 0.966
    env.conn.execute(
        "INSERT INTO daily_bars VALUES ('sz000001', 20261028, 11.5, 11.5, 11.5, 11.5, 1000, 10000, 'backfill')")
    env.conn.commit()

    original = eod_mod._backfill_gap
    eod_mod._backfill_gap = lambda reg, conn, code, now: False      # 补拉失败
    try:
        rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    finally:
        eod_mod._backfill_gap = original

    implied = env.conn.execute(
        "SELECT * FROM corporate_actions WHERE code='sz000001' AND source='snapshot_implied'"
    ).fetchall()
    assert not implied, f"补拉失败仍伪造了除权因子：{[dict(r) for r in implied]}"


def test_gap_cap_covers_both_passes_and_failures(env):
    """`GAP_MAX` 必须约束**两遍合计的请求尝试数**，失败也算。

    旧实现：第一遍的「除权前补拉」没有闸门、`report.gaps` 只数成功，
    205 只全失败时会发 **410 次**（每只两遍各一次），而 `gaps_deferred=0`。

    **不能用 `_backfill_gap` 的 lambda 替身**：那样根本不经过 registry，
    断言 `kline_calls` 永远看不到真实请求数（这正是 Codex 抓到的假绿）。
    这里保留真实 `_backfill_gap`，只让上游抛错。
    """
    from jobs import eod as eod_mod
    from core.datasource.tencent import UpstreamError

    # **必须同时进假快照**：不在快照里的票会先被「empty/stale」分支拦掉，
    # 根本走不到缺口分支（上版测试就栽在这，变异也不会红）
    from tests.conftest import quote_row

    codes = [f"sz00{i:04d}" for i in range(6)]
    for c in codes:
        env.conn.execute(
            "INSERT OR IGNORE INTO stock_meta(code, name, market, updated_at)"
            " VALUES (?, ?, 'cn_main', '2026-10-30T00:00:00+08:00')", (c, c))
        env.conn.execute(
            "INSERT INTO daily_bars VALUES (?, 20261028, 10, 10, 10, 10, 1000, 10000, 'backfill')", (c,))
        for src in (env.reg_today.primary, env.reg_today.fallback):
            src.set(c, **quote_row("测试", 10.5, 10.0, 1000))
    # **移除 fixture 自带的票**：sh600080 有 20260812 的 bar、sh600519/sz000001 也会
    # 被算进缺口，混在一起就无法断言「6 只票、上限 2」
    for src in (env.reg_today.primary, env.reg_today.fallback):
        for c in ("sh600519", "sh600080"):
            src.pop(c)
    env.conn.commit()

    # **包装原函数**而不是替换：`kline_calls.append` 在假源内部，
    # 直接换成抛错的 lambda 会让计数器永远是空，断言恒真（就是假绿来源）。
    src = env.reg_today.primary
    real_kline = src.fetch_kline_daily

    def boom(code, count=1700, end=None, scope="backfill"):
        src.kline_calls.append((code, count))     # 先记账（真实请求已经发出）
        raise UpstreamError("upstream down")

    old_max = eod_mod.GAP_MAX
    eod_mod.GAP_MAX = 2
    src.fetch_kline_daily = boom
    src.kline_calls.clear()
    try:
        rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    finally:
        eod_mod.GAP_MAX = old_max
        src.fetch_kline_daily = real_kline

    calls = len(src.kline_calls)
    assert calls == 2, f"失败的缺口补拉没有被上限约束：{calls} 次（上限 2）"
    assert rep.gaps_deferred >= 1, "被推迟的缺口没有计数"



def test_deferred_gaps_are_persisted_for_next_run(env):
    """被推迟的缺口必须**落库**，否则下次 eod 看不到、backfill 也会跳过它。

    旧实现只有一个内存计数 `gaps_deferred`：今天的 bar 照样追加，明天
    `last_date` 已经是昨天，eod 认为没有缺口；而 `backfill` 的 resume 按
    `fetch_log purpose='backfill' status='ok'` 跳过，这 5 只票永远不会被补。
    历史空洞就此永久留在 raw/adj 里。
    """
    from jobs import eod as eod_mod

    env.conn.execute(
        "INSERT INTO daily_bars VALUES ('sz000001', 20261028, 11.5, 11.5, 11.5, 11.5, 1000, 10000, 'backfill')")
    env.conn.commit()

    old = eod_mod.GAP_MAX
    eod_mod.GAP_MAX = 0                      # 全部推迟
    try:
        rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    finally:
        eod_mod.GAP_MAX = old

    assert rep.gaps_deferred >= 1
    rows = env.conn.execute(
        "SELECT code, missing_through FROM gap_pending ORDER BY code").fetchall()
    assert len(rows) >= 1, "推迟的缺口没有落库，下次运行无从得知"
    # 落库条数 = 推迟的 − 之后补拉成功被清掉的
    assert len(rows) == rep.gaps_deferred - rep.gaps, (
        f"推迟 {rep.gaps_deferred}、补成功 {rep.gaps}、落库 {len(rows)}，对不上")


def test_deferred_gap_still_appends_today_bar(env):
    """配额用尽只推迟**K 线补拉**，当天快照 bar 必须照常写入。

    我加配额时写了 `continue`，把同一次循环里的当日 bar 组装也跳过了——
    205 只超限时只有 199 只写了当天 bar，6 只有效快照被丢弃。
    用一次补拉请求换一天的数据，方向反了。
    """
    from jobs import eod as eod_mod

    env.conn.execute(
        "INSERT INTO daily_bars VALUES ('sz000001', 20261028, 11.5, 11.5, 11.5, 11.5, 1000, 10000, 'backfill')")
    env.conn.commit()

    old = eod_mod.GAP_MAX
    eod_mod.GAP_MAX = 0                       # 全部推迟补拉
    try:
        rep = _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))
    finally:
        eod_mod.GAP_MAX = old

    assert rep.gaps_deferred >= 1
    assert env.count("daily_bars", code="sz000001", date=20261030) == 1, \
        "补拉被推迟，但当天快照 bar 也一起丢了"


def test_actions_cursor_walks_the_whole_list_then_wraps(env):
    """游标滚动推进：每轮从上次停下的地方继续，走到末尾后回到开头。

    全市场 5000 多只、每轮只能发 6 个请求（上游限频），一次拉完不可能。
    游标让"每 30 分钟推进一段"能真正覆盖全市场，而不是每次都从头拉同一批。
    """
    from jobs.eod import actions_cursor, advance_actions_cursor

    codes = [f"sh60{i:04d}" for i in range(1, 21)]        # 20 只
    assert actions_cursor(env.conn, codes)[0] == codes[0]  # 无游标：从头开始

    advance_actions_cursor(env.conn, codes, codes[:10])
    rest = actions_cursor(env.conn, codes)
    assert rest[0] == codes[10], f"第二轮应从第 11 只开始，实际 {rest[0]}"

    advance_actions_cursor(env.conn, codes, rest)          # 走完剩余
    assert actions_cursor(env.conn, codes)[0] == codes[0], "走完应绕回开头"

    # **只走一轮就该绕回**：游标落在最后一只时，下一轮必须回到开头，
    # 否则最后那段永远拉不到（round-robin 变成"卡在末尾"）。
    advance_actions_cursor(env.conn, codes, codes[-1:])
    assert actions_cursor(env.conn, codes)[0] == codes[0], "走到最后一只后没有绕回"


def test_actions_cursor_recovers_when_its_code_is_delisted(env):
    """游标指向的代码退市后，不能卡死——按顺序取第一个更大的。"""
    from jobs.eod import actions_cursor

    with env.conn:
        env.conn.execute("INSERT OR REPLACE INTO job_state(name, value)"
                         " VALUES ('refresh_actions.next', 'sh600999')")
    codes = ["sh600000", "sh600001", "sh600002"]
    assert actions_cursor(env.conn, codes) == codes, "游标越界应回到开头"


def test_store_suspensions_uses_calendar_not_natural_days(env):
    """有日历时按**交易日历**往前推，而不是减一天。

    复牌日 20261012（周一）前的最后一个交易日是 20261009（周五），
    不是 20261011（周日）。减一天会把 end_date 落在休市日上，
    下游 `end_date == 上一交易日` 就永远对不上。
    """
    from jobs.eod import store_suspensions

    class Cal:
        days = {20261008, 20261009, 20261012, 20261013}

        def prev(self, d):
            earlier = [x for x in self.days if x < d]
            if not earlier:
                raise LookupError(d)
            return max(earlier)

    n = store_suspensions(env.conn, [("sh600519", 20260930, 20261012)], cal=Cal())
    assert n == 1
    row = env.one("suspension_calendar", code="sh600519")
    assert row["end_date"] == 20261009, f"应按交易日历取 1009，实际 {row['end_date']}"


def test_store_suspensions_keeps_none_for_undecided_resumption(env):
    """未定复牌（None）原样落 NULL，不能编一个确定日期。"""
    from jobs.eod import store_suspensions

    store_suspensions(env.conn, [("sh600519", 20260901, None)])
    row = env.one("suspension_calendar", code="sh600519")
    assert row["end_date"] is None


def test_store_actions_rebuilds_adj_so_prices_follow_the_new_event(env):
    """写入官方除权事件后**必须立刻重建复权表**。

    否则在下一次 EOD/回填之前，所有读 `daily_bars_adj` 的消费者拿到的仍是
    按旧事件算的价格。滚动刷新常在 EOD 之后或次日盘前跑，正好卡在这个窗口里。
    """
    from core.adjust import rebuild_adj
    from core.datasource.westock import CorpAction

    code = "sh600519"
    with env.conn:
        for d in range(20260901, 20261001):
            env.conn.execute(
                "INSERT OR REPLACE INTO daily_bars(code, date, open, high, low, close, volume, amount, source)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (code, d, 10.0, 10.0, 10.0, 10.0, 1000, 10000.0, "tencent"))
    rebuild_adj(env.conn, code)
    before = env.conn.execute(
        "SELECT close FROM daily_bars_adj WHERE code=? AND date=?",
        (code, 20260930)).fetchone()[0]

    # 新增一条每股派息 0.5 的除权事件
    from jobs.eod import store_actions
    store_actions(env.conn, {code: [
        CorpAction(code=code, ex_date=20260915, cash_per_share=0.5,
                   bonus_ratio=0.0, tran_ratio=0.0)]})

    after = env.conn.execute(
        "SELECT close FROM daily_bars_adj WHERE code=? AND date=?",
        (code, 20260930)).fetchone()[0]
    assert after != before, "写入除权事件后复权价没变：复权表没重建"


def test_cursor_does_not_skip_codes_missed_in_the_middle(env):
    """中间那一段没确认时，游标**不能越过它**。

    限频时常出现「开头几段正常、中间某段只回 1 只、后面又正常」。
    按 `max(done)` 推进的话，中间没确认的 9 只被整段跳过，要等滚动一整圈
    （约 3 天）才再遇到。游标只能推进到"连续确认"的断点。
    """
    from jobs.eod import actions_cursor, advance_actions_cursor

    codes = [f"sh60{i:04d}" for i in range(1, 41)]      # 40 只
    advance_actions_cursor(env.conn, codes, codes[:10])
    assert actions_cursor(env.conn, codes)[0] == codes[10]

    # 第二轮：第 2 段（codes[10:20]）只确认了第一只，后面 10:20 之后全部正常
    done = {codes[10]} | set(codes[20:40])
    advance_actions_cursor(env.conn, codes, done, start=codes[10])

    nxt = actions_cursor(env.conn, codes)[0]
    assert nxt == codes[11], (
        f"游标应停在断点 codes[11]，实际 {nxt}——"
        "中间没确认的 9 只被跳过了")


def test_undecided_suspension_is_closed_by_real_bar_evidence(env):
    """"未定复牌"的区间要能被**真实成交**闭合。

    接口只给"当前正在停牌"的快照：某只票复牌后就不在返回里了，那条
    `end_date IS NULL` 的行永远留着，于是复牌之后的**每个交易日都被算成停牌**
    ——`suspended_250` 虚高、`resume_cooldown` 永不触发。

    闭合依据只能是有成交（该股出现了晚于停牌起始日的 bar）。
    """
    from jobs.eod import store_suspensions

    code = "sh600519"
    with env.conn:
        # 交易日历：1013-1017 那周
        for d in (20261013, 20261014, 20261015, 20261016, 20261017):
            env.conn.execute(
                "INSERT OR REPLACE INTO daily_bars(code, date, open, high, low, close,"
                " volume, amount, source) VALUES ('sh000300',?,10,10,10,10,1,1,'tencent')",
                (d,))
        # 该股 1016 恢复成交
        env.conn.execute(
            "INSERT OR REPLACE INTO daily_bars(code, date, open, high, low, close,"
            " volume, amount, source) VALUES (?,20261016,10,10,10,10,1,1,'tencent')",
            (code,))

    # 落一条"未定复牌"。**此刻已知 1016 有成交**，所以应当场就闭合，
    # 不用等下一轮——落库时就能判断的事没必要留个洞。
    store_suspensions(env.conn, [(code, 20261013, None)])

    end = env.one("suspension_calendar", code=code)["end_date"]
    assert end == 20261015, (
        f"应按成交证据闭合为 1015（1016 复牌的前一交易日），实际 {end}")


def test_suspension_stays_open_when_there_is_no_bar_evidence(env):
    """**没有成交证据时不能闭合**——否则数据源失败会伪造复牌日。

    只凭"这次快照里没有它"就闭合是错的：请求残缺、限频、账号失效时同样没有它。
    """
    from jobs.eod import store_suspensions

    code = "sh600519"
    with env.conn:
        for d in (20261013, 20261014, 20261015):
            env.conn.execute(
                "INSERT OR REPLACE INTO daily_bars(code, date, open, high, low, close,"
                " volume, amount, source) VALUES ('sh000300',?,10,10,10,10,1,1,'tencent')",
                (d,))

    store_suspensions(env.conn, [(code, 20261013, None)])
    store_suspensions(env.conn, [])            # 空快照，且该股没有任何成交

    assert env.one("suspension_calendar", code=code)["end_date"] is None, \
        "没有成交证据却闭合了停牌区间：伪造了复牌日"


def test_zero_volume_bar_is_not_resumption_evidence(env):
    """**零成交量的 bar 不能当复牌证据**。

    停牌期间有的数据源仍会给一根 volume=0 / amount=0 的占位 bar
    （`core/datasource/tencent.py` 接受零量）。拿它当复牌会伪造复牌日，
    还会把停牌天数少算——`suspended_250` 偏低，本该剔除的票留在池里。
    """
    from jobs.eod import store_suspensions

    code = "sh600519"
    with env.conn:
        for d in (20261013, 20261014, 20261015, 20261016, 20261017):
            env.conn.execute(
                "INSERT OR REPLACE INTO daily_bars(code, date, open, high, low, close,"
                " volume, amount, source) VALUES ('sh000300',?,10,10,10,10,1,1,'tencent')",
                (d,))
        # 占位 bar：有日期但零成交
        env.conn.execute(
            "INSERT OR REPLACE INTO daily_bars(code, date, open, high, low, close,"
            " volume, amount, source) VALUES (?,20261016,10,10,10,10,0,0,'tencent')",
            (code,))

    store_suspensions(env.conn, [(code, 20261013, None)])

    assert env.one("suspension_calendar", code=code)["end_date"] is None, \
        "零成交量的占位 bar 被当成了复牌证据"


def test_cursor_three_arg_calls_continue_from_saved_position(env):
    """连续两次三参调用必须**接着上一轮**走，不能回到开头重算。

    这是旧的滚动调用方式；`start=None` 若被实现成"从头算连续前缀"，
    第二轮会把已经确认过的开头再算一遍，游标原地踏步。
    """
    from jobs.eod import advance_actions_cursor

    codes = [f"sh60000{i}" for i in range(5)]
    advance_actions_cursor(env.conn, codes, codes[:2])
    advance_actions_cursor(env.conn, codes, codes[2:4])

    row = env.conn.execute(
        "SELECT value FROM job_state WHERE name='refresh_actions.next'").fetchone()
    assert row[0] == codes[4], f"第二轮应推进到 {codes[4]}，实际 {row[0]}"


def test_cursor_stays_at_start_when_nothing_confirmed(env):
    """空确认集时游标**不动**，留在本轮起点，不要归零重来。"""
    from jobs.eod import advance_actions_cursor

    codes = [f"sh60000{i}" for i in range(5)]
    advance_actions_cursor(env.conn, codes, [], start=codes[2])

    row = env.conn.execute(
        "SELECT value FROM job_state WHERE name='refresh_actions.next'").fetchone()
    assert row[0] == codes[2], f"空确认集应停在 {codes[2]}，实际 {row[0]}"


def test_cursor_handles_a_retired_start_code(env):
    """起点代码已退市（不在当前列表里）时要按二分定位，而不是从头开始。"""
    from jobs.eod import advance_actions_cursor

    codes = ["sh600000", "sh600002", "sh600004"]
    advance_actions_cursor(env.conn, codes, ["sh600002"], start="sh600001")

    row = env.conn.execute(
        "SELECT value FROM job_state WHERE name='refresh_actions.next'").fetchone()
    assert row[0] == "sh600004", f"应推进到 sh600004，实际 {row[0]}"


def test_resume_dates_matches_pool_for_a_weekend_end_date(env):
    """`end_date` 落在休市日时，两个消费者必须**认同一个复牌日**。

    旧版本按自然日 −1 写下的遗留数据会出现 `end_date=20261115`（周六）。
    `pool._resume_dates` 用 bisect 能容忍，`eod.resume_dates` 的严格相等
    匹配不到——同一只票在两个地方"复牌日"不一致。
    """
    from core.strategy.pool import _resume_dates

    cal = TradingCalendar(env.conn)
    days = sorted(cal.days)
    # 取一个"周六"（不在日历里）当 end_date：之后第一个交易日
    weekend = next(d for d in range(days[-1] - 7, days[-1]) if d not in cal.days)
    resume_day = next(d for d in days if d > weekend)

    with env.conn:
        env.conn.execute(
            "INSERT INTO suspension_calendar(code, start_date, end_date) VALUES (?,?,?)",
            ("sh600519", weekend - 20, weekend))

    pool_set = _resume_dates([(weekend - 20, weekend)], cal, resume_day)
    eod_set = resume_dates(env.conn, cal, resume_day)

    assert resume_day in pool_set, "pool 侧没算出复牌日（测试前提就不成立）"
    assert eod_set == {"sh600519"}, (
        f"eod 侧认不出这只票的复牌日；pool 侧是 {pool_set}，两处口径不一致")


def test_gap_backfill_network_happens_before_the_write_transaction(env):
    """缺口补拉的**联网必须在写事务之外**。

    `run_eod` 的事务要写几千行、跑 1～3 分钟。再把 HTTP 塞进去会让
    SQLite 的单写者锁被占得更久——快照线程、请求记账等满 `busy_timeout`
    就 `database is locked`，而记账钩子会把这种失败静默吞掉。

    做法：在事务真正开始写入那一刻检查 `conn.in_transaction`，
    `kline_daily` 被调用时连接不该已经在事务中。
    """
    env.conn.executemany(
        "INSERT INTO daily_bars VALUES (?, 20260901, 10, 10, 10, 10, 1000, 10000, 'backfill')",
        [("sz000001",)],
    )
    env.conn.commit()
    env.reg_today.primary.pop("sz000001")       # 历史停在 09-01 = 真缺口

    in_tx_at_fetch: list[bool] = []
    source = env.reg_today.primary
    original = source.fetch_kline_daily

    def spy(code, count=1700, end=None, scope="backfill"):
        in_tx_at_fetch.append(env.conn.in_transaction)
        return original(code, count, end, scope)

    source.fetch_kline_daily = spy
    _run(env, env.reg_today, dt(2026, 10, 30, 15, 5))

    assert in_tx_at_fetch, "缺口补拉根本没被调用（测试前提不成立）"
    assert not any(in_tx_at_fetch), (
        "缺口补拉的 HTTP 发生在写事务内：会把 SQLite 的写锁占得更久")
