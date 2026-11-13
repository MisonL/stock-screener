"""注意力层显著性打分（§5.4.3）：`base[kind] × (1 + breadth) × focus`。

**疲劳抑制不在这里**：它作用在阈值上，`event_log.salience` 存的永远是绝对分，
这样才能事后统计分布回来标定 base 表。
"""
from __future__ import annotations

_DEFAULT_BREADTH = {"market": 0.60, "sector_merged_3": 0.30, "stock": 0.0}


def salience(kind: str, scope: str, focus: float, merged_n: int = 1, p=None) -> float:
    """`scope` 取 `stock / sector / market`；`focus` 是**浮点数**（1.3 / 1.0 / 0.5）。

    传字符串（`"watch"`）会在 `base × focus` 处 `TypeError`——这是刻意的：
    「自选股」由 `Detection.watch` 决定，不由 kind 决定（D6）。
    """
    base = p.base
    breadth = dict(_DEFAULT_BREADTH)
    breadth.update(p.breadth)         # 广度幅度可配置；配置缺键时用上面的默认
    value = float(base[kind]) if kind in base else 0.0
    extra = 0.0
    if scope == "market":
        extra = breadth["market"]
    elif scope == "sector" and merged_n >= 3:
        extra = breadth["sector_merged_3"]
    elif scope == "stock":
        extra = breadth.get("stock", 0.0)
    return value * (1 + extra) * focus
