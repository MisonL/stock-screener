"""收盘简报必须说清"今天是什么日子"。

2026-11-27：涨停 250→306 的强势日，收盘统计只写「推送 26 条 ｜ 压制 0 条 ｜ 信号 4 条」——
**对当天行情只字未提**，而且"压制 0 条"是错的（实际因通道上限丢了 31 条，
那些根本不写 push_log）。用户最想知道的恰恰是这两件事。
"""

import json
from types import SimpleNamespace

import pytest

from notify.templates import CloseStats, render_close_digest


class _Cfg:
    notify = SimpleNamespace(public_host="example.com")


def _stats(**over):
    base = dict(pushed=26, suppressed=0, signals_n=4, by_kind={"breakdown": 3},
                by_level={"P1": 4}, pool_sizes={"cn_main": 25})
    base.update(over)
    return CloseStats(**base)


def test_breadth_appears_when_available():
    msg = render_close_digest(_stats(limit_up=445, limit_down=2, up_n=5395, down_n=812), _Cfg())
    assert "涨停 445" in msg.body
    assert "跌停 2" in msg.body
    assert "上涨 5395 / 下跌 812" in msg.body
    # 行情必须在**第一行**：用户先要知道今天什么日子，不是推送了几条
    assert msg.body.splitlines()[0].startswith("涨停")


def test_breadth_is_omitted_when_missing():
    """取不到快照时**不能凭空写"涨停 0"**——那比不写更误导。"""
    msg = render_close_digest(_stats(), _Cfg())
    assert "涨停" not in msg.body


def test_dropped_notes_are_reported():
    msg = render_close_digest(_stats(dropped=31), _Cfg())
    assert "丢弃 31 条" in msg.body


def test_no_dropped_line_when_zero():
    msg = render_close_digest(_stats(dropped=0), _Cfg())
    assert "丢弃" not in msg.body


def test_old_stats_still_render():
    """新增字段都有默认值：老调用方（只传六个参数）不能炸。"""
    msg = render_close_digest(_stats(), _Cfg())
    assert "推送 26 条" in msg.body


# ---- 统计来源 ---------------------------------------------------------

@pytest.fixture()
def disp(tmp_path):
    from core.clock import FixedClock
    from core.db import connect, migrate
    from notify.dispatcher import Dispatcher
    from tests.helpers import SH
    from datetime import datetime

    conn = connect(tmp_path / "d.db")
    migrate(conn)
    return SimpleNamespace(conn=conn, clock=FixedClock(datetime(2026, 11, 27, 15, 12, tzinfo=SH)),
                           conn_factory=None)


def test_breadth_is_computed_from_the_snapshot(disp):
    from notify.dispatcher import Dispatcher
    from core.config import load_config
    from pathlib import Path

    rows = [{"code": "a", "price": 11.0, "prev_close": 10.0,
             "up": 11.0, "down": 9.0},                      # 涨停
            {"code": "b", "price": 9.0, "prev_close": 10.0,
             "up": 11.0, "down": 9.0},                      # 跌停
            {"code": "c", "price": 10.5, "prev_close": 10.0,
             "up": 11.0, "down": 9.0}]                      # 上涨
    with disp.conn:
        disp.conn.execute("INSERT INTO snapshot_cache(scope, as_of, payload) VALUES (?,?,?)",
                          ("market", "2026-11-27T15:05:00+08:00",
                           json.dumps({"rows": rows})))
    d = Dispatcher(disp.conn, None, load_config(Path("config")), disp.clock)
    got = d._close_breadth()
    # a 与 c 都是上涨（a 还涨停），b 跌停 → up=2 down=1
    assert got == {"up": 2, "down": 1, "limit_up": 1, "limit_down": 1}


def test_breadth_missing_snapshot_returns_empty(disp):
    from notify.dispatcher import Dispatcher
    from core.config import load_config
    from pathlib import Path

    d = Dispatcher(disp.conn, None, load_config(Path("config")), disp.clock)
    assert d._close_breadth() == {}


def test_dropped_counter_sums_the_day(disp):
    from notify.dispatcher import Dispatcher
    from core.config import load_config
    from pathlib import Path

    with disp.conn:
        disp.conn.execute("INSERT INTO job_state(name, value) VALUES ('push.dropped.20261127.通道 B 日上限', '20')")
        disp.conn.execute("INSERT INTO job_state(name, value) VALUES ('push.dropped.20261127.通道 B 间隔 45s', '11')")
        disp.conn.execute("INSERT INTO job_state(name, value) VALUES ('push.dropped.20261126.通道 B 日上限', '99')")
    d = Dispatcher(disp.conn, None, load_config(Path("config")), disp.clock)
    assert d._dropped_today() == 31, "跨日不该混进来，两条也要相加"
