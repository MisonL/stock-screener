"""第二部分：salience 与沿触发事件检测器（`detector.py`）。"""
import json
from datetime import datetime

import pytest

from core.intraday.detector import EventDetector, level_for
from core.intraday.rules import Detection
from core.intraday.salience import salience

AS_OF = 20261030


def mk(kind, code="sh600519", watch=False, **payload):
    return Detection(kind, code, payload, watch=watch)


# ---- salience ------------------------------------------------------------

def test_watch_salience(CFG):
    assert salience("watch_rule", "stock", 1.3, p=CFG.strategy.attention) == pytest.approx(0.585)


def test_salience_breadth_and_focus(CFG):
    a = CFG.strategy.attention
    assert salience("index_move", "market", 1.0, p=a) == pytest.approx(0.70 * 1.6)
    assert salience("sector_move", "sector", 1.0, merged_n=3, p=a) == pytest.approx(0.50 * 1.3)
    assert salience("sector_move", "sector", 1.0, merged_n=2, p=a) == pytest.approx(0.50)
    assert salience("breakout", "stock", 0.5, p=a) == pytest.approx(0.20)


def test_data_anomaly_never_escalates(CFG):
    assert salience("data_anomaly", "stock", 1.0, p=CFG.strategy.attention) == 0.0


# ---- level_for -----------------------------------------------------------

def test_level_for_extended_beats_score(CFG):
    d = Detection("breakout", "sh600000", {"score": 92, "box_vol_multiple": 3.1}, extended=True)
    assert level_for(d, CFG) == "P2"


def test_level_for_priority_table(CFG):
    def mk2(kind, score=70, bvm=1.0, ext=False, panic=False):
        return Detection(kind, "sh600000", {"score": score, "box_vol_multiple": bvm},
                         extended=ext, panic=panic)

    assert level_for(mk2("breakout", score=92, bvm=3.1), CFG) == "P0"
    assert level_for(mk2("breakout", score=70), CFG) == "P1"
    assert level_for(mk2("breakdown"), CFG) == "P0"
    assert level_for(mk2("breakdown", ext=True), CFG) == "P2"      # extended 优先级最高
    assert level_for(mk2("near_breakout"), CFG) == "P2"
    assert level_for(mk2("data_anomaly"), CFG) == "P2"


# ---- detector ------------------------------------------------------------

@pytest.fixture
def det(tmp_db, CFG):
    d = EventDetector(tmp_db, _clock(), CFG)
    d.seed_today()
    return d


def _clock(hm=(10, 0)):
    from core.clock import FixedClock

    return FixedClock(datetime(2026, 10, 30, hm[0], hm[1]))


def _focus_of(code):
    return "1.0"


