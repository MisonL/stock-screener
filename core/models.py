"""跨层数据模型：一根日线、一份快照。

`Bar` 是 `daily_bars` 的行；`Quote` 是上游快照行；`DayRow` 是标注后的 bar（复权值 +
raw 值 + 涨跌停/复牌标记）。后续任务的 `SectorRow` / `PoolEntry` / `Detection` 在各自
任务里追加，不在本文件提前造。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class Bar:
    """一根日线。`volume` 单位「股」，`amount` 单位「元」（§6.1 的单位归一）。"""

    code: str
    date: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    source: str

    def is_placeholder(
        self,
        today: int,
        prev_close: float | None = None,
        close_time: str = "14:57",
        now: datetime | None = None,
    ) -> bool:
        """盘前快照组装出的「占位 bar」判据（§4.6.9 (e)，三重门 + 时间门）。

        ① 结构不合法 → 占位（**`high == low` 是合法的一字板，不是占位**）；
        ② 四价相等且零成交 → 占位；
        ③ 四价相等、当日、且收盘等于昨收 → 占位（停牌/盘前）；
        时间门：当日 bar 只有在 `close_time + 5min`（默认 15:02）之后才是收盘 bar，
        早于该时刻的当日 bar 一律是中间态，不入库。

        历史 bar 只过 ①②：一字跌停（四价相等、有量、收在跌停价）是真 K 线，必须保留。
        """
        if (
            self.low <= 0
            or self.high < self.low
            or not (self.low <= self.open <= self.high and self.low <= self.close <= self.high)
        ):
            return True

        flat = self.open == self.high == self.low == self.close
        if flat and self.volume == 0:
            return True
        if self.date != today:
            return False

        if now is not None:
            # time 不能直接加 timedelta：按「当日第几分钟」比，跨小时也正确
            if _minutes(now.time()) < _minutes(_hm(close_time)) + 5:
                return True
        return flat and prev_close is not None and self.close == prev_close


@dataclass(frozen=True)
class Quote:
    """一份实时快照。`volume` 股、`amount` 元（腾讯原始是手/万元，数据源负责换算）。"""

    code: str
    name: str
    price: float | None
    prev_close: float | None
    open: float | None
    high: float | None
    low: float | None
    volume: float | None
    amount: float | None
    volume_ratio: float | None
    limit_up: float | None
    limit_down: float | None
    ts: datetime | None
    raw: list[str]


@dataclass(frozen=True)
class DayRow:
    """一根标注后的日线（`core.strategy.window.annotate` 的产物）。

    `o…v` 与 `amount` 是**复权**值（因子、形态用）；`raw_*` 是不复权值——涨跌停与盘中
    快照桥接必须用 raw（§4.6.6）。`resume` 是复牌首日，`since_resume` 是距最近一次
    复牌日的交易日数（复牌首日为 0）。
    """

    date: int
    o: float
    h: float
    l: float
    c: float
    v: float
    raw_h: float
    raw_l: float
    raw_c: float
    raw_v: float
    amount: float
    hit: str | None = None
    oneword: str | None = None
    resume: bool = False
    since_resume: int = 0


@dataclass(frozen=True)
class Decline:
    """B4 / B8 / D2 共用的下跌期窗口（§4.6.5、§4.6.6 末尾红框）。

    `start` / `tail_start` 是 `rows` 的下标；`length = box_start − start`；
    `tail` 是 `rows[tail_start:box_start]`（末尾最多 60 根）。
    """

    start: int
    length: int
    tail_start: int
    mean_vol: float
    min_low: float
    oneword_down: int


@dataclass(frozen=True)
class Box:
    """横盘箱体（复权价，spec §4.6.1）。

    `start` 是窗口左端在 `rows` 里的下标，窗口 = `rows[start:-1]`（右端是昨天，不含今天），
    故 `n == len(rows) - start - 1`。`pos_in_box = (今日复权收盘 − low) / (high − low)`，
    今天刚突破上沿时**可以大于 1**（§4.6.1）。`high == low` 的窗口不会产出 `Box`：
    `find_box` 的 `zero_range` 守卫直接作废该 N，不做 `pos_in_box = 0.5` 之类的兜底。
    """

    n: int
    start: int
    high: float
    low: float
    pos_in_box: float


@dataclass(frozen=True)
class Reject:
    """策略淘汰原因：`stage` 是漏斗阶段（`box / decline / B8 / B4 / A`），`reason` 机器可读。

    `reason` 取值要稳定——池子对拍与日报按它分组，改字面量等于改报表口径。
    """

    stage: str
    reason: str


@dataclass(frozen=True)
class SectorRow:
    """一行板块快照（新浪行业 `new_*` / 概念 `gn_*`）。

    `price` 是上游的「平均价」，`prev_close = 平均价 − 涨跌额`；`chg` 是上游给的
    涨跌幅**百分数**原值（`0.61` 表示 +0.61%），不做除法。上游不给时间戳，`ts` 为 None，
    `as_of` 由调用方的 `Clock` 提供。
    """

    code: str
    name: str
    price: float | None
    prev_close: float | None
    chg: float | None
    volume: float | None
    ts: datetime | None = None
    members: int | None = None     # 成分股数：< min_members 的板块不做异动判定（小样本噪音大）


def _hm(hm: str) -> time:
    hour, minute = hm.split(":")
    return time(int(hour), int(minute))


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute
