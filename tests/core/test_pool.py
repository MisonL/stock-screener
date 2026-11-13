"""Task 18：排除规则、观察池生成与全量对拍（spec §4.4.1、§4.3、§4.6.1）。

三件事各一组用例：

1. **`exclude` 的每一条 §4.4.1 规则**（ST / 有效 bar 数 / 停牌 / 20 日均额 / 横盘期均额 /
   B 股 / 涨跌停价未知）都必须**单独**可触发，理由串互不串味——否则漏斗报表按理由分组时
   会把「僵尸股」与「停牌股」混成一栏。
2. **`build_pool` 的写库口径**：raw 价必须由**当日**因子桥接回不复权（`box_high_raw =
   box_high_adj / factor_T`）；相等分数按 `code ASC`；除权日整只不入池；`candidates`
   与 `size` 分开计数，池子告警按 `candidates` 判。
3. **D24 复牌巨量不进 B4 分母**：复牌日及其后 5 根的量不能进 `decl.mean_vol`，否则
   一只复牌翻倍的票会因为分母被抬高而「看起来缩量」。

夹具形态（`tests.helpers.seed_pool_stock`）：300 根 bar，前 240 根从 100 线跌到 61
（回撤 39%、下跌期 L≈209、下跌量 3e6），后 60 根平在 61（横盘 N*=40、量 1e6 → b4 ≈ 0.42、
横盘期均额 ≈ 6.1e7 > 5000 万门槛）。
"""
import json

import pytest

from core.strategy.filters import exclude
from core.strategy.pool import build_pool, refresh_stock_stats
from tests.helpers import AS_OF, set_bar_volume, seed_index_bars, seed_pool_stock, _trading_days_rev

MARKET = "cn_main"


def codes_in_pool(conn, market=MARKET, as_of=AS_OF):
    return [
        r[0]
        for r in conn.execute(
            "SELECT code FROM pool WHERE trade_date = ? AND market = ? ORDER BY rank",
            (as_of, market),
        )
    ]


def pool_row(conn, code, market=MARKET, as_of=AS_OF):
    return conn.execute(
        "SELECT * FROM pool WHERE trade_date = ? AND market = ? AND code = ?",
        (as_of, market, code),
    ).fetchone()


# ---------------------------------------------------------------- 池子构建


def test_order_is_score_then_code(db_two_equal_scores, CFG):
    """分数相等时按 code 升序——排序不确定会让「观察池 Top 100」每天抖动。"""
    report = build_pool(db_two_equal_scores, CFG, MARKET, AS_OF)
    assert codes_in_pool(db_two_equal_scores) == ["sh600001", "sh600002"]
    assert report.candidates == 2
    assert report.size == 2
    assert report.rejects == {}


def test_raw_box_bridged_by_same_day_factor(db, CFG):
    """`box_high_raw` 必须用当日因子桥接（§4.6.9：盘中只用 raw 价判突破）。"""
    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    seed_pool_stock(db, "sh600519", adj_factor=3.0358)
    build_pool(db, CFG, MARKET, AS_OF)

    row = pool_row(db, "sh600519")
    assert row is not None
    assert row["box_high_raw"] == pytest.approx(row["box_high_adj"] / 3.0358)
    assert row["box_high_raw"] == pytest.approx(61.3, abs=0.01)
    assert row["close_raw"] == pytest.approx(61.0, abs=0.01)
    assert row["box_vol_mean"] == pytest.approx(1e6)


def test_exright_day_not_in_pool(db, CFG):
    """当日除权（`corporate_actions.ex_date == as_of`）的票不入池——复权序列当天还没对齐。"""
    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    seed_pool_stock(db, "sh600519")
    db.execute(
        "INSERT INTO corporate_actions(code, ex_date, source, cash_per_share, bonus_ratio, tran_ratio) "
        "VALUES ('sh600519', ?, 'westock_dividend', 1.0, 0.0, 0.0)",
        (AS_OF,),
    )
    db.commit()

    report = build_pool(db, CFG, MARKET, AS_OF)
    assert codes_in_pool(db) == []
    assert report.rejects["sh600519"] == ("filter", "exright")
    assert report.size == 0


