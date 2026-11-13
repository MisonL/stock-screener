"""Task 8：新浪数据源（校验源、股为单位、全市场代码列表）。"""
import pytest


def test_sina_volume_unit_is_shares(sina_fixture):
    b = sina_fixture.fetch_kline_daily("sh600519", 1)[0]
    assert b.volume == 3866914 and b.amount == 0.0 and b.source == "sina"


def test_sina_snapshot_volume_is_shares(sina_fixture):     # 测试 F-27：降级的受害路径是快照
    q = sina_fixture.fetch_snapshot(["sh600519"])["sh600519"]
    assert q.volume == 3728383                               # 原始串就是股，不走 ×100


def test_referer_sent(sina_fixture):
    sina_fixture.fetch_snapshot(["sh600519"])
    assert sina_fixture.http.calls[-1].headers.get("Referer") == "https://finance.sina.com.cn"


def test_universe_pagination_pages_until_empty(sina_fixture):
    codes = sina_fixture.fetch_universe()                    # fixture：第 1 页 2 只、第 2 页空
    assert codes == [("sh600519", "贵州茅台"), ("sz000001", "平安银行")]
    assert [c.url.split("page=")[1][0] for c in sina_fixture.http.calls[-2:]] == ["1", "2"]


def test_universe_spacing_between_pages(sina_fixture):
    sina_fixture.fetch_universe()
    gaps = sina_fixture.http.gaps("sina")                    # 相邻两次 sina 请求的时间差
    assert all(g >= 0.3 for g in gaps)


def test_ohlc_field_order_differs_from_tencent(sina_fixture, tencent_with_fixture):
    # 覆盖 F5：腾讯是 开/收/高/低，新浪是 开/高/低/收
    sina = sina_fixture.fetch_kline_daily("sh600519", 1)[0]
    tc = tencent_with_fixture.fetch_kline_daily("sh600519", count=1)[-1]
    assert (sina.open, sina.high, sina.low, sina.close) == (1326.00, 1352.00, 1320.10, 1349.98)
    assert (tc.open, tc.close) == (1325.40, 1349.98)         # 两个解析器不能共用一个字段顺序


def test_sina_kline_empty_is_error(sina_fixture, monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(200, content=b"[]")

    sina_fixture.http.client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    import pytest

    from core.http import UpstreamError

    with pytest.raises(UpstreamError):
        sina_fixture.fetch_kline_daily("sh600519", 1)


# ---- Task 12：板块成分股与板块快照（新浪行业/概念） ----


def test_sector_members_paginate(sina_sector):
    codes = sina_sector.fetch_sector_members("new_blhy")
    assert codes == ["sh600519", "sz000001"]                  # 第 2 页空数组即停
    assert "node=new_blhy" in sina_sector.http.calls[0].url
    assert [c.url.split("page=")[1][0] for c in sina_sector.http.calls[-2:]] == ["1", "2"]


def test_sector_members_is_serialised(sina_sector):
    sina_sector.fetch_sector_members("new_blhy")
    assert all(g >= 0.0 for g in sina_sector.http.gaps("sina"))   # 只断言走同一个 sina 限速器


def test_sector_list_from_industry_page(sina_sector):
    assert sina_sector.fetch_sector_list() == [("new_blhy", "玻璃行业")]


def test_sector_snapshot_two_requests(sina_sector):
    rows = sina_sector.fetch_sector_snapshot()                # 行业 1 次 + 概念 1 次
    assert len(sina_sector.http.calls) == 2
    assert {c.headers.get("Referer") for c in sina_sector.http.calls} == {"https://finance.sina.com.cn"}

    industry, concept = rows
    assert (industry.code, industry.name) == ("new_blhy", "玻璃行业")
    assert industry.price == 19.89 and industry.prev_close == 19.77     # 平均价 − 涨跌额
    assert industry.chg == 0.61 and industry.volume == 12345678
    assert (concept.code, concept.name) == ("gn_xny", "新能源")
    assert concept.prev_close == pytest.approx(12.44)


def test_sector_snapshot_rows_are_sector_rows(sina_sector):
    from core.models import SectorRow

    assert all(isinstance(row, SectorRow) for row in sina_sector.fetch_sector_snapshot())
