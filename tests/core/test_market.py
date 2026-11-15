"""市场判别、涨跌停价与一字板判定。"""
import pytest

from core.market import classify_market, limit_hit, limit_prices, oneword


@pytest.mark.parametrize("code,m", [
    ("sh600519", "cn_main"), ("sz002594", "cn_main"), ("sz300750", "cn_gem"), ("sh688981", "cn_star"),
    ("bj920002", "cn_bse"), ("bj430047", "cn_bse"), ("sh900901", None), ("sz200002", None), ("bj400001", None),
    ("sh000001", "index"), ("sz399006", "index"), ("hk00700", "hk"), ("usAAPL.OQ", "us"), ("pt01801081", "sector")])
def test_classify(code, m):
    assert classify_market(code) == m


def test_gem_and_otc_reserved():   # 第一期不开港美股，但判定分支要预留（§4.4.1）
    assert classify_market("hk08001") == "hk"      # 港股 GEM：8 开头五位，子分类预留，暂不阻断
    assert classify_market("usXYZ.PK") == "us"     # 粉单/OTC 后缀，子分类预留


def test_classify_bse_legacy_segments():
    # 920 是主线，但 43/83/87/88 的存量代码仍会出现在清单与历史 bar 里（§4.4.1）
    assert [classify_market(c) for c in ("bj830799", "bj871981", "bj880001", "bj920002")] == ["cn_bse"] * 4


def test_classify_unknown_and_bare_code6():
    # 六位裸代码没有市场信息；Task 23 的 limit_prices 依赖这个返回 None
    assert classify_market("600519") is None
    assert classify_market("") is None


def test_main_st_10pct_after_rule_change():
    assert limit_prices(4.05, "cn_main", "ST", 20261028) == (4.46, 3.65)     # ST金花
    assert limit_prices(19.50, "cn_main", "ST", 20261028) == (21.45, 17.55)  # ST人福
    assert limit_prices(6.67, "cn_main", "*ST", 20261028) == (7.34, 6.00)    # *ST九鼎


@pytest.mark.parametrize("prev,mkt,up,down", [
    (10.25, "cn_main", 11.28, 9.23),   # 10.25×1.1 = 11.275 → ROUND_HALF_UP 11.28（浮点 round() 会得 11.27）
    (3.35, "cn_main", 3.69, 3.02),     # 3.35×1.1 = 3.685 → 3.69
    (1.15, "cn_gem", 1.38, 0.92)])     # 1.15×1.2 = 1.38；跌停 1.15×0.8 = 0.92
def test_xx5_half_up_cases(prev, mkt, up, down):   # 测试 F-03：必须与浮点 round() 区分
    assert limit_prices(prev, mkt, "normal", 20261028) == (up, down)


def test_main_st_5pct_before_rule_change():
    assert limit_prices(4.05, "cn_main", "ST", 20260703) == (4.25, 3.85)


def test_gem_and_no_limit_markets():
    assert limit_prices(10.00, "cn_gem", "normal", 20261028) == (12.00, 8.00)
    assert limit_prices(300.0, "hk", "normal", 20261028) is None


def test_star_and_bse_rates():
    assert limit_prices(10.00, "cn_star", "normal", 20261028) == (12.00, 8.00)
    assert limit_prices(10.00, "cn_bse", "normal", 20261028) == (13.00, 7.00)
    assert limit_prices(10.00, "index", "normal", 20261028) is None


def test_oneword():
    assert oneword(9.00, 9.00, "down") == "down" and oneword(9.10, 9.00, "down") is None


def test_limit_hit_tolerance():
    assert limit_hit(11.28, 11.28, 9.23) == "up"
    assert limit_hit(11.275, 11.28, 9.23) == "up"     # 容差 1 分
    assert limit_hit(9.23, 11.28, 9.23) == "down"
    assert limit_hit(10.50, 11.28, 9.23) is None
    assert oneword(9.00, 9.00, None) is None          # 没触板就不是一字板
