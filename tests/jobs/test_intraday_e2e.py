"""盘中 tick 端到端（快照 → 规则 → 检测器 → 派发 → 库）。"""
from datetime import datetime

import pytest

from core.clock import FixedClock
from core.db import connect, migrate
from core.intraday.curve import curve_at
from core.intraday.detector import EventDetector
from core.models import Quote, SectorRow
from jobs.intraday import IntradayTick
from tests.helpers import SH

T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)


class Disp:
    def __init__(self):
        self.handled = []
        self.tick_calls = 0
        self.alerts = []
        self.briefs = []

    def handle(self, events):
        self.handled.append(list(events))

    def tick(self):
        self.tick_calls += 1

    def alert(self, kind, text, level="P1"):
        self.alerts.append((kind, text, level))

    def open_brief(self, *a, **kw):
        self.briefs.append((a, kw))


def quote(code, price, volume, prev_close=10.04, ts=T0, volume_ratio=1.0):
    return Quote(code, code, price, prev_close, 10.0, price, 9.9, volume, 1e8, volume_ratio,
                 None, None, ts, [])


@pytest.fixture
def env(tmp_path, CFG):
    conn = connect(tmp_path / "i.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO pool(trade_date, market, code, rank, score, factors, box_days, box_high_adj,"
        " box_low_adj, box_high_raw, box_low_raw, close_raw, box_vol_mean, pos_in_box)"
        " VALUES (20261029,'cn_main','sh600519',1,82.9,'{}',40,10.14,9.86,10.14,9.86,10.04,6e6,0.5)")
    conn.commit()
    clock = FixedClock(T0)
    disp = Disp()
    det = EventDetector(conn, clock, CFG)
    det.seed_today()
    tick = IntradayTick(CFG, conn, det, disp, clock)
    return type("E", (), dict(conn=conn, clock=clock, disp=disp, det=det, tick=tick, cfg=CFG))


def _vol(multiple, hm="10:30"):
    return multiple * 6e6 * curve_at(hm, SHCURVE(hm))


def SHCURVE(hm):
    from core.config import load_config
    from pathlib import Path

    return load_config(Path("config")).strategy.intraday.curve


def test_breakout_snapshot_end_to_end(env):
    hm = "10:30"
    env.tick.on_pool({"sh600519": quote("sh600519", 10.05, _vol(1.0))}, T0)   # priming
    env.tick.on_pool({"sh600519": quote("sh600519", 10.22, _vol(3.2))}, T0)

    assert env.conn.execute("SELECT count(*) FROM event_log").fetchone()[0] == 1
    assert env.conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 1
    assert len(env.disp.handled) >= 1
    assert env.disp.handled[-1][0].detection.kind == "breakout"


def test_first_tick_never_judges(env):
    env.tick.on_pool({"sh600519": quote("sh600519", 10.22, _vol(3.2))}, T0)
    assert env.conn.execute("SELECT count(*) FROM event_log").fetchone()[0] == 0


def test_exright_records_implied_once(env):
    q = quote("sh600519", 11.2, _vol(1.0), prev_close=11.35)
    env.tick.on_pool({"sh600519": q}, T0)
    env.tick.on_pool({"sh600519": q}, T0)
    rows = env.conn.execute(
        "SELECT count(*) FROM corporate_actions WHERE source='snapshot_implied'").fetchone()[0]
    assert rows == 1


def test_exright_writes_positive_implied_factor(env):
    """`implied_factor` 留 NULL 会让 core.adjust 的 `_implied` 抛 ValueError，整条复权链崩。"""
    # fixture 里池子的 T−1 raw 收盘是 10.04；除权后昨收 5.675 → 因子 10.04/5.675
    q = quote("sh600519", 5.68, _vol(1.0), prev_close=5.675)
    env.tick.on_pool({"sh600519": q}, T0)
    factor = env.conn.execute(
        "SELECT implied_factor FROM corporate_actions WHERE source='snapshot_implied'").fetchone()[0]
    assert factor is not None and factor > 0
    # **不是 现价/昨收**（那只是当日涨跌幅 1.0009），而是 前收/昨收 = 2.0
    assert factor == pytest.approx(10.04 / 5.675)

    from core.adjust import _implied                                     # noqa: SLF001
    from core.datasource.westock import CorpAction

    assert _implied(CorpAction("sh600519", 20261030, 0, 0, 0, "snapshot_implied", factor)) > 0


