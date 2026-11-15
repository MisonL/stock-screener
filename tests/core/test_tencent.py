"""腾讯数据源（快照 / 日线）与 Bar 的占位判据。"""
from datetime import datetime

import pytest

from core.datasource.tencent import TencentSource
from core.http import Http, RateLimiter, UpstreamError
from core.models import Bar
from tests.helpers import SH


def test_kline_parses_and_units(tencent_with_fixture):
    bars = tencent_with_fixture.fetch_kline_daily("sh600519", count=3)
    assert [b.date for b in bars] == [20261026, 20261027, 20261028]
    assert bars[0].open == 1325.40 and bars[0].close == 1349.98
    assert bars[0].volume == 41599 * 100 and bars[0].amount == 557428.76 * 10000


def test_kline_high_low_and_code(tencent_with_fixture):
    bar = tencent_with_fixture.fetch_kline_daily("sh600519", count=3)[0]
    assert (bar.high, bar.low) == (1350.00, 1324.79)
    assert bar.code == "sh600519" and bar.source == "tencent"


def test_empty_bars_is_error_not_success(tencent_empty):
    with pytest.raises(UpstreamError):
        tencent_empty.fetch_kline_daily("sh600519")


def test_count_over_2000_refused(tencent_with_fixture):
    with pytest.raises(UpstreamError):
        tencent_with_fixture.fetch_kline_daily("sh600519", count=2001)


def test_kline_url_is_four_segment(tencent_with_fixture):
    tencent_with_fixture.fetch_kline_daily("sh600519", count=3)
    assert "param=sh600519,day,,,3," in tencent_with_fixture.http.calls[0].url


def test_kline_end_uses_four_segment_with_dates(tencent_with_fixture):
    tencent_with_fixture.fetch_kline_daily("sh600519", count=3, end="2026-10-28")
    assert "param=sh600519,day,2026-10-28,2026-10-28,3," in tencent_with_fixture.http.calls[0].url


def test_snapshot_missing_code_does_not_shift(tencent_with_snapshot):   # Review Focus #1
    q = tencent_with_snapshot.fetch_snapshot(["sh600519", "sz999999", "sz000001"])
    assert set(q) == {"sh600519", "sz000001"}
    assert q["sz000001"].name == "平安银行"            # 不因中间缺行而错位
    assert "sz999999" in tencent_with_snapshot.last_missing      # 缺的要留痕，不能静默消失


def test_snapshot_index_0_based(tencent_with_snapshot):
    q = tencent_with_snapshot.fetch_snapshot(["sh600519"])["sh600519"]
    assert q.prev_close == 1349.98 and q.volume_ratio == 0.92 and q.ts.hour == 15


def test_snapshot_units_are_shares_and_yuan(tencent_with_snapshot):
    q = tencent_with_snapshot.fetch_snapshot(["sh600519"])["sh600519"]
    assert q.volume == 3728383                # 37283.83 手 → 股
    assert q.amount == 518732.46 * 10000      # 万元 → 元（与日线第 9 列同口径）
    assert (q.limit_up, q.limit_down) == (1484.98, 1214.98)
    assert q.ts == datetime(2026, 10, 28, 15, 0, tzinfo=SH)


def test_no_fetch_universe_on_tencent(tencent_with_fixture):
    assert not hasattr(tencent_with_fixture, "fetch_universe")    # D12：universe 归 SinaSource


@pytest.mark.parametrize("kw,want", [
    (dict(low=0, high=1, open=1, close=1),                       True),   # ① 结构：low<=0
    (dict(low=2, high=1, open=1, close=1),                       True),   # ① high<low
    (dict(low=1, high=2, open=3, close=1),                       True),   # ① open 越界
    (dict(low=1, high=1, open=1, close=1, volume=0),             True),   # ② 四价相等 + 零量
    (dict(low=1, high=1, open=1, close=1, volume=100, close_is_limit=True), False),  # 一字板：放过
    (dict(low=1, high=1, open=1, close=1, volume=100, date_is_today=True, close_eq_prev=True), True),   # ③ 占位
    (dict(low=1, high=1, open=1, close=1, volume=100, date_is_today=True, close_eq_prev=False, now_is_盘中=True), True),  # 时间门
])
def test_is_placeholder(kw, want):   # 测试 F-28：三重门逐条 + 一字板不被误杀 + 时间门
    today = 20261028
    date = today if kw.get("date_is_today") else 20260101
    bar = Bar("sh600519", date, kw["open"], kw["high"], kw["low"], kw["close"],
              kw.get("volume", 1000), 0.0, "tencent")
    prev = kw["close"] if kw.get("close_eq_prev") else kw["close"] + 1
    now = datetime(2026, 10, 28, 10, 0, tzinfo=SH) if kw.get("now_is_盘中") else None
    assert bar.is_placeholder(today, prev_close=prev, close_time="14:57", now=now) is want


def test_is_placeholder_history_oneword_kept(db):
    # 历史一字跌停（四价相等、volume>0、date 非今天）→ 真 K 线，必须入库
    bar = Bar("sh600519", 20260101, 9.0, 9.0, 9.0, 9.0, 1000, 0.0, "tencent")
    assert bar.is_placeholder(20261028, prev_close=8.9, close_time="14:57") is False


def test_today_bar_allowed_after_close_plus_5min():
    # 15:02 之后组装的当日 bar 才是收盘 bar，之前一律是中间态
    bar = Bar("sh600519", 20261028, 1330.75, 1354.98, 1322.60, 1330.75, 3880500, 0.0, "tencent")
    assert bar.is_placeholder(20261028, 1349.98, "14:57", datetime(2026, 10, 28, 15, 2, tzinfo=SH)) is False
    assert bar.is_placeholder(20261028, 1349.98, "14:57", datetime(2026, 10, 28, 15, 1, tzinfo=SH)) is True


def test_short_kline_row_is_upstream_error(tencent_with_fixture):
    """上游只回日期+收盘的短行必须变成 UpstreamError（IndexError 会绕过重试、打断整轮回填）。"""
    import json

    import httpx

    from core.http import Http, RateLimiter

    def handler(request):
        body = {"code": 0, "msg": "", "data": {"sh600519": {"day": [["2026-10-30", "10"]]}}}
        return httpx.Response(200, content=json.dumps(body).encode())

    src = TencentSource(Http({"tencent": RateLimiter(0.0)}, transport=httpx.MockTransport(handler)))
    with pytest.raises(UpstreamError):
        src.fetch_kline_daily("sh600519", 1)


def test_missing_close_is_upstream_error():
    import json

    import httpx

    from core.http import Http, RateLimiter

    def handler(request):
        body = {"code": 0, "msg": "", "data": {"sh600519": {"day": [
            ["2026-10-30", "10.0", "", "10.5", "9.8", "1000", {}, "0", "1000", "0", "0"]]}}}
        return httpx.Response(200, content=json.dumps(body).encode())

    src = TencentSource(Http({"tencent": RateLimiter(0.0)}, transport=httpx.MockTransport(handler)))
    with pytest.raises(UpstreamError):
        src.fetch_kline_daily("sh600519", 1)


def test_root_array_is_upstream_error():
    import httpx

    src = TencentSource(Http({"tencent": RateLimiter(0.0)},
                             transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"[]"))))
    with pytest.raises(UpstreamError):
        src.fetch_kline_daily("sh600519", 1)
