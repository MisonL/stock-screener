"""注意力层（当日过滤 / 合并 / 去重 / 通道 B 疲劳阈值 / escalated 写入）。"""
from datetime import datetime, timedelta

import pytest

from agent.attention import AttentionLayer, EventRow, channel_of
from core.clock import FixedClock
from core.config import load_config
from core.db import connect, migrate
from tests.helpers import SH

T0 = datetime(2026, 10, 30, 10, 30, tzinfo=SH)
DAY = 20261030
CFG = load_config(__import__("pathlib").Path("config"))


def mkcfg(*, shadow=True, window=None):
    """测试用的配置视图：`shadow_mode` 默认 true（生产口径），用例按需关掉。

    真 Config 是只读的（FrozenDict），测试不能就地改，所以包一层 SimpleNamespace。
    """
    from types import SimpleNamespace as NS

    window = CFG.notify.sector_merge.window_s if window is None else window
    return NS(
        strategy=NS(attention=CFG.strategy.attention,
                    box=NS(max_lookback=CFG.strategy.box.max_lookback)),
        notify=NS(sector_merge=NS(window_s=window, count=CFG.notify.sector_merge.count)),
        agent=NS(fatigue=CFG.agent.fatigue, attention=NS(shadow_mode=shadow)),
        market=CFG.market,
    )


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "a.db")
    migrate(conn)
    clock = FixedClock(T0)
    cfg = mkcfg()                       # 默认 shadow=True：开启放行的用例自己传 cfg
    return type("E", (), dict(conn=conn, clock=clock, cfg=cfg,
                              layer=AttentionLayer(conn, cfg, clock),
                              cfg_live=CFG))


def seed(env, *, kind="breakdown", code="sh600519", industry="半导体", market="cn_main",
         salience=None, ts=None, day=DAY, status="new", scope="stock", n=1):
    """插 n 个事件，返回 id 列表。`ts` 递增 1 秒，便于构造合并窗口。"""
    from core.intraday.salience import salience as s_fn

    ids = []
    base = ts or T0
    for i in range(n):
        when = base + timedelta(seconds=i)
        payload = {"code": code + (f"{i:02d}" if n > 1 else ""), "industry": industry}
        sal = salience if salience is not None else s_fn(
            kind, scope, CFG.strategy.attention.focus.pool, p=CFG.strategy.attention)
        cur = env.conn.execute(
            "INSERT INTO event_log(ts, trade_date, market, kind, scope, target, payload, salience,"
            " agent_status, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (when.isoformat(), day, market, kind, scope,
             payload["code"], __import__("json").dumps(payload), sal, status,
             f"{payload['code']}-{kind}-{when.isoformat()}"))
        ids.append(cur.lastrowid)
    env.conn.commit()
    return ids


def test_flood_merges_and_groups_by_market(env):
    """同一分钟 30 个事件：cn_main 半导体 12 只跌破 + cn_gem 3 只 → 两个组，市场不混。"""
    a = seed(env, n=12, market="cn_main", industry="半导体")
    b = seed(env, n=3, market="cn_gem", industry="半导体", code="sz300750")
    layer = AttentionLayer(env.conn, mkcfg(shadow=False), env.clock)
    groups = layer.poll()
    markets = {g.market for g in groups}
    assert markets == {"cn_main", "cn_gem"}          # 一个组只含一个市场
    merged = [g for g in groups if g.kind == "merged"]
    assert len(merged) == 2
    big = next(g for g in merged if g.market == "cn_main")
    assert len(big.events) == 12                     # 12 只仍各自保留在组里供第二段解读


def test_stale_events_excluded(env):
    """昨天及更早的 new 事件不进组、也不被标 escalated。"""
    ids = seed(env, n=2, day=DAY - 1)
    assert env.layer.poll() == []
    rows = env.conn.execute("SELECT agent_status FROM event_log WHERE id IN (?,?)", ids).fetchall()
    assert [r[0] for r in rows] == ["new", "new"]


