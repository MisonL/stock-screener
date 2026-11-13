"""§4.4.1 的排除规则（入池前的硬剔除）。

七条规则各自返回一个稳定的理由串——漏斗报表、池子对拍（`scripts/accept/pool_vs_census.py`）
与日报都按这个字符串分组，改字面量等于改报表口径：

    st / bars / suspended / amount_20d / box_amount / b_share / limit_unknown

**两道成交额门槛不是一道**：`amount_20d` 是「近 20 日均额」（有没有人在交易），
`box_amount` 是「横盘期 N\\* 日均额」（**这段横盘**有没有人在交易）。§4.6.1 取最长有效窗口
天然偏好长期低波动股，只看 20 日均额会把「20 天前对倒放量、横盘期死寂」的僵尸股放进池子。

**ST 判定要带历史**：`st` 是**当日**的 `stock_status_history` 状态（`normal` / `ST` /
`*ST` / `delisting`），不是今天的名字前缀。`meta` 里的名称只作为兜底（盘后出池没有独立
状态表查询时的路径）——核心层不能 import `jobs/`，所以这里有一份与
`jobs.eod.st_status_of` 同规则的前缀表，两处必须一起改。
"""
from __future__ import annotations

# 名称前缀 → ST 状态。顺序不能换（`*ST` 先于 `ST`；`SST`/`ST`/`S` 都是「特别处理」）。
_ST_PREFIXES: tuple[tuple[str, str], ...] = (
    ("退", "delisting"),
    ("*ST", "*ST"),
    ("SST", "ST"),
    ("ST", "ST"),
    ("S", "ST"),
)

# B 股：沪市 900 开头、深市 200 开头（人民币特种股票，外币交易，不在本工具宇宙内）。
_B_SHARE_PREFIXES = ("sh900", "sz200")

_NORMAL = "normal"


def exclude(
    code: str,
    meta,
    n_bars: int,
    amount_20d: float,
    box_amount: float | None,
    suspended_250: int,
    st: str | None,
    p,
    u,
    limit_up: float | None = None,
) -> str | None:
    """按 §4.4.1 逐条判，命中即返回理由串；全部通过返回 `None`。

    `p` 是 `Config.market(m)`（`min_amount_20d`），`u` 是 `Config.strategy.universe`
    （`min_bars` / `suspend_max_in_250` / `box_amount_ratio`）。阈值一律来自配置。

    `box_amount`（横盘期 N\\* 日均额）在箱体选出来之前不存在，给 `None` 表示「还没算到这一步」，
    该门槛跳过——**盘后出池必须在 `find_box` 之后再调一次**，否则僵尸股防线从未执行。
    `limit_up` 是当日快照的涨停价：`None` = 没有快照（盘后出池），`<= 0` = 腾讯返回 `-1`、
    它自己也不确定（北交所、退市整理股），**这两种情况不能混为一谈**（S-A15）。
    """
    if _st_status(meta, st) != _NORMAL:
        return "st"
    if int(n_bars) < int(u.min_bars):
        return "bars"
    if int(suspended_250) > int(u.suspend_max_in_250):
        return "suspended"
    if float(amount_20d) < float(p.min_amount_20d):
        return "amount_20d"
    if box_amount is not None and float(box_amount) < float(amount_20d) * float(u.box_amount_ratio):
        return "box_amount"
    if is_b_share(code):
        return "b_share"
    if limit_up is not None and float(limit_up) <= 0:
        return "limit_unknown"
    return None


def is_b_share(code: str) -> bool:
    """B 股（900 / 200 开头）。用 `classify_market` 判不了：它按 A 股号段把它们也归进沪深。"""
    text = (code or "").lower()
    return text.startswith(_B_SHARE_PREFIXES)


def _st_status(meta, st: str | None) -> str:
    """当日 ST 状态：优先用独立查询给的 `st`，否则从 `meta` 的名称前缀推。"""
    if st is not None:
        return str(st)
    raw = _field(meta, "st_status")
    if raw:
        return str(raw)
    return st_from_name(_field(meta, "name") or "")


def st_from_name(name: str) -> str:
    """名称前缀 → `st_status`；与 `jobs.eod.st_status_of` 同规则（核心层不许 import jobs）。"""
    text = (name or "").strip()
    if not text:
        return _NORMAL
    for prefix, status in _ST_PREFIXES:
        if text.startswith(prefix):
            return status
    return _NORMAL


def _field(meta, key: str):
    """`meta` 可以是 dict / `sqlite3.Row` / 带同名属性的对象——调用方三处都传过。"""
    if meta is None:
        return None
    try:
        return meta[key]
    except (KeyError, IndexError, TypeError):
        return getattr(meta, key, None)
