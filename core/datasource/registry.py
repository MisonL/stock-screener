"""多源降级链：腾讯失败 → 新浪，连续失败 5 分钟内不再打主源。

**降级不改变单位语义**：两个源出来的 `Bar.volume` / `Quote.volume` 都已经是「股」，
换算由数据源自己承担（腾讯 ×100、新浪原样）。调用方不需要知道这次是谁回的。

`force_fallback` 为 `None` 时读环境变量 `SS_FORCE_FALLBACK`（`sina` / `1`）——
Task 29 的降级演练靠它，不实现读取方就等于演练静默通过。
"""
from __future__ import annotations

import os
import time
from typing import Callable

from core.http import ForbiddenHost, UpstreamBlocked, UpstreamError, UpstreamTimeout

FAIL_THRESHOLD = 3
COOLDOWN_S = 300
# 响应超过这个秒数也计一次失败（§6.3 的单只 K 线实测 ~96ms，3 秒已是不正常）
SLOW_S = 3.0

_FORCE_VALUES = {"sina", "1", "true", "yes"}


class HealthTracker:
    """连续失败计数 + 冷却期。"""

    def __init__(self, fail_threshold: int = FAIL_THRESHOLD, cooldown_s: float = COOLDOWN_S,
                 mono: Callable[[], float] = time.monotonic):
        self.fail_threshold = fail_threshold
        self.cooldown_s = cooldown_s
        self._mono = mono
        self._failures = 0
        self._down_until: float | None = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.fail_threshold:
            self._down_until = self._mono() + self.cooldown_s

    def record_success(self) -> None:
        self._failures = 0
        self._down_until = None

    def available(self) -> bool:
        return self._down_until is None or self._mono() >= self._down_until


class SourceRegistry:
    """主源 / 备源的统一入口。返回值第二个元素是实际使用的源名（写 `fetch_log`）。"""

    def __init__(
        self,
        primary,
        fallback,
        health: HealthTracker | None = None,
        mono: Callable[[], float] | None = None,
        force_fallback: bool | None = None,
    ):
        self.primary = primary
        self.fallback = fallback
        self._mono = _as_mono(mono)
        # 不能写成默认参数：可变默认值会让同进程里所有 registry 共享同一份失败状态
        self.health = health if health is not None else HealthTracker(mono=self._mono)
        if force_fallback is None:
            force_fallback = os.environ.get("SS_FORCE_FALLBACK", "").strip().lower() in _FORCE_VALUES
        self.force_fallback = bool(force_fallback)
        # Task 12 的 refresh_sector_members 需要 SinaSource 实例，registry 必须暴露它
        self.sina = fallback if hasattr(fallback, "fetch_universe") else None

    def kline_daily(self, code: str, count: int = 1700, scope: str = "backfill"):
        return self._call("fetch_kline_daily", code, count=count, scope=scope)

    def snapshot(self, codes: list[str], scope: str = "pool"):
        # **scope 必须透传**：数据源内部默认是 "pool"，不传的话 eod/members 的请求
        # 会被记到 pool 上（或被记账白名单跳过），账面永远看不出 eod 花了几次请求
        return self._call("fetch_snapshot", codes, scope=scope)

    def _call(self, method: str, *args, **kwargs):
        if not self.force_fallback and self.health.available():
            started = self._mono()
            try:
                result = getattr(self.primary, method)(*args, **kwargs)
            except (UpstreamError, UpstreamBlocked, UpstreamTimeout, ForbiddenHost):
                self.health.record_failure()
            else:
                if self._mono() - started <= SLOW_S:
                    self.health.record_success()
                    return result, _name_of(self.primary)
                self.health.record_failure()      # 慢响应同样算一次失败

        result = getattr(self.fallback, method)(*args, **kwargs)
        return result, _name_of(self.fallback)


def _as_mono(mono) -> Callable[[], float]:
    """接受可调用对象或带 `.now()` 的时钟实例（`FakeMono`）。"""
    if mono is None:
        return time.monotonic
    if callable(mono):
        return mono
    return mono.now


def _name_of(source) -> str:
    return getattr(source, "name", type(source).__name__.lower())