def test_pool_low_alert(db_with_12_hits, CFG):
    """`pool_low` 按 `candidates` 判：12 只过完成部门槛 → 告警，且两个数都记下来。"""
    report = build_pool(db_with_12_hits, CFG, MARKET, AS_OF)
    assert "pool_low" in report.alerts
    assert report.candidates == 12
    assert report.size == 12
    assert len(codes_in_pool(db_with_12_hits)) == 12


def test_resume_day_excluded_from_b4_denominator(db, CFG):
    """D24：复牌巨量（及其后 5 根）不进 B4 的分母 `decl.mean_vol`。"""
    days, _ = seed_pool_stock(db, "sh600519")
    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    # 第 200..204 个交易日停牌，第 205 天复牌，复牌当天量是常态的 10 倍。
    db.execute(
        "INSERT INTO suspension_calendar(code, start_date, end_date) VALUES ('sh600519', ?, ?)",
        (days[200], days[204]),
    )
    set_bar_volume(db, "sh600519", days[205], 30e6)
    db.commit()

    build_pool(db, CFG, MARKET, AS_OF)

    row = pool_row(db, "sh600519")
    assert row is not None
    factors = json.loads(row["factors"])
    # 分母 = 剔除复牌首日与其后 5 根后的横盘前均量：35 根下跌（3e6）+ 19 根横盘（1e6），共 54 根
    expected_decl_mean = (35 * 3e6 + 19 * 1e6) / 54
    assert factors["items"]["B4"]["raw"] == pytest.approx(1e6 / expected_decl_mean, rel=1e-9)
    # 若把复牌巨量算进分母，b4 会明显更小——上界就够区分两种实现
    assert factors["items"]["B4"]["raw"] > 1e6 / ((41 * 3e6 + 19 * 1e6) / 60)


# ---------------------------------------------------------------- 盘后调用（jobs/after_close）


def test_after_close_skips_when_eod_skipped(db_two_equal_scores, CFG):
    """`run_eod` 跳过（节假日 / 当日 bar 未定盘）时不出池——不然会拿昨天的序列当今天。"""
    from datetime import datetime
    from types import SimpleNamespace

    from core.clock import FixedClock
    from jobs.after_close import run

    clock = FixedClock(datetime(2026, 10, 30, 15, 5))
    assert run(CFG, db_two_equal_scores, clock, eod_report=SimpleNamespace(skipped=True)) == []


def test_after_close_builds_every_enabled_market(db_two_equal_scores, CFG):
    from datetime import datetime

    from core.clock import FixedClock
    from jobs.after_close import run

    clock = FixedClock(datetime(2026, 10, 30, 15, 5))
    reports = run(CFG, db_two_equal_scores, clock)

    assert [r.market for r in reports] == CFG.enabled_markets()
    assert reports[0].size == 2
    assert codes_in_pool(db_two_equal_scores) == ["sh600001", "sh600002"]
    # 其余启用的市场没有行情：空池 → pool_low（不能静默）
    assert all("pool_low" in r.alerts for r in reports)


# ---------------------------------------------------------------- 排除规则


META_OK = {"name": "贵州茅台", "st_status": "normal", "market": "cn_main"}
META_ST = {"name": "*ST九鼎", "st_status": "*ST", "market": "cn_main"}


def _ok(**over):
    """一笔「什么门槛都过」的输入；各用例只改要触发的那一项。"""
    values = dict(n_bars=300, amount_20d=6.1e7, box_amount=6.1e7, suspended_250=0,
                  st="normal")
    values.update(over)
    return values


def test_excluded_reasons(CFG, MAIN):
    u = CFG.strategy.universe
    # `st=None` 时从 meta 的名称前缀推（核心层不 import jobs，两处前缀表同规则）
    assert exclude("sh600080", META_ST, **_ok(st=None), p=MAIN, u=u) == "st"
    assert exclude("sh600519", META_OK, **_ok(st="*ST"), p=MAIN, u=u) == "st"
    assert exclude("sh600519", META_OK, **_ok(n_bars=264), p=MAIN, u=u) == "bars"
    assert exclude("sh600519", META_OK, **_ok(suspended_250=61), p=MAIN, u=u) == "suspended"
    assert exclude("sh600519", META_OK, **_ok(amount_20d=4.9e7), p=MAIN, u=u) == "amount_20d"
    # 横盘期均额是独立的一道门槛：20 日均额过线、横盘期只有它的 49% → 淘汰（僵尸股）
    assert exclude("sh600519", META_OK, **_ok(box_amount=3.0e7), p=MAIN, u=u) == "box_amount"
    assert exclude("sh900901", META_OK, **_ok(), p=MAIN, u=u) == "b_share"
    assert exclude("sz200002", META_OK, **_ok(), p=MAIN, u=u) == "b_share"
    # 全部达标 → None（不淘汰）
    assert exclude("sh600519", META_OK, **_ok(), p=MAIN, u=u) is None


