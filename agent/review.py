"""盘后回访补答：**当天没跑成的闹钟，用真实收盘数据回答**。

2026-11-27 的情形：14:32 预算断粮后，19 个闹钟（14:32–15:17 到期）全部停在
`pending`。它们问的几乎是同一件事——

    "飞利信是否收在 3.9954 上方、量比是否破 1.5"
    "华鲁恒升 17.6128 下沿守没守住"
    "涨停家数是否守住 300"

而这些答案 **15:05 收盘数据一落地就全有了**。原机制是次日开盘按日期作废，
于是"当天没跑成的回访"永远没人回答，用户只看到 agent 下午莫名其妙地哑了。

本模块**不调模型**：直接从 `daily_bars` + `snapshot_cache` 读出收盘结果，
按闹钟里点名的股票组成一条事实清单。这样：

- 零成本、零延迟、可复核（每个数字都能一条 SQL 追回去）；
- 不会因为"再跑一次 LLM"又把刚恢复的预算烧掉；
- 盘后推一条，把当天的事收尾。

**只回答"收盘可验证"的部分**：价格站没站上、涨停数守没守住。闹钟里
"是否放量""板块梯队是否换序"这类需要人判断的，不在本模块范围——那本来
就该由 agent 自己说，只是它没机会说。
"""

from __future__ import annotations

import re
import sqlite3

#: 闹钟 note 里出现的 A 股代码（带市场前缀）。只认 6 位数字+前缀，
#: 避免把"3.9954"这种价位误当成代码。
_CODE_RE = re.compile(r"\b(?:sh|sz|bj)(\d{6})\b")


def codes_in(notes) -> list[str]:
    """从一组闹钟 note 里提取去重后的代码（保持首次出现顺序）。"""
    seen: list[str] = []
    for note in notes:
        for m in _CODE_RE.finditer(str(note or "")):
            code = m.group(0)
            if code not in seen:
                seen.append(code)
    return seen


def named_codes(conn, notes, limit: int = 6) -> list[str]:
    """从 note 里点名的**股票名称**反查代码。

    note 里写的往往是名字（"飞利信""华鲁恒升""史丹利"），代码只在 `target`
    字段上。按 `stock_meta.name` 做精确/包含匹配，先长后短，避免"华鲁"匹配到
    别的票。名称短于 2 字的跳过（"中"这种会匹配一大片）。
    """
    text = "\n".join(str(n or "") for n in notes)
    hits: list[str] = []
    try:
        names = conn.execute("SELECT code, name FROM stock_meta WHERE name IS NOT NULL").fetchall()
    except sqlite3.Error:
        return hits
    for code, name in sorted(names, key=lambda r: -len(str(r[1]))):
        name = str(name).strip()
        if len(name) < 2 or name not in text or code in hits:
            continue
        hits.append(code)
        if len(hits) >= limit:
            break
    return hits


def breadth(conn, trade_date: int) -> dict:
    """从**当天**的全市场快照算收盘广度（涨停/跌停/上涨/下跌）。

    **必须校验快照日期**：`snapshot_cache` 只有一行"最后一个快照"，不带日期查询。
    不校验的话，收盘后再跑（或 EOD 抓取失败那天）会把**昨天的广度当成今天的**
    报出去——这比不报严重得多：用户会以为今天涨停 445，实际那是上一交易日的数。

    与 `notify/dispatcher.py::_close_breadth()` 同一口径：拿不到就返回空，
    **不凭空写 0**。
    """
    try:
        import json
        row = conn.execute(
            "SELECT as_of, payload FROM snapshot_cache WHERE scope = 'market'").fetchone()
        if not row or not row[1]:
            return {}
        # as_of 形如 `2026-11-27T15:05:15+08:00`；取前 10 位与交易日比对
        as_of_day = str(row[0] or "")[:10].replace("-", "")
        if as_of_day and as_of_day != str(trade_date):
            return {}
        rows = json.loads(str(row[1])).get("rows") or []
    except (TypeError, ValueError, sqlite3.Error):
        return {}
    if not rows:
        return {}
    up = down = lu = ld = 0
    for r in rows:
        price, prev = r.get("price"), r.get("prev_close")
        if not isinstance(price, (int, float)) or not isinstance(prev, (int, float)):
            continue
        if price > prev:
            up += 1
        elif price < prev:
            down += 1
        cap, floor = r.get("up"), r.get("down")
        if isinstance(cap, (int, float)) and price >= cap:
            lu += 1
        if isinstance(floor, (int, float)) and price <= floor:
            ld += 1
    return {"up": up, "down": down, "limit_up": lu, "limit_down": ld}


def build(conn, notes, trade_date: int, *, targets=()) -> tuple[str, list[str]]:
    """生成补答正文与点名的代码列表。

    返回 `(body, codes)`；没有可用数据时 `body` 为空——**不推空消息**。
    """
    notes = list(notes)
    # **个股优先于指数**（v0.2.158，今日实测踩到）：原来的顺序是
    # `targets + 代码 + 名字` 截前 6 个，结果 19 条闹钟里有 4 条挂在指数上
    # （sh000001 / sh000688 / sz399001 / sz399006），把名额占满——
    # 而真正被反复问的「飞利信」「史丹利」一个都没出现。
    #
    # 补答的价值在个股：指数涨多少用户自己看得见，个股守没守住某条线才是
    # agent 真正答应要回访的事。所以先排个股，指数放到最后。
    raw = list(dict.fromkeys(list(targets) + codes_in(notes) + named_codes(conn, notes)))
    raw = [c for c in raw if c and isinstance(c, str)]
    try:
        from core.market import INDEX, classify_market
        indexes = [c for c in raw if classify_market(c) == INDEX]
        stocks = [c for c in raw if classify_market(c) != INDEX]
    except Exception:                            # noqa: BLE001
        stocks, indexes = raw, []
    codes = (stocks + indexes)[:6]
    lines: list[str] = []

    if codes:
        marks = ",".join("?" for _ in codes)
        rows = conn.execute(
            f"SELECT code, close, high, low, volume, amount FROM daily_bars"
            f" WHERE date = ? AND code IN ({marks})", (trade_date, *codes)).fetchall()
        got = {r[0]: r for r in rows}
        if got:
            lines.append("【今日收盘复核】")
            meta = {}
            try:
                meta = {r[0]: r[1] for r in conn.execute(
                    f"SELECT code, name FROM stock_meta WHERE code IN ({marks})", tuple(codes))}
            except sqlite3.Error:
                pass
            for code in codes:
                r = got.get(code)
                if r is None:
                    continue
                name = meta.get(code, "")
                pct = ""
                try:
                    prev = conn.execute(
                        "SELECT close FROM daily_bars WHERE date = (SELECT max(date) FROM"
                        " daily_bars WHERE date < ? AND code = ?) AND code = ?",
                        (trade_date, code, code)).fetchone()
                    if prev and prev[0]:
                        pct = f" {(r[1] / prev[0] - 1) * 100:+.2f}%"
                except sqlite3.Error:
                    pass
                lines.append(f"- {name or code} 收 {r[1]}{pct}（高 {r[2]} / 低 {r[3]}）")

    b = breadth(conn, trade_date)
    if b:
        lines.append(f"- 涨停 {b['limit_up']} ｜ 跌停 {b['limit_down']}"
                     f" ｜ 上涨 {b['up']} / 下跌 {b['down']}")

    if not lines:
        return "", codes
    lines.append("（以上为收盘数据，替代今日未能发出的回访）")
    return "\n".join(lines), codes