def test_sector_tick_emits_sector_move(env):
    ring_old = T0.replace(minute=20)
    row_old = SectorRow("new_001", "银行", 100.0, 100.0, 0.0, 1e6, ring_old, members=12)
    env.tick.on_sector([row_old], ring_old)                     # 环里先放 100
    row_new = SectorRow("new_001", "银行", 103.0, 100.0, 3.0, 1e6, T0, members=12)
    env.tick.on_sector([row_new], T0)                           # 10 分钟后 +3%
    kinds = [r[0] for r in env.conn.execute("SELECT kind FROM event_log")]
    assert "sector_move" in kinds
    assert env.conn.execute("SELECT scope FROM event_log WHERE kind='sector_move'").fetchone()[0] == "sector"


def test_index_move_uses_shared_ring(env):
    from datetime import timedelta

    first = T0 - timedelta(seconds=310)
    env.tick.on_pool({"sh000001": quote("sh000001", 3000.0, 1e6)}, first)
    env.tick.on_pool({"sh000001": quote("sh000001", 3040.0, 1e6)}, T0)
    row = env.conn.execute("SELECT market, scope FROM event_log WHERE kind='index_move'").fetchone()
    assert row is not None and row[0] == "index" and row[1] == "market"


def test_tick_wraps_dispatcher_under_lock(env):
    env.tick.tick()
    assert env.disp.tick_calls == 1


def test_tick_resets_long_lived_state_on_new_day(env):
    """跨日 tick：freezer 与每股状态必须整体重置，昨天的冻结值不能带到今天。"""
    code = "sh600519"
    env.tick._freezer_of().ratio(code, "14:57", 9.99)          # 昨天 14:57 冻结成 9.99
    assert env.tick._freezer_of()._frozen[code] == 9.99
    env.tick._state(code).primed = True

    env.clock._now = datetime(2026, 11, 2, 9, 30, tzinfo=SH)    # 下周一开盘
    env.tick.tick()

    assert env.tick._freezer_of()._frozen == {}
    assert code not in env.tick._states
    assert env.tick._day == 20261102


def test_tick_does_not_reset_within_same_day(env):
    code = "sh600519"
    env.tick.tick()
    env.tick._freezer_of().ratio(code, "14:57", 9.99)
    env.tick.tick()
    assert env.tick._freezer_of()._frozen.get(code) == 9.99


def _sector_move(env, now):
    from datetime import timedelta

    old = now - timedelta(minutes=6)
    env.tick.on_sector([SectorRow("new_001", "银行", 100.0, 100.0, 0.0, 1e6, old, members=12)], old)
    env.tick.on_sector([SectorRow("new_001", "银行", 103.0, 100.0, 3.0, 1e6, now, members=12)], now)


def test_sector_event_does_not_rearm_stock(env):
    """板块事件空转（无个股检测）时，不能把已触发的个股 breakout 重新武装。"""
    env.tick.on_pool({"sh600519": quote("sh600519", 10.05, _vol(1.0))}, T0)      # priming
    env.tick.on_pool({"sh600519": quote("sh600519", 10.22, _vol(3.2))}, T0)      # 突破触发
    key = ("breakout", "sh600519")
    assert env.det._armed[key] is False

    _sector_move(env, T0)

    assert env.det._armed[key] is False, "板块事件把个股重新武装了 —— 回箱后再突破会漏发"


def test_empty_pool_tick_still_rearms_detector(env):
    """`dets` 为空（回箱 / 本轮无任何检测）时也必须走 detector.ingest 重新武装。

    旧实现 `_ingest` 一开头 `if not dets: return`，于是「突破 → 回箱 → 再突破」
    第二次永远不会发。

    注意重新武装要过 `price_rearm_min` 冷却：这里把时钟推过冷却期，
    否则测出来的是「抖动也算重新武装」——那正是 300052 八分钟 19 条信号的成因。
    """
    env.tick.on_pool({"sh600519": quote("sh600519", 10.05, _vol(1.0))}, T0)      # priming
    env.tick.on_pool({"sh600519": quote("sh600519", 10.22, _vol(3.2))}, T0)      # 突破触发
    assert env.conn.execute("SELECT count(*) FROM event_log WHERE kind='breakout'").fetchone()[0] == 1
    env.tick.on_pool({}, T0)                                                    # 本轮 dets == []
    # 重新武装只在「条件缺席」的 tick 上发生，且要过冷却期：
    # 先推进时钟，再发一次空 tick，然后才可能再突破。
    from datetime import timedelta
    env.tick.detector.clock = type(env.clock)(T0 + timedelta(seconds=1801))
    env.tick.on_pool({}, T0)                                                    # 过冷却期的空 tick
    env.tick.on_pool({"sh600519": quote("sh600519", 10.22, _vol(3.2))}, T0)      # 再突破
    assert env.conn.execute("SELECT count(*) FROM event_log WHERE kind='breakout'").fetchone()[0] == 2


