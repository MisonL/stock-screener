"""市场判别与涨跌停价。

`classify_market` 是过滤器、涨跌停、网页展示、数据源路由四处唯一入口（§4.4.1）：
不要在任何别处再写一套前缀判断。

涨跌停价一律按「板块 + 当日 ST 状态」自己算，不用快照的 `f[47]/f[48]`：
判历史 bar 时快照字段根本不存在，而 `f[47] == -1` 的票（北交所、退市整理股）
本来就要直接被排除（§4.6.6）。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CN_MAIN = "cn_main"
CN_GEM = "cn_gem"
CN_STAR = "cn_star"
CN_BSE = "cn_bse"
HK = "hk"
US = "us"
INDEX = "index"
SECTOR = "sector"

# 2026-07-06 起主板 ST 的涨跌幅由 ±5% 调整为 ±10%（沪深交易所 2026-04-24 修订、07-06 施行）。
_ST_MAIN_WIDENED_FROM = 20260706

_CN_MAIN_SZ_PREFIXES = ("000", "001", "002", "003")
_CN_MAIN_SH_PREFIXES = ("600", "601", "603", "605")
_GEM_PREFIXES = ("300", "301")
_STAR_PREFIXES = ("688", "689")
_BSE_PREFIXES = ("43", "83", "87", "88", "920")

_NO_LIMIT = (HK, US, INDEX, SECTOR)


def classify_market(code: str) -> str | None:
    """`sh600519` → `cn_main`；指数/板块另有独立取值；B 股与老三板返回 `None`。

    传六位裸代码（`600519`）返回 `None`——没有前缀就没有市场信息，不要猜。
    """
    if len(code) < 3:
        return None
    prefix, rest = code[:2].lower(), code[2:]
    if prefix == "pt":
        return SECTOR if rest.isdigit() else None
    if prefix == "hk":
        # 港股 GEM（8 开头五位）本期不开，但子分类位置预留在注释里、暂不阻断。
        return HK if rest.isdigit() else None
    if prefix == "us":
        return US
    if prefix == "sh":
        if rest.startswith("000"):
            return INDEX            # sh000001 / sh000300 / sh000688
        if rest.startswith(_CN_MAIN_SH_PREFIXES):
            return CN_MAIN
        if rest.startswith(_STAR_PREFIXES):
            return CN_STAR
        return None                 # sh900xxx = B 股，不在宇宙内
    if prefix == "sz":
        if rest.startswith("399"):
            return INDEX            # sz399001 / sz399006
        if rest.startswith(_CN_MAIN_SZ_PREFIXES):
            return CN_MAIN
        if rest.startswith(_GEM_PREFIXES):
            return CN_GEM
        return None                 # sz200xxx = B 股
    if prefix == "bj":
        # 920 是新号段主线，但 43/83/87/88 的存量代码仍会出现在清单/历史 bar 里；
        # 400/420（老三板）不在宇宙内。
        return CN_BSE if rest.startswith(_BSE_PREFIXES) else None
    return None


def _limit_rate(market: str, st_status: str, date: int) -> Decimal | None:
    if market in _NO_LIMIT:
        return None
    if market in (CN_GEM, CN_STAR):
        return Decimal("0.20")          # 普通股与 ST 一致
    if market == CN_BSE:
        return Decimal("0.30")          # 普通股与 ST 一致
    if market != CN_MAIN:
        return None
    is_st = st_status != "normal"
    if is_st and date < _ST_MAIN_WIDENED_FROM:
        return Decimal("0.05")
    return Decimal("0.10")


def _cent(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_prices(prev_close: float, market: str, st_status: str, date: int) -> tuple[float, float] | None:
    """`(涨停价, 跌停价)`；港美股、指数、板块返回 `None`（`st_status` 取 normal/ST/*ST/delisting）。"""
    rate = _limit_rate(market, st_status, date)
    if rate is None:
        return None
    base = Decimal(str(prev_close))
    return _cent(base * (1 + rate)), _cent(base * (1 - rate))


def limit_hit(close: float, up: float, down: float) -> str | None:
    """收盘价是否触及涨跌停（容差 1 分钱，§4.6.6）。"""
    if abs(close - up) <= 0.01:
        return "up"
    if abs(close - down) <= 0.01:
        return "down"
    return None


def oneword(bar_high: float, bar_low: float, hit: str | None) -> str | None:
    """一字板：四价相同且收在涨跌停价上，返回 `up` / `down` / `None`。

    用原始价判，不要用复权价——复权的四舍五入可能把 high 和 low 算成不同的值。
    """
    if hit is None:
        return None
    if round(bar_high, 2) != round(bar_low, 2):
        return None
    return hit
