"""数据源共用的小工具与协议。

腾讯 / 新浪 / westock 的响应字段位置、空值处理各不相同，但「取不到就给 None、
不抛 KeyError」这条纪律是共通的——上游少一列时程序应当降级，不该炸在解析里。
"""
from __future__ import annotations

from typing import Protocol

from core.models import Bar, Quote


class Source(Protocol):
    """行情源的公共面。`fetch_universe` 只有新浪实现（D12）。"""

    def fetch_kline_daily(self, code: str, count: int = 1700, end: str | None = None) -> list[Bar]: ...

    def fetch_snapshot(self, codes: list[str]) -> dict[str, Quote]: ...


def num(value: str | None) -> float | None:
    """空串/`-`/非数字 → `None`（上游缺字段是常态，不是异常）。"""
    if value is None:
        return None
    text = value.strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def field(parts: list[str], index: int) -> str:
    """越界取空串：`len(f)` 不固定，不要在解析里假设字段数。"""
    return parts[index] if index < len(parts) else ""
