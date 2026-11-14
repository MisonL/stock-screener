# 可行性验证 demo 脚本

2026-10-31 为验证设计方案可行性写的一次性脚本。**结论已全部吸收进
`docs/strategy.md`**（因子定义、阈值与市场分档都在那里），这里保留脚本是为了
**可复现与再标定**，不是为了长期维护。脚本里的 `§x.y` 是私有设计文档的章节号，
那份文档没有随仓库公开——`docs/architecture.md` 末尾有一张编号对照表。

> **⚠️ 本目录里的 demo 脚本只用抽样数据，其外推结论可能系统性偏高。**
> `demo_pool_full.py` 报的「全市场 ≈97 只」已于同日被全市场全量普查推翻（真值 **58 只**）。
> 原因是命中票在 code 序上高度聚集、`random.sample` 依赖输入列表顺序——**抽到 18 只的概率只有 2.8%**。
> 详见 `docs/strategy.md` 里关于抽样外推的说明。**任何池子规模数字请以全量普查为准，不要引用 demo 的外推值。**

## 依赖

```bash
python3 -m venv .venv && .venv/bin/pip install pandas   # 仅 demo_pool_full 需要
brew install westock || npx -y westock-data-clawhub     # m5 分钟线（demo ①、③）
curl                                                     # 日线主源（腾讯直连 raw）
```

## 数据源注意

| 用途 | 走什么 | 备注 |
|---|---|---|
| 日线 raw | 腾讯 `proxy.finance.qq.com`（fq 参数留空） | **第 9 列 amount 单位是万元**，乘 10000 才是元 |
| m5 分钟线 | `westock kline --period m5 --start X --end Y` | 单次最多 20 只；非 TTY 下必须 shell 重定向才拿到完整批量 |

## 脚本

| 脚本 | 验证什么 | 关键结论 |
|---|---|---|
| `demo_volume_curve.py` | §4.6.8 分时量能占比曲线 | 设计曲线早盘偏低 6–10pp，09:45 处系统高估量能 1.56× |
| `demo_volume_curve_report.py` | 同上，含集合竞价拆分 | 高估主因是 09:25 集合竞价（占全天 9.1%） |
| `demo_pool_full.py` | §4.2 选池规模 | 600 只主板 → A∧B 过 18（3.0%）→ 外推 3194 只 = ~~97 只~~。**该数字已被全量普查推翻（真值 58 只），此脚本只用于演示漏斗与复现抽样偏差** |
| `demo_replay.py` | §4.6.8 + C1/C2 端到端 | 设计曲线 vs 实测曲线，判定翻转率 **14.3%，全为假阳性** |

## 输入文件

`demo_pool_full.py` 读 `/tmp/screener_demo/mainboard.txt`（主板代码清单，一行一个）。
生成方式：

```bash
westock universe --market hs --type stock --limit 6000 \
  | grep '^| s[hz]' | awk -F'|' '{gsub(/ /,"",$2); print $2}' \
  | grep -E '^(sh60|sz000|sz001|sz002|sz003)' > /tmp/screener_demo/mainboard.txt
```

## 重跑

```bash
mkdir -p /tmp/screener_demo && cd /tmp/screener_demo
/path/to/.venv/bin/python /path/to/scripts/demo_pool_full.py
```

限速纪律：脚本内已加 0.25–0.5s 间隔与重试，**不要并发**（腾讯是 IP 级 WAF）。