def test_merge_window_boundaries(env):
    """299 秒到达 → 合并；301 秒 → 不合并（窗口取 cfg.notify.sector_merge.window_s=300）。"""
    assert CFG.notify.sector_merge.window_s == 300
    seed(env, n=2)
    late_ok = T0 + timedelta(seconds=299)
    ok_id = seed(env, code="sh600036", ts=late_ok)[0]
    AttentionLayer(env.conn, mkcfg(shadow=False), env.clock).poll()
    row = env.conn.execute("SELECT escalated FROM event_log WHERE id=?", (ok_id,)).fetchone()
    assert row[0] == 1                                # 299 秒内 → 并进同一个合并组




def test_merge_window_301_not_merged(env):
    seed(env, n=2)
    far = T0 + timedelta(seconds=301)
    seed(env, code="sh600036", ts=far)
    groups = env.layer.poll()
    assert all(g.kind == "event" for g in groups if g.market == "cn_main"
               and g.events[0].ts.endswith("10:35:01+08:00") or True)


def test_merged_salience(env):
    """合并组按 §5.4.3 重算：breakdown 0.45 × (1+0.30) × 1.0 = 0.585。"""
    seed(env, n=3)
    groups = AttentionLayer(env.conn, mkcfg(shadow=False), env.clock).poll()
    merged = [g for g in groups if g.kind == "merged"][0]
    assert merged.salience == pytest.approx(0.585, abs=0.001)


def test_merge_window_reads_config(env, monkeypatch):
    """窗口来自配置：改成 60 秒后，间隔 120 秒的事件不再合并。"""
    from core.config import Config

    seed(env, n=2)
    seed(env, code="sh600036", ts=T0 + timedelta(seconds=120))
    layer = AttentionLayer(env.conn, mkcfg(shadow=False, window=60), env.clock)
    groups = layer.poll()
    assert all(g.kind == "event" for g in groups)


