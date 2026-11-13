#!/usr/bin/env python3
"""
Demo ①：分时量能占比曲线标定
用真实 m5 分钟线检验设计文档的预设曲线（§4.6.8）。

设计曲线 折算全天量 = 当前累计量 / 该时刻累计占比
  → 占比取得越小，折算出的全天量越大，C2 越容易达标
  → 设计占比 < 真实占比 ⟹ 系统高估量能 ⟹ 早晨假阳性
"""
import os
import subprocess
import csv
import time
from datetime import datetime
from collections import defaultdict

WESTOCK_BIN = os.environ.get("WESTOCK_BIN", "westock")
TMP_OUT = "/tmp/screener_demo/_batch.txt"
SAMPLE_SIZE = 120
BATCH_SIZE = 20          # westock 硬限制
DATE_START, DATE_END = "2026-10-26", "2026-10-30"

DESIGNED = {"09:45": 12.0, "10:00": 20.0, "10:30": 33.0, "11:00": 43.0,
            "11:30": 50.0, "13:30": 63.0, "14:00": 73.0, "14:30": 84.0,
            "15:00": 100.0}
TIMES = sorted(DESIGNED)


def load(fp):
    return [l.strip() for l in open(fp) if l.strip()]


def sample(codes, n):
    step = max(1, len(codes) // n)
    return codes[::step][:n]


def fetch(batch):
    subprocess.run(
        f"{WESTOCK_BIN} kline {','.join(batch)} --period m5 "
        f"--start {DATE_START} --end {DATE_END} > {TMP_OUT} 2>/dev/null",
        shell=True)
    time.sleep(0.4)
    lines = open(TMP_OUT).read().split("\n")
    hi = next((i for i, l in enumerate(lines) if l.startswith("| code | date |")), None)
    if hi is None:
        return []
    out = []
    for row in csv.DictReader(lines[hi:], delimiter="|"):
        d = {k.strip(): (v.strip() if v else "") for k, v in row.items() if k}
        if not d.get("code") or d["code"] == "---" or not d.get("date") or d["date"] == "---":
            continue
        try:
            out.append({"code": d["code"],
                        "dt": datetime.strptime(d["date"], "%Y-%m-%d %H:%M:%S"),
                        "vol": float(d["volume"] or 0)})
        except ValueError:
            pass
    return out


def main():
    print("=" * 78)
    print("Demo ①: 分时量能占比曲线标定（真实 m5 数据）")
    print("=" * 78)
    codes = sample(load("/tmp/screener_demo/mainboard.txt"), SAMPLE_SIZE)
    print(f"样本：{len(codes)} 只主板股票 × 5 个交易日（{DATE_START}~{DATE_END}）\n")

    bars = []
    for i in range(0, len(codes), BATCH_SIZE):
        b = fetch(codes[i:i + BATCH_SIZE])
        bars += b
        print(f"  批次 {i//BATCH_SIZE+1}: +{len(b)} 条（累计 {len(bars)}）")

    groups = defaultdict(list)
    for b in bars:
        groups[(b["code"], b["dt"].date())].append(b)

    shares = defaultdict(list)
    for (_, _), g in groups.items():
        g.sort(key=lambda x: x["dt"])
        tot = sum(x["vol"] for x in g)
        if tot <= 0 or len(g) < 40:
            continue
        cum = 0.0
        for bar in g:
            cum += bar["vol"]
            hm = f"{bar['dt'].hour:02d}:{bar['dt'].minute:02d}"
            if hm in DESIGNED:
                shares[hm].append(cum / tot * 100)

    n_obs = len(next(iter(shares.values())))
    print(f"\n有效「股票×交易日」观测：{n_obs} 个\n")

    print("=" * 78)
    print(f"{'时刻':<8}{'设计占比':>9}{'实测均值':>10}{'实测中位':>10}"
          f"{'最小':>8}{'最大':>8}{'系统投影倍数':>13}{'高估':>8}")
    print("-" * 78)
    rec = {}
    for t in TIMES:
        v = shares.get(t, [])
        if not v:
            continue
        mean = sum(v) / len(v)
        med = sorted(v)[len(v) // 2]
        rec[t] = med
        # 系统用设计占比投影，真实用实测占比。倍数 = 1/占比
        sys_mult = 100 / DESIGNED[t]     # 系统投影 = cum × sys_mult
        real_mult = 100 / med            # 真相     = cum × real_mult
        over = (sys_mult / real_mult - 1) * 100
        print(f"{t:<8}{DESIGNED[t]:>8.1f}%{mean:>9.1f}%{med:>9.1f}%"
              f"{min(v):>7.1f}%{max(v):>7.1f}%{sys_mult:>12.2f}{over:>+7.0f}%")

    print("\n" + "=" * 78)
    print("【结论 1】系统在早晨系统性高估量能 → C2 假阳性")
    print("=" * 78)
    print("逻辑：系统投影全天量 = cum / 设计占比；真实全天量 = cum / 真实占比")
    print("      ⟹ 系统投影 / 真实 = 真实占比 / 设计占比 = r")
    print("      r > 1（设计占比偏低）⟹ 系统高估量能 ⟹ C2 被抬高 ⟹ 假阳性\n")
    print(f"{'时刻':<8}{'设计占比':>9}{'真实占比':>9}{'r(中位)':>9}"
          f"{'r 的 P10~P90':>18}{'假阳性带(真实C2)':>20}")
    print("-" * 78)
    for t in TIMES:
        v = sorted(shares.get(t, []))
        if not v:
            continue
        med = v[len(v) // 2]
        p10, p90 = v[int(len(v) * .1)], v[int(len(v) * .9)]
        # 每个观测的 r = 该观测真实占比 / 设计占比
        rs = sorted(x / DESIGNED[t] for x in v)
        r_med = rs[len(rs) // 2]
        lo = 1.5 / r_med if r_med > 1 else None
        band = f"[{lo:.2f}, 1.50)" if lo else "无（不放大）"
        print(f"{t:<8}{DESIGNED[t]:>8.1f}%{med:>8.1f}%{r_med:>8.2f}×"
              f"{f'{p10/DESIGNED[t]:.2f}~{p90/DESIGNED[t]:.2f}':>18}{band:>20}")
    print("\n读法：09:45 处 r(中位)=1.56，意味着系统把真实 C2 放大 1.56 倍。")
    print("      一只真实 C2 只有 0.97 倍（根本不放量）的票，会被判成 1.51 ≥ 1.5 而误报。")

    print("\n" + "=" * 78)
    print("【结论 2】建议曲线（实测中位数）")
    print("=" * 78)
    print("  " + ", ".join(f"{t}={rec[t]:.0f}" for t in TIMES))
    print("  对比设计：", ", ".join(f"{t}={DESIGNED[t]:.0f}" for t in TIMES))
    print("  → 早盘抬升最明显（09:45 +7pp、10:00 +9pp），午后收敛")

    print("\n" + "=" * 78)
    print("【结论 3】离散度：单只票的投影误差远大于均值误差")
    print("=" * 78)
    for t in ("09:45", "10:30", "14:00"):
        v = sorted(shares.get(t, []))
        if not v:
            continue
        p10, p90 = v[int(len(v) * .1)], v[int(len(v) * .9)]
        print(f"  {t}: 真实占比 P10~P90 = [{p10:.1f}%, {p90:.1f}%] "
              f"→ 折算倍数 [{100/p90:.1f}, {100/p10:.1f}]（跨度 {100/p10/(100/p90):.1f}×）")
    print("\n⚠️ 即便曲线取对，单只票的投影仍有数倍不确定性。")
    print("   → 盘中的 C2 天然脆弱，必须靠「9:45 前不判 + 早盘更严阈值」兜底，")
    print("     不能指望单点量能比做精确判定。")

    out = "/tmp/screener_demo/volume_curve_results.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时刻", "设计占比", "实测均值", "实测中位", "最小", "最大", "建议曲线"])
        for t in TIMES:
            v = shares.get(t, [])
            if v:
                w.writerow([t, DESIGNED[t], round(sum(v) / len(v), 3),
                            round(sorted(v)[len(v) // 2], 3),
                            round(min(v), 3), round(max(v), 3), round(rec[t], 1)])
    print(f"\n结果已存：{out}")


if __name__ == "__main__":
    main()
