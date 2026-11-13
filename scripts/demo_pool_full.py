#!/usr/bin/env python3
"""
Demo ②：全市场真实日线跑完整选池漏斗
数据源：腾讯 proxy.finance.qq.com raw（不复权，设计文档主源）
⚠️ 本脚本用 600 只抽样并外推全市场。2026-10-31 全量普查证明
  该外推「≈97 只」系统性偏高（真值 58 只，命中票在 code 序上聚集、
  random.sample 依赖输入列表顺序）。这里只用于演示漏斗结构与
  复现抽样偏差，池子规模请以全量普查为准（见设计文档 §4.2 更正块）。
"""
import json
import time
import subprocess
import random
import csv
from datetime import datetime

TENCENT = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
           "newfqkline/get?param={code},day,,,{n},")
BARS = 400
SAMPLE = 600
MIN_AMOUNT = 5000_0000        # 5000 万
BOX_MIN_N, BOX_MAX_N = 10, 40
BOX_RANGE_T = 0.15            # 硬门槛
DAILY_AMP_T = 0.03
SLOPE_T = 0.0015              # 0.15%/day
B4_HARD = 0.75
B8_BUFFER = 0.98
DRAWDOWN_MIN = 0.20
A2_T = 0.08


def fetch_raw(code):
    url = TENCENT.format(code=code, n=BARS)
    for attempt in range(3):
        r = subprocess.run(["curl", "-s", "--max-time", "20", url],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout:
            try:
                d = json.loads(r.stdout)
                node = d["data"][code]
                arr = node.get("day") or node.get("qfqday")
                if arr:
                    return [{
                        "date": b[0],
                        "open": float(b[1]),
                        "close": float(b[2]),
                        "high": float(b[3]),
                        "low": float(b[4]),
                        "vol": float(b[5]),
                        # 🔴 腾讯 raw 第 9 列 amount 单位是「万元」，不是元
                        #    实测茅台 2026-10-30: 825738.18 → 82.57 亿元
                        "amount": float(b[8]) * 10000 if len(b) > 8 and b[8] else 0.0,
                    } for b in arr]
            except Exception:
                pass
        time.sleep(0.5)
    return None


def ma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def slope_pct(vals):
    """OLS 斜率 / 均值"""
    n = len(vals)
    if n < 5:
        return None
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(vals) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, vals))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return (num / den) / my if my else None


