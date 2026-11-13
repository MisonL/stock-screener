#!/usr/bin/env python3
"""
Demo ③：端到端一天回放——箱体(截至 T-1) + 真实 m5(T 日) → C1/C2 判定
核心问题：用「设计曲线」和用「实测曲线」判 C2，结论会不会不同？会不会造成假阳性？
"""
import os
import json, subprocess, time, csv
from datetime import datetime

TENCENT = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
           "newfqkline/get?param={c},day,,,{n},")
WESTOCK = os.environ.get("WESTOCK_BIN", "westock")
TMP = "/tmp/screener_demo/_m5.txt"

BOX_RANGE_T = 0.15
DAILY_AMP_T = 0.03
SLOPE_T = 0.0015
C1_BUFFER = 0.005
C2_TH = 1.5

# 设计曲线（§4.6.8）vs 实测曲线（Demo ① 结论）
DESIGN = {"09:45": 12.0, "10:00": 20.0, "10:30": 33.0, "11:00": 43.0,
          "11:30": 50.0, "13:30": 63.0, "14:00": 73.0, "14:30": 84.0}
MEASURED = {"09:45": 19.0, "10:00": 29.0, "10:30": 42.0, "11:00": 50.0,
            "11:30": 58.0, "13:30": 69.0, "14:00": 77.0, "14:30": 86.0}


def fetch_day(code, n=400):
    r = subprocess.run(["curl", "-s", "--max-time", "20",
                        TENCENT.format(c=code, n=n)], capture_output=True, text=True)
    try:
        arr = json.loads(r.stdout)["data"][code]["day"]
    except Exception:
        return None
    return [{"date": b[0], "open": float(b[1]), "close": float(b[2]),
             "high": float(b[3]), "low": float(b[4]), "vol": float(b[5])} for b in arr]


def slope_pct(v):
    n = len(v); xs = list(range(n)); mx = sum(xs) / n; my = sum(v) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, v))
    den = sum((x - mx) ** 2 for x in xs)
    return (num / den) / my if my else None


