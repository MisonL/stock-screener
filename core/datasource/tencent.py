"""腾讯行情源：日线（raw）与批量快照。

**禁用 `web.ifzq.gtimg.cn`**（WAF 持续 501），日线走 `proxy.finance.qq.com`。
K 线参数只用四段形态 `,,,count,`，`count ≤ 2000`（回填取 1700）。

**`code==0` 但 `data` 不是 dict 或 bar 数组为空 = 异常**，不是「没数据」：
静默返回空列表会让回填把整只票记成「成功但 0 根」，缺口永远补不上。
"""
from __future__ import annotations

import json
from datetime import datetime

from core.datasource.base import field, num
from core.http import Http, UpstreamError
from core.models import Bar, Quote

KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
SNAPSHOT_URL = "https://qt.gtimg.cn/q="

# 快照 600 只/请求（§6.1.2.2 的运营值；上游真正的上限是 URL 长度，约 900 只）
CHUNK = 600

_MAX_COUNT = 2000


class TencentSource:
    def __init__(self, http: Http):
        self.http = http
        # 批量快照里上游没返回的代码：按用途写 fetch_log(purpose='snapshot_intraday' | 'snapshot_eod', status='empty')
        # （schema 的 CHECK 只允许 backfill / snapshot_eod / snapshot_intraday 三种）
        self.last_missing: set[str] = set()

    # ---- 日线 -------------------------------------------------------------

    def fetch_kline_daily(self, code: str, count: int = 1700, end: str | None = None, scope: str = "backfill") -> list[Bar]:
        if count > _MAX_COUNT:
            raise UpstreamError(f"count 超过上游上限 {_MAX_COUNT}: {count}")
        param = f"{code},day,{end},{end},{count}," if end else f"{code},day,,,{count},"
        text = self.http.get(f"{KLINE_URL}?param={param}", scope=scope)

        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise UpstreamError(f"{code} 日线响应不是 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise UpstreamError(f"{code} 日线响应根不是对象：{type(payload).__name__}")
        if payload.get("code") != 0:
            raise UpstreamError(f"{code} 日线返回 code={payload.get('code')!r} msg={payload.get('msg')!r}")

        data = payload.get("data")
        node = data.get(code) if isinstance(data, dict) else None
        if not isinstance(node, dict):
            raise UpstreamError(f"{code} 日线 data 不是 dict")
        rows = node.get("day") or node.get("qfqday")
        if not rows:
            raise UpstreamError(f"{code} 日线 bar 数组为空")
        if not isinstance(rows, list):
            raise UpstreamError(f"{code} 日线 bar 不是数组：{type(rows).__name__}")

        return [self._bar(code, row) for row in rows]

    @staticmethod
    def _bar(code: str, row: list) -> Bar:
        # 列序：0 日期、1 开、2 收、3 高、4 低、5 量(手)、8 额(万元)
        # 短行（上游截断、只回日期+收盘）必须变成 UpstreamError：IndexError 会绕过
        # backfill 的重试与单只淘汰，把整轮回填打断。
        if not isinstance(row, list) or len(row) < 6:
            raise UpstreamError(f"{code} 日线行字段不足（{len(row) if hasattr(row, '__len__') else type(row)} 列）")
        try:
            date = int(str(row[0]).replace("-", ""))
            open_, close, high, low = (num(str(row[i])) for i in (1, 2, 3, 4))
            volume = num(str(row[5]))
            amount = num(str(row[8])) if len(row) > 8 else None
        except (TypeError, ValueError) as exc:
            raise UpstreamError(f"{code} 日线行解析失败：{exc}") from None
        for name, value in (("open", open_), ("close", close), ("high", high), ("low", low)):
            if value is None:
                raise UpstreamError(f"{code} {date} 日线缺 {name}")
        return Bar(
            code=code,
            date=date,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=(volume or 0.0) * 100,      # 手 → 股
            amount=(amount or 0.0) * 10000,    # 万元 → 元
            source="tencent",
        )

    # ---- 快照 -------------------------------------------------------------

    def fetch_snapshot(self, codes: list[str], scope: str = "pool") -> dict[str, Quote]:
        """按**返回行的代码**回填，不按下标对号：上游对无效代码直接不返回那一行。"""
        self.last_missing = set()
        if not codes:
            return {}
        text = self.http.get(SNAPSHOT_URL + ",".join(codes), encoding="gbk", scope=scope)

        out: dict[str, Quote] = {}
        wanted = set(codes)
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            key = key.strip()
            if not key.startswith("v_"):
                continue
            code = key[2:]
            body = value.strip().strip('"')
            if code not in wanted or not body:
                continue
            out[code] = self._quote(code, body.split("~"))

        self.last_missing = wanted - set(out)
        return out

    @staticmethod
    def _quote(code: str, f: list[str]) -> Quote:
        volume = num(field(f, 6))
        amount = num(field(f, 37))
        return Quote(
            code=code,
            name=field(f, 1),
            price=num(field(f, 3)),
            prev_close=num(field(f, 4)),
            open=num(field(f, 5)),
            high=num(field(f, 33)),
            low=num(field(f, 34)),
            volume=None if volume is None else volume * 100,          # 手 → 股
            amount=None if amount is None else amount * 10000,        # 万元 → 元
            volume_ratio=num(field(f, 49)),
            limit_up=num(field(f, 47)),
            limit_down=num(field(f, 48)),
            ts=_ts(field(f, 30)),
            raw=f,
        )


def _ts(value: str) -> datetime | None:
    """`f[30]` 是 `YYYYMMDDHHMMSS` 的上海本地时刻。"""
    from core.clock import SH

    text = value.strip()
    if len(text) < 14 or not text.isdigit():
        return None
    return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=SH)