def _rows(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_edge_trigger_fires_once(det):
    for _ in range(3):
        det.ingest([mk("breakout", score=90, box_vol_multiple=3.0)], _focus_of)
    assert _rows(det.conn, "event_log") == 1


def test_rearm_after_falling_back(det):
    """跌破再突破要能**再次报警**，但要过冷却期（`price_rearm_min`）。

    无条件「消失一个 tick 就武装」会让上沿附近的抖动反复触发：
    实测 300052 在 8 分钟出 19 条 breakout。冷却期内不重复、过后仍会报，
    两个方向都要保住。
    """
    det.ingest([mk("breakout", score=90, box_vol_multiple=3.0)], _focus_of)
    evs = det.ingest([mk("breakout", score=90, box_vol_multiple=3.0)], _focus_of)
    assert evs == []
    det.ingest([mk("near_breakout")], _focus_of)          # 价格回到上沿以下
    evs = det.ingest([mk("breakout", score=90, box_vol_multiple=3.0)], _focus_of)
    assert evs == [], "冷却期内不该重复触发"

    # 过冷却期后仍要能报出第 2 次
    det.clock = _clock((10, 45))                          # 距首次触发 45 分钟
    det.ingest([mk("near_breakout")], _focus_of)
    evs = det.ingest([mk("breakout", score=90, box_vol_multiple=3.0)], _focus_of)
    assert [e.seq for e in evs] == [2]


def test_restart_does_not_duplicate(tmp_db, CFG):        # Review Focus #4
    d1 = EventDetector(tmp_db, _clock(), CFG)
    d1.seed_today()
    d1.ingest([mk("breakout", score=90, box_vol_multiple=3.0)], _focus_of)
    d2 = EventDetector(tmp_db, _clock(), CFG)
    d2.seed_today()
    assert d2.ingest([mk("breakout", score=90, box_vol_multiple=3.0)], _focus_of) == []
    assert _rows(tmp_db, "event_log") == 1


def test_channel_a_writes_signal_with_same_seq(det):
    evs = det.ingest([mk("breakout", score=90, box_vol_multiple=3.0)], _focus_of)
    e = evs[0]
    assert e.salience == pytest.approx(0.40)
    sig = det.conn.execute("SELECT * FROM signals WHERE event_id=?", (e.event_id,)).fetchone()
    ev = det.conn.execute("SELECT * FROM event_log WHERE id=?", (e.event_id,)).fetchone()
    assert sig["seq"] == e.seq == int(ev["dedupe_key"].rsplit(":", 1)[1])
    assert sig["level"] == "P0" and sig["market"] == "cn_main"
    assert json.loads(sig["payload"])["box_vol_multiple"] == 3.0
    assert ev["agent_status"] == "new"
    assert ev["market"] == "cn_main" and ev["scope"] == "stock"


def test_low_salience_is_suppressed(det):
    evs = det.ingest([mk("near_breakout")], _focus_of)
    assert evs[0].salience == pytest.approx(0.25)
    row = det.conn.execute("SELECT agent_status FROM event_log WHERE id=?", (evs[0].event_id,)).fetchone()
    assert row["agent_status"] == "suppressed"
    assert _rows(det.conn, "signals") == 0            # 通道 A 之外不写 signals


def test_payload_carries_detection_flags(det):
    d = Detection("breakout", "sh600519", {"score": 90, "box_vol_multiple": 3.0},
                  extended=True, sealed=True)
    evs = det.ingest([d], _focus_of)
    payload = json.loads(det.conn.execute(
        "SELECT payload FROM event_log WHERE id=?", (evs[0].event_id,)).fetchone()["payload"])
    assert payload["extended"] is True and payload["sealed"] is True and payload["watch"] is False


def test_scope_and_market_derived_per_detection(det):
    dets = [mk("breakout", code="sh600519", score=90, box_vol_multiple=3.0),
            mk("index_move", code="sh000001"), mk("sector_move", code="new_001")]
    evs = det.ingest(dets, _focus_of)
    got = {(e.detection.kind, e.detection.code): (
        det.conn.execute("SELECT market, scope FROM event_log WHERE id=?", (e.event_id,)).fetchone()["market"],
        det.conn.execute("SELECT market, scope FROM event_log WHERE id=?", (e.event_id,)).fetchone()["scope"],
    ) for e in evs}
    assert got[("breakout", "sh600519")] == ("cn_main", "stock")
    assert got[("index_move", "sh000001")] == ("index", "market")
    assert got[("sector_move", "new_001")] == ("sector", "sector")


def test_data_anomaly_daily_once(det):
    det.ingest([mk("data_anomaly", code="sh600519", reason="halt")], _focus_of)
    assert det.ingest([mk("data_anomaly", code="sh600519", reason="halt")], _focus_of) == []
    evs = det.ingest([mk("data_anomaly", code="sh600519", reason="exright")], _focus_of)
    assert len(evs) == 1                       # 不同 reason 各自每日一次


def test_watch_flag_raises_focus(det):
    evs = det.ingest([mk("watch_rule", code="sh600519", watch=True)], _focus_of)
    assert evs[0].salience == pytest.approx(0.45 * 1.3)


def test_restart_restores_cooldown(tmp_db, CFG):          # Review Focus #4 的另一半
    from core.clock import FixedClock
    from datetime import datetime, timedelta

    c1 = EventDetector(tmp_db, _clock(), CFG)
    c1.seed_today()
    c1.ingest([mk("index_move", code="sh000001")], _focus_of)
    c2 = EventDetector(tmp_db, _clock(), CFG)
    c2.seed_today()
    assert c2.ingest([mk("index_move", code="sh000001")], _focus_of) == []   # 冷却期内
    later = EventDetector(tmp_db, _clock((11, 0)), CFG)   # 1 小时后：冷却已过
    later.seed_today()
    evs = later.ingest([mk("index_move", code="sh000001")], _focus_of)
    assert len(evs) == 1 and evs[0].seq == 2


def test_salience_breadth_from_config():
    """用**非默认**的 breadth 值：硬编码 0.6 的实现会在这里失败。"""
    from types import SimpleNamespace

    from core.intraday.salience import salience as sal

    a = SimpleNamespace(base={"index_move": 0.7, "sector_move": 0.5, "breakout": 0.4},
                        breadth={"market": 0.25, "sector_merged_3": 0.8, "stock": 0.1},
                        focus={"market": 1.0, "sector": 1.0, "pool": 1.0, "watch": 1.3, "outside": 0.5})
    assert sal("index_move", "market", 1.0, p=a) == pytest.approx(0.7 * 1.25)
    assert sal("sector_move", "sector", 1.0, merged_n=3, p=a) == pytest.approx(0.5 * 1.8)
    assert sal("breakout", "stock", 1.0, p=a) == pytest.approx(0.4 * 1.1)


def test_price_kind_has_cooldown_not_just_rearm(det):
    """价格类信号要**冷却期**，不能「消失一个 tick 就重新触发」。

    真实形态：一只票一直停在上沿之上，量能条件偶尔不满足 → `present`
    缺它一个 tick → `_armed` 重置 → 下次满足又发。8 分钟产生 19 条 breakout，
    还挤进同一批推送（`batch-804` 里同一只重复 8 次，用户看到「11 只同向异动」
    其实只有 4 只）。
    """
    fires: list = []
    tick = lambda ds: fires.extend(det.ingest(ds, focus_of=_focus_of))   # noqa: E731
    d = Detection("breakout", "sz300052", {"score": 80, "box_vol_multiple": 3.0})

    tick([d])
    assert len(fires) == 1
    tick([])                                   # 条件消失一个 tick
    tick([d])                                  # 立刻又满足
    assert len(fires) == 1, f"冷却期内重复触发（共 {len(fires)} 条）"


def test_price_kind_cooldown_survives_restart(tmp_db, CFG):
    """价格类信号的冷却基准必须从 `event_log.ts` 恢复，重启不能清零。

    `seed_today()` 原本只给 `index_move` / `sector_move` 恢复 `_last_fire`，
    价格类（breakout/breakdown/near_breakout/stall）没有——重启一次冷却就归零，
    重启后第一次满足条件立刻再报一条。实测 11-11 当天重启 5 次，
    每两次重启之间同一只票都能重报。注释里写着「重启后从 event_log.ts 恢复，
    否则重启一次冷却清零」，但代码只对两种 kind 生效。
    """
    t = datetime(2026, 10, 30, 10, 0)
    d1 = EventDetector(tmp_db, _clock((10, 0)), CFG)
    d1.seed_today()
    evs = d1.ingest([mk("breakout", score=90, box_vol_multiple=3.0)], _focus_of)
    assert len(evs) == 1

    # 模拟重启：新实例，同一分钟
    d2 = EventDetector(tmp_db, _clock((10, 1)), CFG)
    d2.seed_today()
    d2.ingest([], _focus_of)          # 空 tick：本可以重新武装
    evs = d2.ingest([mk("breakout", score=90, box_vol_multiple=3.0)], _focus_of)
    assert evs == [], "重启后冷却被清零，同一只票又被报了一次"


def test_p1_score_gate_actually_reads_the_config(conn):
    """**`notify.p1_score` 必须真的生效**（v0.2.105）。

    审计发现这个键全仓零读取：旧实现是"非 P0 的突破一律 P1"，60 分这个闸门
    完全不存在。运维以为 P1 有分数门槛，实际不分青红皂白全 P1——
    进而放大 `p1_merge` 的合并压力与"该不该响"的误判。

    接线**不改变默认行为**：默认 0 = 不设闸门，与旧实现一致。
    """
    from core.intraday.detector import Detection, level_for
    from types import SimpleNamespace

    def cfg(p1_score):
        return SimpleNamespace(notify=SimpleNamespace(
            p0=SimpleNamespace(score=80, vol=2.5), p1_score=p1_score))

    # 分数 65 的突破，不是 P0（没到 80）
    det = Detection(kind="breakout", code="sh600519", extended=False,
                    payload={"score": 65, "box_vol_multiple": 1.0})

    assert level_for(det, cfg(0)) == "P1", "默认 0 应当不设闸门（与旧行为一致）"
    assert level_for(det, cfg(60)) == "P1", "65 分过了 60 的闸门，仍该是 P1"
    assert level_for(det, cfg(70)) == "P2", (
        "65 分没过 70 的闸门，应为 P2——闸门没接线")
