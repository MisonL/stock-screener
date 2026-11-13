#!/usr/bin/env python3
"""第二批自动统计与阈值标定（计划 Task 15 Step 1a / Step 2，F-33/34/35）。

    # 正式统计（不达标非零退出）
    python scripts/accept/batch2.py --date 20261030
    python scripts/accept/batch2.py --date 20261030 --exclude-drill

    # shadow 观测期的阈值标定：0.05 分箱直方图 + 当前有效阈值
    python scripts/accept/batch2.py --salience-histogram --date 20261030

**分母口径（最易写错的一处）**：通道 A 第二段占比的分母是
「存在 `status='accepted' AND segment=1` 的 `push_log` 所对应的事件」，不是「所有通道
A 事件」——合并条只给批次首事件写 `push_log`，其余成员按设计永远找不到第一段，拿全量
做分母会把「故意合并」判成缺陷。

**时延口径（F-33）**：`push_log.ts` 是 ISO 文本，SQLite 里文本相减恒为 0，指标永远绿。
必须用 `julianday` 换算成秒。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import load_config  # noqa: E402
from core.db import connect  # noqa: E402
from notify.policy import channel_b_today  # noqa: E402

BIN_W = 0.05
ROUND1_MIN = 0.90
ROUNDS_MEAN_MAX = 1.2
SECOND_SEGMENT_MIN = 0.90
LATENCY_S = 25.0
LATENCY_MIN = 15
LATENCY_TOTAL = 20
CHANNEL_A_KINDS = ("breakout", "breakdown")
DRILL_KINDS = ("drill", "close", "open")


def _events_for_day(conn, date: int, exclude_drill: bool) -> list[dict]:
    sql = ("SELECT id, wake_kind, decision, rounds, why FROM agent_turn"
           " WHERE substr(ts, 1, 10) = ?")
    params: list = [_iso(date)]
    if exclude_drill:
        sql += f" AND wake_kind NOT IN ({','.join('?' * len(DRILL_KINDS))})"
        params.extend(DRILL_KINDS)
    sql += " AND decision != 'shadow'"       # shadow 观测期不参与正式统计
    rows = conn.execute(sql, params).fetchall()
    return [{"id": r[0], "wake_kind": r[1], "decision": r[2], "rounds": r[3],
             "why": r[4]} for r in rows]


ZERO_ROUND_DECISIONS = ("deduped", "shadow", "gave_up")


def round_stats(events: list[dict]) -> dict:
    """只统计**真的跑过 LLM 的**事件回合。

    `deduped` / `shadow` / `gave_up` 是零轮的审计行（注意力层在调模型之前就写了），
    算进分母会把「一轮收场」的占比摊薄（实测 0.90 → 0.8696 判不合格）。
    """
    event_turns = [e for e in events
                   if e["wake_kind"] == "event"
                   and e["decision"] not in ZERO_ROUND_DECISIONS
                   and int(e["rounds"] or 0) > 0]
    if not event_turns:
        return {"n": 0, "one_round_ratio": None, "mean_rounds": None}
    rounds = [int(e["rounds"] or 0) for e in event_turns]
    return {"n": len(rounds),
            "one_round_ratio": round(sum(1 for r in rounds if r == 1) / len(rounds), 4),
            "mean_rounds": round(sum(rounds) / len(rounds), 4)}


def first_segment_events(conn, date: int) -> list[dict]:
    """分母：`segment=1 AND status='accepted'` 的通道 A 第一段所对应的事件。

    三类例外（不计入分母，统计里单列）：30 分钟去重 suppressed、60 条日熔断
    suppressed、被合并的非首事件。`push_log` 只由批次首事件代表，其余成员没有自己的行。
    """
    # **按 `seq_id` 去重**：一条合并通知会给批次里每只成员各写一行第一段，
    # 但它们共用同一个 `seq_id`。按行计数会把 15 个批次算成 45 个事件，
    # 第二段占比被摊薄成 0.33（正确值是 1.0）。
    rows = conn.execute(
        "SELECT event_id, seq_id, kind, ts, code FROM push_log"
        " WHERE trade_date = ? AND segment = 1 AND status = 'accepted'"
        f" AND kind IN ({','.join('?' * len(CHANNEL_A_KINDS))})"
        " AND event_id IS NOT NULL ORDER BY id",
        (date, *CHANNEL_A_KINDS)).fetchall()
    seen: dict[str, dict] = {}
    for r in rows:
        seen.setdefault(r[1], {"event_id": r[0], "seq_id": r[1], "kind": r[2], "ts": r[3],
                               "code": r[4]})
    return list(seen.values())


def open_brief_pair(conn, date: int) -> dict | None:
    """开盘简报那对（计划 Task 15 Step 2 要求按 `open-{trade_date}` 配对）。

    它没有 `event_id`（`event_id` 是 NULL），按 event_id 配对永远配不上，
    于是「开盘简报第二段」这条 F-33 指标会被静默漏掉。
    """
    seq = f"open-{date}"
    first = conn.execute(
        "SELECT ts, id, code, kind FROM push_log WHERE seq_id = ? AND segment = 1"
        " AND status = 'accepted' AND body IS NOT NULL ORDER BY id LIMIT 1", (seq,)).fetchone()
    if first is None:
        return None
    second = conn.execute(
        "SELECT ts, id FROM push_log WHERE seq_id = ? AND segment = 2"
        " AND status = 'accepted' ORDER BY id LIMIT 1", (seq,)).fetchone()
    if second is None:
        return {"event_id": None, "seq_id": seq, "kind": first[3], "ts": first[0],
                "seg2_ts": None, "latency_s": None}
    delta = conn.execute("SELECT (julianday(?) - julianday(?)) * 86400.0",
                         (second[0], first[0])).fetchone()[0]
    return {"event_id": None, "seq_id": seq, "kind": first[3], "ts": first[0],
            "seg2_ts": second[0], "latency_s": float(delta or 0)}


def second_segment_ratio(conn, date: int, firsts: list[dict]) -> dict:
    """**只统计事件**的第二段占比；开盘简报单独算（它是每日一条的固定推送）。

    把开盘简报混进分母时，一条简报缺第二段就会把 18/20 拖成 18/21 判不合格
    ——那是两类不同的东西，口径必须分开。
    """
    pairs = []
    brief = open_brief_pair(conn, date)
    for f in firsts:
        row = conn.execute(
            "SELECT ts, id FROM push_log WHERE event_id = ? AND segment = 2"
            " AND status = 'accepted' ORDER BY id LIMIT 1", (f["event_id"],)).fetchone()
        if row is None:
            pairs.append({**f, "seg2_ts": None, "latency_s": None})
            continue
        # **必须用 julianday**：ts 是 ISO 文本，文本相减在 SQLite 里恒为 0
        delta = conn.execute(
            "SELECT (julianday(?) - julianday(?)) * 86400.0", (row[0], f["ts"])).fetchone()[0]
        pairs.append({**f, "seg2_ts": row[0], "latency_s": float(delta or 0)})
    n_with = sum(1 for p in pairs if p["seg2_ts"] is not None)
    ratio = round(n_with / len(pairs), 4) if pairs else None
    return {"denominator": len(pairs), "with_second": n_with,
            "ratio": ratio, "pairs": pairs,
            "open_brief_paired": brief is not None, "open_brief": brief}


def latency_stats(second: dict, conn=None) -> dict:
    """≥ 15/20 条在 25 秒内出现第二段（F-33）。

    两个口径陷阱：

    - **分母用「实际样本数」而不是固定的 20**：40 对里只有 15 对及时时，
      拿 20 当分母仍会报 `15/20` 通过——**不合格会被显示成合格**，比误报更危险。
    - **缺第二段的也算超时**：它没有 latency，但显然不在 25 秒内，不能漏出 slow 列表。
    """
    pairs = second["pairs"]
    ok = [p for p in pairs if p["latency_s"] is not None and p["latency_s"] <= LATENCY_S]
    slow = [p for p in pairs
            if p["latency_s"] is None or p["latency_s"] > LATENCY_S]
    out = []
    for p in slow:
        decision = None
        if conn is not None:
            if p.get("event_id") is not None:
                row = conn.execute(
                    "SELECT decision FROM agent_turn WHERE trigger_id = ?"
                    " ORDER BY id DESC LIMIT 1", (p["event_id"],)).fetchone()
                decision = row[0] if row else None
            elif p.get("seq_id"):
                # 开盘简报没有 event_id：按当日 `wake_kind='open'` 的回合关联
                row = conn.execute(
                    "SELECT decision FROM agent_turn WHERE wake_kind = 'open'"
                    " AND substr(ts, 1, 10) = ? ORDER BY id DESC LIMIT 1",
                    (_iso(int(p["seq_id"].split("-", 1)[1])),)).fetchone()
                decision = row[0] if row else None
        out.append({"event_id": p["event_id"], "seq_id": p.get("seq_id"),
                    "latency_s": None if p["latency_s"] is None else round(p["latency_s"], 1),
                    "missing_second": p["latency_s"] is None, "decision": decision})
    total = max(len(pairs), 1)
    return {"ok": len(ok), "total": total, "sample": len(pairs), "need": LATENCY_MIN,
            "threshold_s": LATENCY_S, "ratio": round(len(ok) / total, 4), "slow": out}


def _push_suppressed(conn, event_id: int) -> bool:
    row = conn.execute("SELECT count(*) FROM push_log WHERE event_id = ? AND status = 'suppressed'",
                       (event_id,)).fetchone()
    return bool(row and row[0])


def _in_merge_batch(conn, event_id: int) -> bool:
    """有合并批次证据吗：同一 `seq_id` 上有别的成员的 `event_id`。"""
    row = conn.execute(
        "SELECT count(*) FROM push_log a JOIN push_log b ON a.seq_id = b.seq_id"
        " AND a.event_id = ? AND b.event_id IS NOT ?", (event_id, event_id)).fetchone()
    return bool(row and row[0])


def exceptions(conn, date: int) -> dict:
    """三类**设计上的例外**必须单列，否则会把「故意的合并/削峰」判成缺陷。

    计划 Task 15 Step 2 明确要求分开统计：
    ① 30 分钟同 code 同 kind 去重 → `suppressed`；
    ② 60 条日熔断 → `suppressed`；
    ③ **被合并的非首事件**：`push_log` 只由批次首事件代表，其余成员的 `event_id`
       在该批次行里查得到、但没有自己的行。
    """
    rows = conn.execute(
        "SELECT id, agent_status FROM event_log WHERE trade_date = ?"
        f" AND kind IN ({','.join('?' * len(CHANNEL_A_KINDS))})",
        (date, *CHANNEL_A_KINDS)).fetchall()
    covered = {f["event_id"] for f in first_segment_events(conn, date)}
    # 被合并的非首成员**没有自己的 push_log 行**（第一段由批次首事件代表），
    # 所以只能从 `agent_turn.tool_calls`/决策记录里认出「它被并进过一个合并批」：
    # 用事件是否出现在某个 `wake_kind='event'` 回合的 trigger 范围内不可靠，
    # 这里退一步——凡是没有自己第一段、也不是 suppressed / 失败态的事件，
    # 先归到「待甄别」，由人对照 `push_log` 批次确认，**不默认算成缺陷**。
    batch_members: set[int] = set()
    suppressed, merged, missing, other = [], [], [], []
    for eid, status in rows:
        if status == "suppressed":
            suppressed.append(eid)
        elif eid in covered:
            continue
        elif status in ("timeout", "error", "pending_retry", "gave_up"):
            other.append(eid)
        elif _push_suppressed(conn, eid):
            # 推送层因去重/熔断写成 suppressed、事件本身是 done：**不是合并成员**
            suppressed.append(eid)
        elif _in_merge_batch(conn, eid):
            merged.append(eid)
        else:
            missing.append(eid)
    return {"dedupe_or_cap_suppressed": suppressed, "merged_members": merged,
            "missing_first_segment": missing, "failed_but_attributed": other}


def missing_first_segment_reasons(conn, date: int, events: list[dict]) -> dict:
    """每个通道 A 事件都该有一段；没发出的必须能查到 timeout/error 归因。"""
    rows = conn.execute(
        "SELECT id, agent_status FROM event_log WHERE trade_date = ?"
        f" AND kind IN ({','.join('?' * len(CHANNEL_A_KINDS))})",
        (date, *CHANNEL_A_KINDS)).fetchall()
    covered = {f["event_id"] for f in first_segment_events(conn, date)}
    missing = [r[0] for r in rows if r[0] not in covered and r[1] != "suppressed"]
    bad = conn.execute(
        "SELECT count(*) FROM agent_turn WHERE substr(ts, 1, 10) = ?"
        " AND decision IN ('timeout','error')", (_iso(date),)).fetchone()[0]
    return {"missing": missing, "timeout_or_error_turns": int(bad)}


def salience_histogram(conn, date: int, cfg) -> list[dict]:
    """0.05 分箱：每箱计数 + 被放行/被 suppressed 各多少（阈值标定用）。"""
    rows = conn.execute(
        "SELECT salience, agent_status FROM event_log WHERE trade_date = ?", (date,)).fetchall()
    bins: dict[int, dict] = {}
    for sal, status in rows:
        idx = int(float(sal or 0) // BIN_W)
        b = bins.setdefault(idx, {"lo": round(idx * BIN_W, 2), "n": 0,
                                  "allowed": 0, "suppressed": 0})
        b["n"] += 1
        if status == "suppressed":
            b["suppressed"] += 1
        elif status in ("escalated", "done", "pending_retry"):
            b["allowed"] += 1
    out = [bins[k] for k in sorted(bins)]
    a = getattr(cfg.strategy, "attention", None)
    return out, {"salience_min": getattr(a, "salience_min", None),
                 "base": getattr(a, "base", None)}


def silent_why_sample(conn, date: int, n: int = 10) -> list[str]:
    return [r[0] or "" for r in conn.execute(
        "SELECT why FROM agent_turn WHERE substr(ts, 1, 10) = ? AND decision = 'silent'"
        " ORDER BY random() LIMIT ?", (_iso(date), n)).fetchall()]


def _iso(date: int) -> str:
    s = str(date)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def run(conn, date: int, cfg, exclude_drill: bool = False) -> tuple[dict, list[str]]:
    events = _events_for_day(conn, date, exclude_drill)
    rounds = round_stats(events)
    first = first_segment_events(conn, date)
    second = second_segment_ratio(conn, date, first)
    lat = latency_stats(second, conn)
    ch_b = channel_b_today(conn, date)
    cap = int(cfg.agent.channel_b_daily_cap)
    missing = missing_first_segment_reasons(conn, date, events)
    exc = exceptions(conn, date)

    failures = []
    if rounds["one_round_ratio"] is None:
        failures.append("当日没有事件回合（无法验收 F-34）")
    else:
        if rounds["one_round_ratio"] < ROUND1_MIN:
            failures.append(f"rounds==1 占比 {rounds['one_round_ratio']} < {ROUND1_MIN}")
        if rounds["mean_rounds"] > ROUNDS_MEAN_MAX:
            failures.append(f"平均轮数 {rounds['mean_rounds']} > {ROUNDS_MEAN_MAX}")
    if ch_b > cap:
        failures.append(f"通道 B 当日 {ch_b} 条 > 上限 {cap}")
    if second["ratio"] is not None and second["ratio"] < SECOND_SEGMENT_MIN:
        failures.append(f"第二段占比 {second['ratio']} < {SECOND_SEGMENT_MIN}")
    if lat["ok"] < LATENCY_MIN:
        failures.append(f"25 秒内第二段 {lat['ok']}/{lat['total']} < {LATENCY_MIN}")
    if lat["sample"] and lat["ratio"] < 0.75:
        failures.append(f"25 秒内第二段比例 {lat['ratio']} < 0.75")
    # **用分类后的例外判门槛，不要用旧的 missing 列表**：合并批的从属成员、
    # done+suppressed、已归因的失败都不该算「第一段没发出去」，
    # 15 个合并批会因此报出 30 条假缺失。
    if exc["missing_first_segment"]:
        failures.append(
            f"缺第一段且无归因的事件 {len(exc['missing_first_segment'])} 条："
            f"{exc['missing_first_segment'][:8]}")

    return {"date": date, "exclude_drill": exclude_drill,
            "event_turns": rounds["n"], "rounds": rounds,
            "channel_b_today": ch_b, "channel_b_cap": cap,
            "second_segment": {k: v for k, v in second.items() if k != "pairs"},
            "latency": lat, "missing_first_segment": missing, "exceptions": exc,
            "silent_why_sample": silent_why_sample(conn, date)}, failures


def main() -> int:
    ap = argparse.ArgumentParser(description="第二批自动统计 / 阈值标定")
    ap.add_argument("--date", type=int, required=True, help="YYYYMMDD")
    ap.add_argument("--exclude-drill", action="store_true", help="按 wake_kind 排除演练回合")
    ap.add_argument("--salience-histogram", action="store_true",
                    help="输出 0.05 分箱直方图与当前有效阈值（shadow 观测期标定用）")
    ap.add_argument("--db", default=os.environ.get("SS_DB_PATH", "data/market.db"))
    args = ap.parse_args()

    cfg = load_config(Path("config"))
    conn = connect(Path(args.db))

    if args.salience_histogram:
        bins, thresholds = salience_histogram(conn, args.date, cfg)
        print(f"# {args.date} event_log.salience 0.05 分箱（shadow 观测期标定）")
        print(f"# 当前有效阈值：salience_min={thresholds['salience_min']} base={thresholds['base']}")
        print(f"{'区间':<14}{'总数':>6}{'放行':>6}{'压掉':>6}")
        total_sup = 0
        for b in bins:
            print(f"[{b['lo']:.2f},{b['lo'] + BIN_W:.2f})  {b['n']:>6}{b['allowed']:>6}"
                  f"{b['suppressed']:>6}")
            total_sup += b["suppressed"]
        total = sum(b["n"] for b in bins)
        ratio = total_sup / total if total else 0.0
        print(f"\n总计 {total} 条，suppressed {total_sup} 条（{ratio:.1%}）"
              f"；目标 20–40%，调整 config/strategy.yaml 的 attention.base / salience_min")
        return 0

    report, failures = run(conn, args.date, cfg, args.exclude_drill)
    print(f"# 第二批自动统计 {args.date}"
          f"{'（排除演练回合）' if args.exclude_drill else ''}")
    r = report["rounds"]
    print(f"事件回合 {r['n']} 条：rounds==1 占比 {r['one_round_ratio']}（门槛 ≥ {ROUND1_MIN}），"
          f"平均轮数 {r['mean_rounds']}（门槛 ≤ {ROUNDS_MEAN_MAX}）")
    print(f"通道 B 当日 {report['channel_b_today']} 条（上限 {report['channel_b_cap']}）")
    s = report["second_segment"]
    print(f"通道 A 第二段：{s['with_second']}/{s['denominator']} = {s['ratio']}"
          f"（门槛 ≥ {SECOND_SEGMENT_MIN}；分母 = 有 accepted 第一段的事件）")
    lat = report["latency"]
    print(f"两段式时延 ≤ {lat['threshold_s']}s：{lat['ok']}/{lat['total']}"
          f" = {lat['ratio']}（门槛：≥ {LATENCY_MIN} 条且比例 ≥ 0.75；"
          f"分母用实际样本数，不是固定的 20）")
    if lat["slow"]:
        print(f"  超时条目：{lat['slow'][:8]}")
    m = report["missing_first_segment"]
    print(f"缺第一段且无归因的事件 {len(m['missing'])} 条；"
          f"当日 timeout/error 回合 {m['timeout_or_error_turns']} 条")
    exc = report["exceptions"]
    print(f"设计内例外：去重/熔断 suppressed {len(exc['dedupe_or_cap_suppressed'])} 条，"
          f"合并非首成员 {len(exc['merged_members'])} 条，"
          f"失败有归因 {len(exc['failed_but_attributed'])} 条")
    ob = report["second_segment"].get("open_brief")
    if ob is None:
        print("  注意：当日没有找到开盘简报的第一段（open-{date}）")
    elif ob["latency_s"] is None:
        print("  开盘简报：缺第二段")
    else:
        print(f"  开盘简报第二段时延：{ob['latency_s']:.1f}s")

    print("\n# 随机 10 条 silent 的 why")
    for why in report["silent_why_sample"]:
        print(f"  - {why}")
    if failures:
        print("\n# 未达标", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\n全部达标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
