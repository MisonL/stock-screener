"""复权对拍脚本（只校验，不参与生产路径）。"""
import pytest

from core.adjust import verify_adj
from core.models import Bar
from jobs.verify_adj import fetch_qfq
from tests.helpers import make_http, seed_daily_bars


def _qfq(closes, start=20250102):
    from tests.helpers import _trading_days

    days = _trading_days(start, len(closes))
    return [Bar("sh600519", d, c, c, c, c, 1000, 10000, "tencent") for d, c in zip(days, closes)]


def test_fetch_qfq_reads_qfqday_branch():
    import json

    import httpx

    def handler(request):
        body = {"code": 0, "msg": "", "data": {"sh600519": {"qfqday": [
            ["2026-10-26", "50.0", "51.0", "52.0", "49.0", "1000", {}, "0", "1000", "0", "0"],
        ]}}}
        return httpx.Response(200, content=json.dumps(body).encode())

    bars = fetch_qfq(make_http(transport=httpx.MockTransport(handler)), "sh600519")
    assert bars[0].date == 20261026 and bars[0].close == 51.0 and bars[0].volume == 100000.0


def test_verify_adj_matches_identical_series(db):
    # 无除权事件时本地 adj == raw，qfq 也相同 → 偏差应接近 0
    seed_daily_bars(db, "sh600519", 10, close=10.0)
    from core.adjust import rebuild_adj

    rebuild_adj(db, "sh600519")
    diffs = verify_adj(db, "sh600519", _qfq([10.0] * 10))
    assert diffs and all(abs(d) < 1e-9 for _, d in diffs)


def test_verify_adj_reports_mismatch(db):
    seed_daily_bars(db, "sh600519", 10, close=10.0)
    from core.adjust import rebuild_adj

    rebuild_adj(db, "sh600519")
    qfq = _qfq([10.0] * 9 + [11.0])          # 上游说最后一天涨 10%，本地是平的
    diffs = verify_adj(db, "sh600519", qfq)
    assert max(abs(d) for _, d in diffs) == pytest.approx(0.10, abs=1e-6)


def test_verify_adj_without_qfq_is_empty(db):
    seed_daily_bars(db, "sh600519", 3)
    assert verify_adj(db, "sh600519") == []