def test_exclude_limit_unknown(CFG):
    """S-A15：快照 `f[47] == -1` 表示腾讯自己也不确定涨跌停价 → 不入池。"""
    u = CFG.strategy.universe
    bse = CFG.market("cn_bse")
    assert exclude("bj920002", META_OK, **_ok(), p=bse, u=u, limit_up=-1) == "limit_unknown"
    assert exclude("bj920002", META_OK, **_ok(), p=bse, u=u, limit_up=11.28) is None
    # 盘后出池没有快照（limit_up=None）：不拿「没有」当「不确定」
    assert exclude("bj920002", META_OK, **_ok(), p=bse, u=u, limit_up=None) is None


# ---------------------------------------------------------------- stock_stats 刷新


def test_refresh_stats_n_bars_is_unconditional_count(db):
    """`n_bars` 必须数全历史（加 `date >= d20` 会把每只都数成 20、`min_bars` 全灭）。"""
    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    days, _ = seed_pool_stock(db, "sh600519")
    n = refresh_stock_stats(db, AS_OF)
    row = db.execute("SELECT * FROM stock_stats WHERE code = 'sh600519'").fetchone()
    assert row["n_bars"] == len(days)
    assert row["amount_20d"] == pytest.approx(61.0 * 1e6, rel=1e-6)
    assert n >= 1


def test_refresh_stats_long_halt_amount_is_zero_not_null(db):
    """近 20 日一根 bar 都没有的票：`amount_20d` 必须是 0（`REAL NOT NULL` 不能是 NULL）。"""
    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    days, _ = seed_pool_stock(db, "sh600519")
    # 另一只票只有 101 根历史 bar（101 个交易日前就停了）
    for d in days[:101]:
        db.execute(
            "INSERT OR REPLACE INTO daily_bars(code,date,open,high,low,close,volume,amount,source) "
            "VALUES ('sh600520',?,10,10,10,10,100000,1000000,'backfill')",
            (d,),
        )
    db.commit()

    refresh_stock_stats(db, AS_OF)

    row = db.execute("SELECT * FROM stock_stats WHERE code = 'sh600520'").fetchone()
    assert row["n_bars"] == 101
    assert row["amount_20d"] == 0.0


def test_refresh_stats_suspension_counts_trading_days(db):
    """「近 250 个交易日内的停牌日数」按交易日算：45 个交易日 ≈ 63 自然日，不能被误淘汰。"""
    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    days, _ = seed_pool_stock(db, "sh600519")
    db.execute(
        "INSERT INTO suspension_calendar(code, start_date, end_date) VALUES ('sh600519', ?, ?)",
        (days[100], days[144]),          # 45 个交易日
    )
    db.commit()

    refresh_stock_stats(db, AS_OF)

    row = db.execute("SELECT * FROM stock_stats WHERE code = 'sh600519'").fetchone()
    assert row["suspended_250"] == 45


def test_missing_today_factor_row_rejects(db, CFG):
    """当日因子行缺失 ⇒ 淘汰，不能拿 1.0 当因子把箱顶写进 pool（错得很安静）。"""
    seed_index_bars(db, _trading_days_rev(AS_OF, 320))
    seed_pool_stock(db, "sh600519", adj_factor=3.0358)
    db.execute("DELETE FROM daily_bars_adj WHERE code='sh600519' AND date=?", (AS_OF,))
    db.execute("DELETE FROM daily_bars WHERE code='sh600519' AND date=?", (AS_OF,))
    db.commit()

    report = build_pool(db, CFG, MARKET, AS_OF)
    assert codes_in_pool(db) == []
    assert report.rejects["sh600519"] == ("data", "no_factor")
