"""新浪行情源：校验日线、快照备源、全市场代码列表。

**单位**：新浪的成交量本来就是「股」，原样存；腾讯 `f[6]` 是「手」要 ×100。
这条差异必须由数据源自己承担，否则降级到新浪时快照 `volume` 会整体 ×100 跳变。
新浪日线没有成交额，`amount` 固定 0.0——所以它只能做校验与补缺，**不写 `daily_bars`**（决定 #3）。

**Referer 必填**：`hq.sinajs.cn` 不带 `https://finance.sina.com.cn` 会 403（§6.3）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from core.datasource.base import field, num
from core.http import Http, UpstreamError
from core.market import classify_market
from core.models import Bar, Quote, SectorRow

KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
SNAPSHOT_URL = "https://hq.sinajs.cn/list="
UNIVERSE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
)
# 板块快照：行业 49 个 `new_*` + 概念 `gn_*`，两套分类零重叠（spec §7.5.5 实测）。
INDUSTRY_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
CONCEPT_URL = "https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class"

REFERER = {"Referer": "https://finance.sina.com.cn"}

PAGE_SIZE = 100
_MAX_PAGES = 200        # 兜底：上游异常时不要死循环

# 板块快照每条是 `"<代码>":"<代码>,<名称>,…"` 的 JS 对象字面量（GBK）。
_SECTOR_ENTRY = re.compile(r'"([^"]+)"\s*:\s*"([^"]*)"')


class SinaSource:
    def __init__(self, http: Http):
        self.http = http

    # ---- 日线（只做校验） -------------------------------------------------

    def fetch_kline_daily(self, code: str, count: int = 1250, scope: str = "backfill") -> list[Bar]:
        url = f"{KLINE_URL}?symbol={code}&scale=240&ma=no&datalen={count}"
        text = self.http.get(url, headers=REFERER, scope=scope)
        try:
            rows = json.loads(text)
        except ValueError as exc:
            raise UpstreamError(f"{code} 新浪日线不是 JSON: {exc}") from exc
        if not isinstance(rows, list) or not rows:
            raise UpstreamError(f"{code} 新浪日线为空数组")
        return [self._bar(code, row) for row in rows]

    @staticmethod
    def _bar(code: str, row: dict) -> Bar:
        # 新浪列序是 开/高/低/收（腾讯是 开/收/高/低）——两个解析器不能共用字段顺序。
        if not isinstance(row, dict):
            raise UpstreamError(f"{code} 日线行不是对象：{type(row).__name__}")
        for field in ("day", "open", "high", "low", "close"):
            if row.get(field) in (None, ""):
                raise UpstreamError(f"{code} 日线行缺 {field}")
        return Bar(
            code=code,
            date=int(str(row["day"]).replace("-", "")),
            open=num(str(row.get("open"))),
            high=num(str(row.get("high"))),
            low=num(str(row.get("low"))),
            close=num(str(row.get("close"))),
            volume=num(str(row.get("volume"))) or 0.0,   # 已是股，不 ×100
            amount=0.0,                                  # 新浪日线没有成交额
            source="sina",
        )

    # ---- 快照 -------------------------------------------------------------

    def fetch_snapshot(self, codes: list[str], scope: str = "pool") -> dict[str, Quote]:
        if not codes:
            return {}
        text = self.http.get(SNAPSHOT_URL + ",".join(codes), headers=REFERER, encoding="gbk", scope=scope)
        wanted = set(codes)
        out: dict[str, Quote] = {}
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            key = key.strip()
            if not key.startswith("var hq_str_"):
                continue
            code = key[len("var hq_str_"):]
            body = value.strip().strip('"')
            if code not in wanted or not body:
                continue
            out[code] = self._quote(code, body.split(","))
        return out

    @staticmethod
    def _quote(code: str, f: list[str]) -> Quote:
        volume = num(field(f, 8))
        return Quote(
            code=code,
            name=field(f, 0),
            price=num(field(f, 3)),
            prev_close=num(field(f, 2)),
            open=num(field(f, 1)),
            high=num(field(f, 4)),
            low=num(field(f, 5)),
            volume=volume,            # 已是股
            amount=num(field(f, 9)),
            volume_ratio=None,        # 新浪不给量比
            limit_up=None,            # 新浪不给涨跌停价，按 §4.6.6 自己算
            limit_down=None,
            ts=_ts(field(f, 30), field(f, 31)),
            raw=f,
        )

    # ---- 全市场代码列表（D12） --------------------------------------------

    def fetch_universe(self, scope: str = "backfill") -> list[tuple[str, str]]:
        """分页直到返回空数组；`(code, name)` 的 code 带 sh/sz/bj 前缀。"""
        return self._paged_symbols("hs_a", scope=scope)

    def _paged_symbols(self, node: str, scope: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for page in range(1, _MAX_PAGES + 1):
            url = (
                f"{UNIVERSE_URL}?page={page}&num={PAGE_SIZE}&sort=symbol&asc=1&node={node}"
            )
            text = self.http.get(url, headers=REFERER, scope=scope)
            try:
                rows = json.loads(text)
            except ValueError as exc:
                raise UpstreamError(f"新浪 {node} 第 {page} 页不是 JSON: {exc}") from exc
            if not isinstance(rows, list):
                raise UpstreamError(f"新浪 {node} 第 {page} 页不是数组")
            if not rows:
                return out
            for row in rows:
                code = str(row.get("symbol", "")).strip()
                name = str(row.get("name", "")).strip()
                # B 股与老三板不在宇宙内（classify_market 返回 None）
                if not code or code in seen or classify_market(code) is None:
                    continue
                seen.add(code)
                out.append((code, name))
        raise UpstreamError(f"新浪 {node} 分页超过 {_MAX_PAGES} 页仍未结束")

    # ---- 板块（Task 12：成分股与板块快照） ---------------------------------

    def fetch_sector_list(self, scope: str = "members") -> list[tuple[str, str]]:
        """行业板块清单：`newSinaHy.php` 的 49 个 `new_*`（spec §7.5.5 实测 49 个）。"""
        text = self.http.get(INDUSTRY_URL, headers=REFERER, encoding="gbk", scope=scope)
        return [(row.code, row.name) for row in _sector_rows(text)]

    def fetch_sector_members(self, sector: str, scope: str = "members") -> list[str]:
        """行业/概念成分股：`getHQNodeData?node=<new_xxx|gn_xxx>`，必须分页取全。"""
        return [code for code, _ in self._paged_symbols(sector, scope=scope)]

    def fetch_sector_snapshot(self, scope: str = "sector") -> list[SectorRow]:
        """行业 + 概念两套快照（各 1 个请求，字段布局相同，spec §7.5.5）。

        **不要合并这两套分类**：`new_*` 与 `gn_*` 零重叠，是两条独立的合成序列。
        """
        rows: list[SectorRow] = []
        for url in (INDUSTRY_URL, CONCEPT_URL):
            text = self.http.get(url, headers=REFERER, encoding="gbk", scope=scope)
            rows.extend(_sector_rows(text))
        return rows


def _sector_rows(text: str) -> list[SectorRow]:
    """把 `newSinaHy.php` / `newFLJK.php` 的 JS 对象字面量拆成 `SectorRow`。

    13 个字段（两个接口布局相同，spec §7.5.5）：
    `[代码, 名称, 成分股数, 平均价, 涨跌额, 涨跌幅%, 成交量, 成交额, 领涨股…]`。
    """
    out: list[SectorRow] = []
    for _, value in _SECTOR_ENTRY.findall(text):
        parts = value.split(",")
        if len(parts) < 7:
            continue
        price = num(field(parts, 3))
        chg_amount = num(field(parts, 4))
        out.append(
            SectorRow(
                code=field(parts, 0).strip(),
                name=field(parts, 1).strip(),
                price=price,
                prev_close=None if price is None or chg_amount is None
                else round(price - chg_amount, 2),
                chg=num(field(parts, 5)),
                volume=num(field(parts, 6)),
            )
        )
    return out


def _ts(day: str, hm: str) -> datetime | None:
    from core.clock import SH

    text = f"{day.strip()} {hm.strip()}"
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH)
    except ValueError:
        return None