def find_box(bars):
    """滑动搜索最长有效箱体，返回 (N, box_low, box_high, box_start_idx) 或 None"""
    n_total = len(bars)
    best = None
    for N in range(BOX_MAX_N, BOX_MIN_N - 1, -1):
        if n_total < N + 1 + 60:
            continue
        win = bars[-(N + 1):-1]          # 不含今天
        highs = [b["high"] for b in win]
        lows = [b["low"] for b in win]
        closes = [b["close"] for b in win]
        box_low, box_high = min(lows), max(highs)
        if box_low <= 0:
            continue
        # (a) 整体振幅
        if (box_high - box_low) / box_low > BOX_RANGE_T:
            continue
        # (b) 单日振幅中位数
        amps = sorted((b["high"] - b["low"]) / b["close"] for b in win)
        if amps[len(amps) // 2] > DAILY_AMP_T:
            continue
        # (c) 斜率
        s = slope_pct(closes)
        if s is None or abs(s) > SLOPE_T:
            continue
        # (d) 今天仍在箱体内/刚突破
        today = bars[-1]["close"]
        if today < box_low * 0.98 or today > box_high * 1.07:
            continue
        best = (N, box_low, box_high, n_total - 1 - N)
        break
    return best


def b4_ratio(bars, box_start_idx):
    """横盘期均量 / 下跌期均量（下跌期取箱体前最多 60 日）"""
    box_vols = [b["vol"] for b in bars[box_start_idx:-1]]
    decl = bars[max(0, box_start_idx - 60):box_start_idx]
    if not decl or not box_vols:
        return None
    dv = sum(b["vol"] for b in decl) / len(decl)
    if dv <= 0:
        return None
    return (sum(box_vols) / len(box_vols)) / dv


def main():
    codes = [l.strip() for l in open("/tmp/screener_demo/mainboard.txt")]
    random.seed(20261031)
    sample = random.sample(codes, min(SAMPLE, len(codes)))
    print(f"Demo ②  样本 {len(sample)} 只主板，日线 {BARS} 根（腾讯 raw）\n")

    funnel = {"total": 0, "bars_ok": 0, "amount_ok": 0, "box": 0,
              "B8": 0, "B4": 0, "Agate": 0, "AandB": 0}
    passed = []
    failed_fetch = []

    t0 = time.time()
    for i, code in enumerate(sample):
        bars = fetch_raw(code)
        time.sleep(0.3)                 # 限速纪律
        if not bars or len(bars) < 265:
            failed_fetch.append(code)
            continue
        funnel["total"] += 1
        funnel["bars_ok"] += 1

        last = bars[-1]
        if last["amount"] < MIN_AMOUNT:
            continue
        funnel["amount_ok"] += 1

        box = find_box(bars)
        if not box:
            continue
        funnel["box"] += 1
        N, box_low, box_high, bstart = box

        # B8 不创新低
        decl = bars[max(0, bstart - 60):bstart]
        if decl:
            decl_low = min(b["low"] for b in decl)
            box_low_actual = min(b["low"] for b in bars[bstart:-1])
            if box_low_actual <= decl_low * B8_BUFFER:
                continue
        funnel["B8"] += 1

        # B4 缩量
        r = b4_ratio(bars, bstart)
        b4ok = (r is not None and r <= B4_HARD)
        if b4ok:
            funnel["B4"] += 1

        # A 组
        closes = [b["close"] for b in bars]
        cur = closes[-1]
        high250 = max(b["high"] for b in bars[-250:])
        dd = (high250 - cur) / high250 if high250 else 0
        m60 = ma(closes, 60)
        a2 = abs(cur - m60) / m60 if m60 else 9
        m60_series = []
        for k in range(20):
            idx = len(closes) - 20 + k
            mm = sum(closes[idx - 59:idx + 1]) / 60 if idx >= 59 else None
            if mm:
                m60_series.append(mm)
        a3 = slope_pct(m60_series) if len(m60_series) >= 5 else None
        a_ok = (dd >= DRAWDOWN_MIN and a2 <= A2_T and a3 is not None and a3 <= 0)
        if a_ok:
            funnel["Agate"] += 1
        if a_ok and b4ok:
            funnel["AandB"] += 1
            passed.append({
                "code": code, "date": last["date"], "close": cur,
                "drawdown": f"{dd*100:.1f}%", "a2": f"{a2*100:.1f}%",
                "N": N, "box_low": box_low, "box_high": box_high,
                "b4": f"{r:.2f}" if r else "—",
            })

        if (i + 1) % 50 == 0:
            el = time.time() - t0
            print(f"  {i+1}/{len(sample)}  已用 {el:.0f}s  命中 {funnel['AandB']}")

    print("\n" + "=" * 74)
    print("漏斗（真实数据，非外推）")
    print("=" * 74)
    tot = funnel["total"]
    for k, label in [("bars_ok", "有足够 bar(≥265)"), ("amount_ok", "成交额≥5000万"),
                     ("box", "找到合格箱体"), ("B8", "B8 未创新低"),
                     ("B4", "B4 缩量≤0.75"), ("Agate", "A 组三项全过"),
                     ("AandB", "A组∧B组 都过")]:
        v = funnel[k]
        print(f"  {label:<20} {v:>4}只  ({v/tot*100:>5.1f}%)")
    print(f"\n  抓取失败/bar 不足: {len(failed_fetch)} 只")

    # 外推
    MAINBOARD = 3194
    if tot:
        p = funnel["AandB"] / tot
        lo, hi = wilson(funnel["AandB"], tot)
        print("\n" + "=" * 74)
        print(f"外推全主板（{MAINBOARD} 只）")
        print("=" * 74)
        print(f"  点估计: {p*MAINBOARD:.0f} 只  ({p*100:.1f}%)")
        print(f"  95%CI : [{lo*MAINBOARD:.0f}, {hi*MAINBOARD:.0f}] 只")
        print(f"  → 是否落入 30–100: 点估计 {'✅' if 30<=p*MAINBOARD<=100 else '❌'}，"
              f"CI 下界 {'✅' if lo*MAINBOARD>=30 else '❌'}")

    print("\n通过 A组∧B组 的样本（前 20）:")
    print(f"{'code':<10}{'close':>9}{'回撤':>8}{'|距MA60|':>9}{'N':>4}{'箱体下':>9}{'箱体上':>9}{'B4':>6}")
    for p in passed[:20]:
        print(f"{p['code']:<10}{p['close']:>9.2f}{p['drawdown']:>8}{p['a2']:>9}"
              f"{p['N']:>4}{p['box_low']:>9.2f}{p['box_high']:>9.2f}{p['b4']:>6}")

    with open("/tmp/screener_demo/pool_result.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(passed[0].keys()) if passed else ["code"])
        w.writeheader()
        w.writerows(passed)
    print("\n结果已存 /tmp/screener_demo/pool_result.csv")


def wilson(k, n, z=1.96):
    if n == 0:
        return 0, 0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** .5) / d
    return max(0, c - h), min(1, c + h)


if __name__ == "__main__":
    main()
