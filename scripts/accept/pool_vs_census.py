#!/usr/bin/env python3
"""观察池 vs. 全市场普查基线：集合对拍 + 逐行归因（Task 18 Step 5）。

在**回填好的真实库**里跑（不是单测）：

    docker compose -f deploy/docker-compose.yml --project-directory . --profile tools \
        run --rm backfill python scripts/accept/pool_vs_census.py --as-of 20261030 --market cn_main

输出两张表：

- **基线有、产品没有**：每一行给 `rejects[code]` 的 `(阶段, 理由)` 与对应的 `rule_id`；
- **产品有、基线没有**：每一行给「基线脚本没实现的规则」里命中的那一条。

`rule_id` 是 §4.4.1 / §4.6.x 的**枚举值**（不是自由文本），验收口径是
「`rule_id is None` 的行数 == 0」——这就是计划里那句「无法归因 = 0 行」的可执行版本（F-25）。

基线本身是**已知错口径**（`/tmp/screener_market/census58.txt` 由 demo 的 `find_box()` /
`b4_ratio()` 生成，下跌期固定取箱体前 60 根、没有 §4.4.1 的七道排除），所以数量不要求
等于基线条数，要求的是**每个差集都能归因到一条具体规则**（F-24）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# `python scripts/accept/pool_vs_census.py` 时 sys.path[0] 是脚本目录，核心包在仓库根下。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import load_config  # noqa: E402
from core.db import connect  # noqa: E402
from core.strategy.pool import build_pool  # noqa: E402

DEFAULT_BASELINE = "tests/fixtures/census_main_20261030.txt"

# 淘汰原因 → §4.4.1 / §4.6.x 的规则编号（枚举值；报表与验收都按它分组）
RULE_BY_REJECT = {
    ("b_share", "b_share"): "4.4.1-b_share",
    ("bars", "bars"): "4.4.1-min_bars",
    ("amount", "amount_20d"): "4.4.1-min_amount_20d",
    ("st", "st"): "4.4.1-st",
    ("suspended", "suspended_250"): "4.4.1-suspend_250",
    ("filter", "box_amount"): "4.4.1-box_amount",
    ("filter", "exright"): "4.6.6-exright_day",
    ("filter", "limit_unknown"): "4.4.1-limit_unknown",
    ("box", "no_window"): "4.6.1-no_box",
    ("box", "oneword_down"): "4.6.6-oneword_down",
    ("box", "oneword_up_today"): "4.6.6-oneword_up",
    ("box", "resume_cooldown"): "4.6.6-resume",
    ("box", "zero_range"): "4.6.1-zero_range",
    ("decline", "no_decline"): "4.6.5-no_decline",
    ("decline", "too_short"): "4.6.5-decline_too_short",
    ("decline", "oneword_down"): "4.6.6-oneword_down",
    ("decline", "zero_vol"): "4.6.5-zero_vol",
    ("B8", "new_low"): "4.2-B8",
    ("B4", "vol_not_dry"): "4.2-B4",
    ("B4", "zero_box_vol"): "4.6.4-zero_box_vol",
    ("A", "drawdown"): "4.2-A1",
    ("A", "below_ma"): "4.2-A2",
    ("A", "ma_trend"): "4.2-A3",
    ("A", "warmup"): "4.2-A-warmup",
    ("A", "bad_price"): "4.2-A-bad_price",
    ("data", "no_adj"): "6.1-no_adj",
    ("data", "bad_factor"): "4.6.9-bad_factor",
}


def load_baseline(path: Path) -> list[str]:
    """读基线清单：`#` 开头是注释，空白与重复忽略。"""
    codes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if text and text not in codes:
            codes.append(text)
    return codes


def rule_of(reject: tuple[str, str] | None) -> str | None:
    if reject is None:
        return None
    return RULE_BY_REJECT.get(tuple(reject))


def product_only_rule(conn, cfg, code: str, as_of: int) -> str | None:
    """「产品有、基线没有」时，判断是基线脚本漏实现的哪一条规则。

    基线（demo 的 `screen()`）只做了 bars / 20 日均额 / box / B8 / B4 / A 六道，
    没有 ST、停牌、横盘期均额、一字板、复牌、触板重新起算、共用下跌期窗口七条。
    这里能机械判定的四类按 §4.4.1 / §4.6.6 编号返回，判不了的回 `None`（= 无法归因）。
    """
    row = conn.execute(
        "SELECT COALESCE(h.st_status, 'normal') FROM stock_meta m"
        " LEFT JOIN stock_status_history h ON h.code = m.code AND h.start_date <= ?"
        "   AND (h.end_date IS NULL OR h.end_date >= ?)"
        " WHERE m.code = ?",
        (as_of, as_of, code),
    ).fetchone()
    if row is not None and row[0] != "normal":
        return "4.4.1-st"

    stats = conn.execute(
        "SELECT suspended_250 FROM stock_stats WHERE code = ?", (code,)
    ).fetchone()
    if stats is not None and int(stats[0]) > 0:
        return "4.4.1-suspend_250"

    lookback = int(cfg.strategy.box.max_lookback) + 1
    recent = conn.execute(
        "SELECT high, low, close FROM daily_bars WHERE code = ? AND date <= ?"
        " ORDER BY date DESC LIMIT ?",
        (code, as_of, lookback),
    ).fetchall()
    if any(r[0] == r[1] for r in recent):
        return "4.6.6-oneword"

    window_start = conn.execute(
        "SELECT min(date) FROM (SELECT date FROM daily_bars WHERE code = ? AND date <= ?"
        " ORDER BY date DESC LIMIT 250)",
        (code, as_of),
    ).fetchone()[0]
    resume = conn.execute(
        "SELECT 1 FROM suspension_calendar WHERE code = ? AND end_date >= ? LIMIT 1",
        (code, window_start),
    ).fetchone()
    if resume is not None:
        return "4.6.6-resume"

    return None


def fmt(report) -> str:
    funnel = " ".join(f"{k}={v}" for k, v in report.funnel.items())
    return (
        f"candidates={report.candidates} size={report.size} "
        f"alerts={','.join(report.alerts) or '-'}\n  funnel: {funnel}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="观察池 vs. 全市场普查基线（Task 18 Step 5）")
    parser.add_argument("--as-of", type=int, required=True, help="交易日 YYYYMMDD")
    parser.add_argument("--market", default="cn_main")
    parser.add_argument("--db", default=os.environ.get("SS_DB_PATH", "/app/data/market.db"))
    parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    args = parser.parse_args(argv)

    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(f"基线清单不存在：{baseline_path}", file=sys.stderr)
        return 2
    baseline = load_baseline(baseline_path)
    if not baseline:
        print(
            f"基线清单为空：{baseline_path}\n"
            "  这份文件应当是从普查机器拷进来的 58 只代码（每行一个）。空清单时对拍没有意义，"
            "不写 0 差集冒充通过。",
            file=sys.stderr,
        )
        return 2

    cfg = load_config(Path("config"))
    conn: sqlite3.Connection = connect(Path(args.db))
    report = build_pool(conn, cfg, args.market, args.as_of)
    product = [
        r[0]
        for r in conn.execute(
            "SELECT code FROM pool WHERE trade_date = ? AND market = ? ORDER BY rank",
            (args.as_of, args.market),
        )
    ]

    print(f"== 产品池 {args.market} as_of {args.as_of} ==")
    print(f"  {fmt(report)}")

    baseline_only = [c for c in baseline if c not in set(product)]
    product_only = [c for c in product if c not in set(baseline)]

    unattributed = 0
    print(f"\n== 基线有、产品没有（{len(baseline_only)} 行）==")
    print("| code | stage | reason | rule_id |")
    print("|---|---|---|---|")
    for code in baseline_only:
        reject = report.rejects.get(code)
        rule = rule_of(reject)
        if rule is None:
            unattributed += 1
        stage, reason = reject if reject else ("-", "-")
        print(f"| {code} | {stage} | {reason} | {rule or 'UNATTRIBUTED'} |")

    print(f"\n== 产品有、基线没有（{len(product_only)} 行）==")
    print("| code | rank | score | rule_id |")
    print("|---|---|---|---|")
    for code in product_only:
        row = conn.execute(
            "SELECT rank, score FROM pool WHERE trade_date = ? AND market = ? AND code = ?",
            (args.as_of, args.market, code),
        ).fetchone()
        rule = product_only_rule(conn, cfg, code, args.as_of)
        if rule is None:
            unattributed += 1
        print(f"| {code} | {row[0]} | {row[1]:.2f} | {rule or 'UNATTRIBUTED'} |")

    print(f"\n无法归因 = {unattributed} 行（验收要求 0）")
    return 0 if unattributed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
