"""复权对拍：自算因子 vs 腾讯 `qfqday` 的前复权日收益。

**只做校验，不参与生产路径**。复权一律走本地自算的因子表（`core.adjust`），
`qfqday` 只在这里读一次，用来回答「本地因子算出来的日收益与上游前复权序列
是否一致」。除权日前后偏差超过阈值 ⇒ 因子算错（最常见的错法是漏了同日聚合，
或把 `f = ref/前收` 写反）。

腾讯的 `qfqday` 只回溯约 1211 根（§6.1.3），所以对拍窗口天然短于本地回填的 1700 根。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.adjust import verify_adj
from core.clock import SystemClock
from core.config import load_config
from core.db import connect, migrate
from core.datasource.tencent import KLINE_URL
from core.http import Http, RateLimiter
from core.models import Bar

# 日收益偏差超过这个值就算对不上（复权因子的 2 位小数舍入本身带来 ~1e-4 级噪声）
TOLERANCE = 5e-3


def fetch_qfq(http: Http, code: str, count: int = 1700) -> list[Bar]:
    """`fqkline` 的 `qfqday` 分支：列序与 `day` 相同，价格已前复权。"""
    text = http.get(f"{KLINE_URL}?param={code},day,,,{count},", scope="backfill")
    payload = json.loads(text)
    node = (payload.get("data") or {}).get(code) or {}
    rows = node.get("qfqday") or []
    if not rows:
        raise ValueError(f"{code} 没有 qfqday（腾讯前复权只回溯约 1211 根）")
    return [
        Bar(code, int(str(r[0]).replace("-", "")), float(r[1]), float(r[3]), float(r[4]),
            float(r[2]), float(r[5]) * 100, float(r[8]) * 10000, "tencent")
        for r in rows
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.verify_adj")
    parser.add_argument("--codes", default="sh600519,sz002594,sz000001",
                        help="逗号分隔的代码；默认取 spec §10 的三只对拍样本")
    args = parser.parse_args(argv)

    cfg = load_config(Path("config"))
    conn = connect()
    migrate(conn)
    http = Http({"tencent": RateLimiter(0.3)})

    worst = 0.0
    bad = 0
    for code in [c.strip() for c in args.codes.split(",") if c.strip()]:
        try:
            qfq = fetch_qfq(http, code)
        except Exception as exc:                       # noqa: BLE001 —— 对齐脚本要逐只报告，不中断
            print(f"{code}: 取 qfq 失败 {exc}")
            bad += 1
            continue
        diffs = verify_adj(conn, code, qfq)
        if not diffs:
            print(f"{code}: 本地 adj 与 qfq 无重叠区间，跳过")
            continue
        peak = max(diffs, key=lambda d: abs(d[1]))
        worst = max(worst, abs(peak[1]))
        flag = "OK" if abs(peak[1]) <= TOLERANCE else "MISMATCH"
        print(f"{code}: {len(diffs)} 个交易日，最大偏差 {peak[1]:+.2%} @ {peak[0]} {flag}")
        if flag != "OK":
            bad += 1

    print(f"worst={worst:.4%} bad={bad} at {SystemClock().now().isoformat()}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