def test_capture_auction_writes_volume_in_shares(env):
    q = quote("sh600519", 10.5, volume=123400.0)               # 股
    env.tick.capture_auction({"sh600519": q}, {"sh600519": [1e5] * 20}, lambda c: "1.0")
    row = env.conn.execute("SELECT auction_vol, auction_amt FROM auction_daily").fetchone()
    assert row[0] == 123400.0
    assert env.disp.briefs                                        # 简报拿到竞价检测
    assert env.conn.execute("SELECT count(*) FROM event_log WHERE kind='auction_anomaly'").fetchone()[0] == 1


def test_watch_rule_flagged_watch(env):
    env.conn.execute("INSERT INTO daily_bars(code,date,open,high,low,close,volume,amount,source) "
                     "VALUES ('sh600519',20261029,10.5,11,10.4,10.5,1e6,1e7,'backfill')")
    env.conn.execute("INSERT INTO watch_stock(code, added_at) VALUES ('sh600519','x')")
    env.conn.commit()
    env.tick.on_pool({"sh600519": quote("sh600519", 10.6, _vol(1.0), prev_close=10.5,
                                       volume_ratio=2.5)}, T0)
    kinds = {r[0] for r in env.conn.execute("SELECT kind FROM event_log")}
    assert "watch_rule" in kinds


def test_tick_and_pool_handle_are_serialized_by_the_same_lock(env):
    """去掉锁必须变红：持锁时另一线程不得进入 tick（同一 conn 的事务不能交错）。"""
    import threading
    import time

    entered = threading.Event()
    done = threading.Event()
    tick_obj = env.tick

    def hold():
        with tick_obj.lock:
            entered.set()
            time.sleep(0.3)

    t = threading.Thread(target=hold)
    t.start()
    assert entered.wait(2)
    started = time.monotonic()
    tick_obj.tick()                 # 必须在 hold 释放后才能返回
    elapsed = time.monotonic() - started
    t.join()
    assert elapsed >= 0.2, "tick 没有等待共享锁，说明它没进锁"


def test_implied_factor_uses_prev_close_not_intraday_price(env):
    """因子口径是「前收/除权后昨收」，不是「现价/昨收」（后者只是当日涨跌幅，差一个数量级）。"""
    q = quote("sh600519", 6.02, _vol(1.0), prev_close=5.675)     # 盘中还在涨
    env.tick.on_pool({"sh600519": q}, T0)
    factor = env.conn.execute(
        "SELECT implied_factor FROM corporate_actions WHERE source='snapshot_implied'").fetchone()[0]
    assert factor == pytest.approx(10.04 / 5.675)                # 与现价 6.02 无关
    assert factor != pytest.approx(6.02 / 5.675)


def test_auction_gap_only_reports_for_pool_stocks(env):
    """**池外的票不报高低开**：全市场每天几百只涨跌 2%，那是常态不是异动。

    实测 11-17 一天 `auction_anomaly` 860 条，全是单纯高低开、没有一条是量能异常、
    也没有一条属于自选——把 salience 分布整个压死（97.6% 被抑制），阈值怎么调都不行。
    """
    q = quote("sh600519", 10.5, volume=123400.0)      # 相对 prev_close 明显高开
    # 池外（"outside"）：只写 auction_daily / 简报，不产生 auction_anomaly
    env.tick.capture_auction({"sh600519": q}, {"sh600519": [1e5] * 20},
                             lambda c: "outside")
    assert env.conn.execute(
        "SELECT count(*) FROM event_log WHERE kind='auction_anomaly'").fetchone()[0] == 0, (
        "池外的票高低开被记成了竞价异动：这会把全市场噪声灌进事件流")
    assert env.conn.execute("SELECT count(*) FROM auction_daily").fetchone()[0] == 1, (
        "池外票仍要写 auction_daily（竞价均量基线靠它攒）")