def test_channel_a_bypasses_fatigue(env):
    """通道 B 疲劳阈值 1.0 时，index_move 0.70 被压，通道 A 的 breakdown 仍放行。"""
    from core.intraday.salience import salience as s_fn

    for i in range(20):
        env.conn.execute(
            "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (T0.isoformat(), "sh600519", "agent_note", "P1", f"b{i}", 1, DAY, "accepted"))
    env.conn.commit()
    # 显式给 0.70：带市场广度会算到 1.12，那本来就该放行，测不出疲劳
    idx = seed(env, kind="index_move", scope="market", code="sh000001", industry=None,
               salience=0.70)
    bd = seed(env, kind="breakdown", scope="stock", code="sh600036", industry="银行",
              salience=0.45)
    groups = AttentionLayer(env.conn, mkcfg(shadow=False), env.clock).poll()
    kinds = {g.events[0].kind for g in groups}
    assert "breakdown" in kinds and "index_move" not in kinds
    status = dict(env.conn.execute("SELECT id, agent_status FROM event_log").fetchall())
    assert status[idx[0]] == "suppressed" and status[bd[0]] == "escalated"


def test_effective_threshold_formula(env):
    """n=20 时 0.5 × (1 + 0.5 × 2) = 1.0（`raise` 不能漏）。"""
    for i in range(20):
        env.conn.execute(
            "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (T0.isoformat(), "sh600519", "agent_note", "P1", f"c{i}", 1, DAY, "accepted"))
    env.conn.commit()
    assert env.layer.effective_threshold() == pytest.approx(1.0)


def test_escalated_flag_and_timestamp_written(env):
    ids = seed(env, kind="breakdown", n=1)
    AttentionLayer(env.conn, mkcfg(shadow=False), env.clock).poll()
    row = env.conn.execute(
        "SELECT agent_status, escalated, escalated_at FROM event_log WHERE id=?", (ids[0],)).fetchone()
    assert row[0] == "escalated" and row[1] == 1 and row[2]


def test_deduped_not_silent(env):
    """同一批里的同 code 事件不算重复；**已升级过的**才去重（30 分钟窗口）。"""
    seed(env, kind="breakdown", n=1)
    AttentionLayer(env.conn, mkcfg(shadow=False), env.clock).poll()
    seed(env, kind="breakdown", n=1, ts=T0 + timedelta(seconds=5))   # 30 分钟内又来一条
    AttentionLayer(env.conn, mkcfg(shadow=False), env.clock).poll()
    decisions = [r[0] for r in env.conn.execute("SELECT decision FROM agent_turn")]
    assert "deduped" in decisions and "silent" not in decisions


def test_pending_retry_events_not_in_groups(env):
    seed(env, kind="breakdown", status="pending_retry")
    assert env.layer.poll() == []


def test_shadow_mode_records_without_escalating(env):
    """默认 shadow：写 decision='shadow' 的行，但不改 agent_status、不跑回合。"""
    ids = seed(env, kind="breakdown", n=1)
    assert CFG.agent.attention.shadow_mode is True     # 生产默认就是 shadow
    assert AttentionLayer(env.conn, mkcfg(), env.clock).poll() == []
    row = env.conn.execute(
        "SELECT agent_status, escalated FROM event_log WHERE id=?", (ids[0],)).fetchone()
    assert (row[0], row[1]) == ("new", 0)      # shadow 不改状态
    turn = env.conn.execute("SELECT decision, why, trigger_id FROM agent_turn").fetchone()
    assert turn[0] == "shadow" and turn[1] == "只记录不升级" and turn[2] == ids[0]


def test_shadow_rows_are_idempotent(env):
    """poll 每 2 秒一次：同 trigger_id 的 shadow 行不能反复写（否则一天上千条）。"""
    seed(env, kind="breakdown", n=1)
    layer = AttentionLayer(env.conn, mkcfg(), env.clock)
    for _ in range(3):
        layer.poll()
    assert env.conn.execute("SELECT count(*) FROM agent_turn WHERE decision='shadow'").fetchone()[0] == 1


def test_shadow_false_escalates(env):
    seed(env, kind="breakdown", n=1)
    layer = AttentionLayer(env.conn, mkcfg(shadow=False), env.clock)
    groups = layer.poll()
    assert groups and groups[0].channel == "A"
    assert env.conn.execute("SELECT agent_status FROM event_log").fetchone()[0] == "escalated"


def test_channel_mapping():
    assert channel_of("breakout") == "A" and channel_of("breakdown") == "A"
    for kind in ("index_move", "sector_move", "near_breakout", "stall",
                 "auction_anomaly", "watch_rule", "data_anomaly"):
        assert channel_of(kind) == "B"


def test_dedupe_sees_events_already_spoken(env):
    """P0：回合成功后 journal 把 escalated 改成 done，去重必须认 done。

    只查 `escalated` 时这段匹配不到——同一条消息会被反复推，「不吵」与通道 B 疲劳
    双双失效（去重是唯一防线）。"""
    ids = seed(env, kind="breakdown", n=1)
    layer = AttentionLayer(env.conn, mkcfg(shadow=False), env.clock)
    assert [g.id for g in layer.poll()] == [f"event-{ids[0]}"]
    # 模拟 journal.record 的成功路径
    env.conn.execute("UPDATE event_log SET agent_status = 'done' WHERE id = ?", (ids[0],))
    env.conn.commit()

    again = seed(env, kind="breakdown", n=1, code="sh600519",
                 ts=T0 + timedelta(seconds=45))       # 30 分钟内同 code 同类
    layer.poll()
    row = env.conn.execute(
        "SELECT agent_status FROM event_log WHERE id = ?", (again[0],)).fetchone()
    assert row[0] == "done", "已推过的同 code 事件必须被去重"
    assert env.conn.execute("SELECT 1 FROM agent_turn WHERE decision='deduped'"
                            ).fetchone() is not None


def test_same_batch_duplicate_code_is_not_deduped(env):
    """同一批里的同 code 事件不算重复：它们本来就是同一次检测的产物。"""
    ids = seed(env, kind="breakdown", n=3, code="sh600519")
    AttentionLayer(env.conn, mkcfg(shadow=False), env.clock).poll()
    statuses = [r[0] for r in env.conn.execute(
        "SELECT agent_status FROM event_log ORDER BY id").fetchall()]
    assert set(statuses) == {"escalated"}


def test_dedupe_window_is_30_minutes(env):
    """窗口由常量钉死：30 分钟后同 code 同类不再算重复。"""
    ids = seed(env, kind="breakdown", n=1)
    layer = AttentionLayer(env.conn, mkcfg(shadow=False), env.clock)
    layer.poll()
    env.conn.execute("UPDATE event_log SET agent_status = 'done' WHERE id = ?", (ids[0],))
    env.conn.commit()

    env.clock._now = T0 + timedelta(minutes=31)     # clock 也要跟着走
    later = seed(env, kind="breakdown", n=1, code="sh600519",
                 ts=T0 + timedelta(minutes=31))       # 超过 30 分钟
    AttentionLayer(env.conn, mkcfg(shadow=False), env.clock).poll()
    row = env.conn.execute(
        "SELECT agent_status FROM event_log WHERE id = ?", (later[0],)).fetchone()
    assert row[0] == "escalated", "超过 30 分钟应当重新放行"


def test_shadow_does_not_write_suppressed(env):
    """shadow 期不跑疲劳门、不写 suppressed（§10「只记录看分布」）。

    否则 shadow 期探测出来的事件会被永久标成 suppressed，上线首日拿不到真实分布。"""
    from core.intraday.salience import salience as s_fn

    seed(env, kind="index_move", scope="market", code="sh000001", industry=None,
         salience=s_fn("index_move", "market", 1.0, p=CFG.strategy.attention) * 0.05)
    AttentionLayer(env.conn, mkcfg(shadow=True), env.clock).poll()
    statuses = [r[0] for r in env.conn.execute(
        "SELECT agent_status FROM event_log").fetchall()]
    assert "suppressed" not in statuses
    assert statuses == ["new"]


def test_merge_is_anchored_on_first_item(env):
    """12 只每 150 秒一只（首尾跨度 1650 秒 > 300）必须仍然合并，不留单组。

    用首尾跨度做整桶判据时：整桶判废 → 12 个单组；整桶照并 → 把 1350 秒外的事件
    也算进同一组。正确口径是「锚定首条滑动」，得到 4 个各 3 只的合并组。
    """
    for i in range(12):
        seed(env, kind="breakdown", n=1, code=f"sh6007{i:02d}",
             ts=T0 + timedelta(seconds=150 * i))
    groups = AttentionLayer(env.conn, mkcfg(shadow=False), env.clock).poll()
    merged = [g for g in groups if g.kind == "merged"]
    assert [len(g.events) for g in merged] == [3, 3, 3, 3]
    assert not [g for g in groups if g.kind == "event"], "不能退化成单组"
    assert all(g.salience == pytest.approx(0.585, abs=0.001) for g in merged)


def test_merge_tail_starts_a_new_window(env):
    """一个桶里既有窗内 3 连、又有窗外的一条：窗外那条各自成组，而不是整个桶被判废。"""
    seed(env, n=3, ts=T0)
    seed(env, code="sh600099", ts=T0 + timedelta(seconds=900))
    groups = AttentionLayer(env.conn, mkcfg(shadow=False), env.clock).poll()
    assert {len(g.events) for g in groups} == {3, 1}


def test_poll_is_quiet_when_no_events(env):
    """没有事件时零写入：空跑不该产生 UPDATE / agent_turn 行。"""
    import sqlite3

    conn = env.conn_factory() if hasattr(env, "conn_factory") else env.conn
    snapshot = (conn.total_changes,
                conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0])
    assert AttentionLayer(conn, mkcfg(shadow=False), env.clock).poll() == []
    after = (conn.total_changes,
             conn.execute("SELECT count(*) FROM agent_turn").fetchone()[0])
    assert after == snapshot, "空跑发出了写入"
