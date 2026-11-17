"""首次全量回填：逐只拉 1700 根 raw 日线，按 200 只一批单事务写入。

**回填与 `jobs` 不同时运行**（D18）：两者共用同一条上游限速出口，
执行前必须 `docker compose stop jobs`。

断点续跑的判据是 `fetch_log.status == 'ok'`，**不是 `n >= count`**：
2019-10 之后上市的新股只有 ~1458 根，按根数判会把大半市场每次重拉。
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.adjust import rebuild_adj
from core.clock import SH
from core.datasource.sina import SinaSource
from core.datasource.tencent import TencentSource
from core.http import ForbiddenHost, UpstreamBlocked, UpstreamError, UpstreamTimeout
# 交易日历的权威来源是 sh000300（§6.4）；其余四只是 intraday.index_move 的判定对象。
INDICES = ("sh000300", "sh000001", "sz399001", "sz399006", "sh000688")

BATCH = 200
RETRIES = 3
# 连续这么多只**首轮就失败** → 判定上游整体不可用，停止本轮。
# spec:2481 记录过 WAF 封禁「不是 60 秒能恢复的，代价是整条 K 线链路不可用」，
# 继续逐只重试 3 次等于对已封禁的 IP 连打（5581 只 × 3 = 16743 次请求）。
BREAKER_THRESHOLD = 5
# 可恢复错误的退避：0.5s → 1s（配合 300ms 限速，实际间隔 ≥0.8s/1.3s）
BACKOFF_S = (0.5, 1.0)
HEALTH_FILE = Path("/tmp/jobs_healthy")


@dataclass
class BackfillReport:
    done: int = 0
    empty: int = 0
    failed: int = 0
    skipped: int = 0
    bars_written: int = 0
    gap_unfilled: int = 0       # 有待补记录但目标日仍未补到（marker 保留，下次继续）
    aborted: bool = False       # 连续失败触发熔断，本轮提前结束


def backfill(cfg, http, conn, codes: list[str] | None = None, count: int = 1700,
             sleep_s: float = 0.3, resume: bool = True) -> BackfillReport:
    tencent = TencentSource(http)
    sina = SinaSource(http)
    names: dict[str, str] = {}

    if codes is None:
        universe = sina.fetch_universe(scope="backfill")
        names = dict(universe)
        codes = [code for code, _ in universe] + list(INDICES)

    # **待补缺口优先于「曾经成功」**：`gap_pending` 里的票正是「backfill 曾经 ok、
    # 但 eod 发现缺了几天」的情形，只看 fetch_log 会把它们永远跳过。
    pending = {row[0] for row in conn.execute("SELECT code FROM gap_pending")}
    done_codes = set()
    if resume:
        done_codes = {
            row[0]
            for row in conn.execute("SELECT code FROM fetch_log WHERE purpose='backfill' AND status='ok'")
        }

    report = BackfillReport()
    consec_fail = 0
    total = len(codes)
    for index, code in enumerate(codes, start=1):
        if code in done_codes and code not in pending:
            report.skipped += 1
            continue

        bars = _fetch(tencent, code, count)
        if bars is None:
            report.failed += 1
            _log(conn, code, 0, "error")
            consec_fail += 1
            if consec_fail >= BREAKER_THRESHOLD:
                # 熔断：连续多只首轮失败说明上游整体不可用，继续跑只会浪费配额
                report.aborted = True
                print(f"⚠️ 连续 {consec_fail} 只失败，判定上游不可用，本轮中止"
                      f"（已完成 {report.done}、失败 {report.failed}）", flush=True)
                break
            continue
        consec_fail = 0

        kept = _drop_placeholders(bars)
        if not kept:
            report.empty += 1
            _log(conn, code, 0, "empty")
            continue

        _write(conn, code, kept)
        _upsert_meta(conn, code, names.get(code, code))
        report.bars_written += len(kept)
        report.done += 1
        _log(conn, code, len(kept), "ok")
        if code in pending:
            # **确认目标日真的补到了才清**：上游"成功"但只回更早的旧日线时，
            # 按 code 直接删会让缺口失去续跑入口，而 `_log(ok)` 又让下次续跑跳过它。
            target = conn.execute(
                "SELECT missing_through FROM gap_pending WHERE code = ?", (code,)).fetchone()
            filled = (target is None) or conn.execute(
                "SELECT 1 FROM daily_bars WHERE code = ? AND date = ? LIMIT 1",
                (code, int(target[0]))).fetchone() is not None
            if filled:
                conn.execute("DELETE FROM gap_pending WHERE code = ?", (code,))
                conn.commit()
            else:
                report.gap_unfilled += 1

        if index % BATCH == 0:
            HEALTH_FILE.touch()
            print(f"已完成 {index}/{total}、跳过 {report.skipped}、失败 {report.failed}", flush=True)

    rebuild_adj(conn)
    return report


def _fetch(tencent: TencentSource, code: str, count: int):
    """拉一只票的日线。

    **不可恢复的错误立即放弃**（`ForbiddenHost` 是配置错、`UpstreamBlocked` 是 403/WAF）：
    同一秒内重试 3 次几乎必然全挂，还对已封禁的 IP 连打。
    **可恢复的**（超时、连接）按 `BACKOFF_S` 退避后再试。
    """
    for attempt in range(RETRIES):
        try:
            return tencent.fetch_kline_daily(code, count=count, scope="backfill")
        except (ForbiddenHost, UpstreamBlocked):
            return None                      # 重试救不回来
        except (UpstreamError, UpstreamTimeout):
            if attempt < len(BACKOFF_S):
                time.sleep(BACKOFF_S[attempt])
            continue
    return None


def _drop_placeholders(bars):
    """回填口径：`today=bar.date`（结果不随执行日期漂移），`now=None`（历史 bar 无时间门）。"""
    return [
        bar
        for i, bar in enumerate(bars)
        if not bar.is_placeholder(bar.date, prev_close=bars[i - 1].close if i else None)
    ]


def _write(conn, code: str, bars) -> None:
    for start in range(0, len(bars), BATCH):
        part = bars[start:start + BATCH]
        with conn:                       # 每批一个事务
            conn.executemany(
                "INSERT OR REPLACE INTO daily_bars(code,date,open,high,low,close,volume,amount,source) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(b.code, b.date, b.open, b.high, b.low, b.close, b.volume, b.amount, b.source) for b in part],
            )


def _upsert_meta(conn, code: str, name: str) -> None:
    from core.market import classify_market

    conn.execute(
        "INSERT INTO stock_meta(code, name, market, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market, updated_at=excluded.updated_at",
        (code, name, classify_market(code) or "", datetime.now(SH).isoformat()),
    )
    conn.commit()


def _log(conn, code: str, n: int, status: str) -> None:
    conn.execute(
        "INSERT INTO fetch_log(code, purpose, n, status, ts) VALUES (?,?,?,?,?) "
        "ON CONFLICT(code, purpose) DO UPDATE SET n=excluded.n, status=excluded.status, ts=excluded.ts",
        (code, "backfill", n, status, datetime.now(SH).isoformat()),
    )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    from core.clock import SystemClock
    from core.config import load_config
    from core.db import connect, migrate
    from core.http import Http, RateLimiter

    parser = argparse.ArgumentParser(prog="python -m jobs.backfill")
    parser.add_argument("--codes-file", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--count", type=int, default=1700)
    args = parser.parse_args(argv)

    cfg = load_config(Path("config"))
    conn = connect()
    migrate(conn)
    # 300ms 串行出口：回填进程与 jobs 互斥（D18），必须与 jobs 用同一档限速
    from jobs.snapshot import make_recorder
    from core.clock import SystemClock

    http = Http({"tencent": RateLimiter(0.3), "sina": RateLimiter(0.3)},
                on_request=make_recorder(conn, SystemClock()))

    codes = None
    if args.codes_file:
        codes = [line.strip() for line in args.codes_file.read_text().splitlines() if line.strip()]
    elif args.limit:
        codes = [code for code, _ in SinaSource(http).fetch_universe()][: args.limit]

    report = backfill(cfg, http, conn, codes=codes, count=args.count, resume=not args.no_resume)
    print(
        f"done={report.done} empty={report.empty} failed={report.failed} "
        f"skipped={report.skipped} bars={report.bars_written} at {SystemClock().now().isoformat()}",
        flush=True,
    )
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