def find_box(bars):
    """以 bars[-1] 为 T-1（即 T 日前一天），找截至它的箱体"""
    n_total = len(bars)
    for N in range(40, 9, -1):
        if n_total < N + 1 + 60:
            continue
        win = bars[-(N + 1):-1]
        hl = [b["high"] for b in win]; lo = [b["low"] for b in win]
        cl = [b["close"] for b in win]
        low, high = min(lo), max(hl)
        if low <= 0 or (high - low) / low > BOX_RANGE_T:
            continue
        amps = sorted((b["high"] - b["low"]) / b["close"] for b in win)
        if amps[len(amps) // 2] > DAILY_AMP_T:
            continue
        s = slope_pct(cl)
        if s is None or abs(s) > SLOPE_T:
            continue
        return {"N": N, "low": low, "high": high, "start": n_total - 1 - N,
                "today": bars[-1]["close"]}
    return None


def fetch_m5(code, day):
    subprocess.run(f"{WESTOCK} kline {code} --period m5 --start {day} --end {day} "
                   f"> {TMP} 2>/dev/null", shell=True)
    time.sleep(0.35)
    lines = open(TMP).read().split("\n")
    hi = next((i for i, l in enumerate(lines)
               if l.startswith("| code | date |") or l.startswith("| date |")), None)
    if hi is None:
        return []
    bars = []
    for row in csv.DictReader(lines[hi:], delimiter="|"):
        d = {k.strip(): (v.strip() if v else "") for k, v in row.items() if k}
        dt = d.get("date") or ""
        if not dt or dt == "---":
            continue
        try:
            bars.append({"dt": datetime.strptime(dt, "%Y-%m-%d %H:%M:%S"),
                         "high": float(d.get("high") or 0),
                         "close": float(d.get("last") or 0),
                         "vol": float(d.get("volume") or 0)})
        except ValueError:
            pass
    bars.sort(key=lambda x: x["dt"])
    return bars


def curve_at(t, curve):
    """取 <=t 的最近曲线点，线性插值"""
    keys = sorted(curve)
    hm = f"{t.hour:02d}:{t.minute:02d}"
    if hm in curve:
        return curve[hm]
    # 找前后
    prev = None
    for k in keys:
        if k <= hm:
            prev = k
        else:
            return curve[k] if prev is None else curve[prev]
    return curve[keys[-1]]


def replay(code, box, bars_t1, day):
    """逐 5 分钟推进，判定 C1/C2"""
    box_high = box["high"]
    # 横盘期均量（T-1 之前的箱体窗口）
    box_bars = bars_t1[box["start"]:len(bars_t1) - 1]
    box_avg = sum(b["vol"] for b in box_bars) / len(box_bars)
    if box_avg <= 0:
        return []

    m5 = fetch_m5(code, day)
    if not m5:
        return []

    events = []
    cum = 0.0
    for bar in m5:
        cum += bar["vol"]
        t = bar["dt"]
        # C1: 现价 > 箱体上沿 × 1.005（用截至该 bar 的最高价判是否曾突破）
        if bar["high"] > box_high * (1 + C1_BUFFER):
            c1 = True
        else:
            continue
        # C2: 折算全天量 / 横盘均量
        sh_d = curve_at(t, DESIGN) / 100
        sh_m = curve_at(t, MEASURED) / 100
        proj_d = cum / sh_d if sh_d else 0
        proj_m = cum / sh_m if sh_m else 0
        c2_d = proj_d / box_avg
        c2_m = proj_m / box_avg
        events.append({
            "time": f"{t.hour:02d}:{t.minute:02d}",
            "price": bar["high"], "box_high": box_high,
            "cum_vol": cum, "box_avg": box_avg,
            "c2_design": c2_d, "c2_measured": c2_m,
            "verdict_design": "触发" if c2_d >= C2_TH else "不触发",
            "verdict_measured": "触发" if c2_m >= C2_TH else "不触发",
        })
    return events


def main():
    # 用 Demo ② 的池子作为候选（这些票在 T-1 时已满足箱体条件）
    pool = list(csv.DictReader(open("/tmp/screener_demo/pool_result.csv")))
    print(f"候选池：{len(pool)} 只（来自 Demo ② 的 A组∧B组 结果）\n")

    # 回放日选择：用 m5 能覆盖的最近交易日
    DAY = "2026-10-30"
    print(f"回放日：{DAY}（真实 m5 分钟线）")
    print(f"C2 阈值 {C2_TH}，C1 缓冲 {C1_BUFFER*100}%\n")
    print("=" * 96)
    print(f"{'code':<10}{'时刻':<8}{'突破价':>9}{'箱上沿':>9}{'累计量':>10}"
          f"{'设计C2':>9}{'实测C2':>9}{'设计判':>7}{'实测判':>7}{'结论是否一致'}")
    print("-" * 96)

    total_events = 0
    mismatch = 0
    for p in pool:
        code = p["code"]
        # 取截至 DAY-1 的日线（m5 是 DAY 的，所以日线最后一天应是 DAY 前一天）
        bars = fetch_day(code)
        time.sleep(0.3)
        if not bars:
            continue
        # 去掉 DAY 当天及以后的 bar（含 DAY）
        bars_t1 = [b for b in bars if b["date"] < DAY]
        if len(bars_t1) < 265:
            continue
        box = find_box(bars_t1)
        if not box:
            continue
        evs = replay(code, box, bars_t1, DAY)
        for e in evs[:3]:        # 每只票最多列前 3 个触发点
            total_events += 1
            same = e["verdict_design"] == e["verdict_measured"]
            if not same:
                mismatch += 1
            flag = "✅ 一致" if same else "⚠️ 不一致"
            print(f"{code:<10}{e['time']:<8}{e['price']:>9.2f}{e['box_high']:>9.2f}"
                  f"{e['cum_vol']:>10.0f}{e['c2_design']:>9.2f}{e['c2_measured']:>9.2f}"
                  f"{e['verdict_design']:>7}{e['verdict_measured']:>7}  {flag}")

    print("-" * 96)
    print(f"\n回放事件总数：{total_events}")
    print(f"设计曲线 vs 实测曲线 结论不一致：{mismatch}")
    if total_events:
        print(f"不一致率：{mismatch/total_events*100:.1f}%")
    if mismatch:
        print("\n⚠️ 曲线误差确实改变了判定结论——Demo ① 的发现不是理论问题，是实践问题。")
    elif total_events == 0:
        print("\n（本次回放日没有票触发 C1，样本为空。需要换一天或放宽 C1 判定）")


if __name__ == "__main__":
    main()
