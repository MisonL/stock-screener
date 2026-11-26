## [Unreleased]

## [0.2.136] - 2026-11-26

三路独立审查（运维 / 业务逻辑 / 安全）之后的一轮集中修复。
前三条（自选股 DESC、备份漏备、EOD marker）见 v0.2.135，这里是从那之后的部分。

###修复（安全）

- **口令缺失改为 fail-closed**：`api/auth.py` 在 `SS_WEB_PASS` 为空时
 **全站放行**，而唯一的表现是启动日志里一条 warning。公网 URL 上
 `/api/status`、`/api/push-log` 全量可读，`/api/chat` 能烧 LLM 预算。
 现在 `public_host` 是真域名时**直接拒绝启动**；compose 的 `web.env`
 也从 `required: false` 改成 `true`（文件缺失就报错，而不是静默开放）。
 占位域（本地开发）仍放行。
- **`/api/hook/*` 加独立限流**：它通过 `SELF_SIGNED_PREFIXES` 完全绕过
 口令中间件，也就不进那两档桶——匿名者可以无限打，每条新建 SQLite 连接 +
  查询 + 读密钥。512m/单核容器里足以饿死正常请求。新增 `_hook_hits` 桶
 （120 次/窗口，超限 429），并纳入 `_sweep` 防止无界增长。
- **`/api/hook/*` 统一 403**：原来 `404 卡不存在` 与 `403 验签失败`并存，
 等于给匿名者一个 **signal_id / turn_id 存在性探针**。
- **`scripts/scan_secrets.py` 补本项目凭据规则**：原规则全是"窄前缀+精确长度"，
 对本项目自己的密钥**一个都抓不到**（实测 `NTFY_TOKEN=tk_...`、
  `SS_WEB_PASS=...`、JWT 全不命中）。新增按密钥名赋值匹配 + `tk_` + JWT，
 排除 `$VAR`/占位符/布尔值；正反两面各有用例。

###修复（运维）

- **全服务加 CPU 上限**：原来只有 `mem_limit`，完全不管 CPU。构建
 （`pip install pandas/numpy/TA-Lib` + 两次 `npm ci` + `vite build`）
 会跑满 4 核，而这是台共享机——2026-11-24 实测 ssh 卡到 banner 交换超时、
  healthz 530。现在 api 0.8 / jobs 1.2 / agent 0.5 / backfill 2.0 /
  ntfy 0.1 / cloudflared 0.2。
- **`jobs` 加 `stop_grace_period: 180s`**：EOD 事务约 3 分钟，默认 10 秒
 被 SIGKILL 会毁数据。
- **部署避开危险窗口**（`15:00–15:40` / `23:25–23:50` / `08:35–09:10`），
 除非 `--force-window`。这是 2026-11-24 丢备份事故的直接防线。
- **部署互斥锁**（`data/.deploy.lock`）：两个部署同时跑会污染回滚点。
 锁带时间戳、40 分钟视为陈旧可抢占。**不能用 `flock`**——部署是几十条
 独立 ssh 命令，`ssh 'flock -n 9'` 一退出锁就释放，等于没锁。
- **UNKNOWN 退出码不再被异常击穿**：`_capture` 原来会让
 `TimeoutExpired`/`FileNotFoundError` 冒出去，`main()` 以退出码 1 结束——
 而 1 的语义是「确认坏、已回滚」，值班会去回滚一个健康的新版本。
 现在返回哨兵 → `verify_deployment()` 判 UNKNOWN（退出 3、不回滚）。
- **配置漂移提示失败不再翻掉成功部署**：新版本已上线且已记入
 `.last_good_image`，却因一句提示报失败。
- **`.pre-upgrade` 备份带时间戳**（保留最近 3 份）：原来写死文件名，
 每次不可逆迁移都覆盖同一份，想回退到更早的点时原始备份已经没了。
- **`compose` 命令 `env -u TAG` + 固定 `-p`**：`image: stock-screener:${TAG:-latest}`，
 运维 shell 里若 `export TAG=...`，构建产物与 `_image_id()` 查的不是一个东西 →
  验收假失败 → "回滚"之后跑的还是新版本。

###修复（告警质量）

- **磁盘告警去抖**：`Dispatcher.alert` 的 30 分钟去重只按 `kind + 时间`、
 **不看内容**，而巡检每 10 分钟一次。生产实测 2026-11-25：磁盘整天稳在
 80~82%，`disk_p2` 推了 **17 条**同样的话。现在同档位内水位变化不到
 3 个百分点不重复推；回落安全线以下会清状态（否则下次越线被自己压掉）。

###文档

- `deploy/RUNBOOK.md`：缓存回收从「80% / `until=72h`」改为
 「95% / `--keep-storage 5GB`」并写明两个数都改过、别再改回去的理由；
 磁盘阈值统一为「10 分钟巡检、75/85/95 三档」；新增「不能部署的时间窗口」
 与「部署互斥」两节。

###测试

- 每条修复都配了变异验证（改坏实现 → 对应用例变红），包括：
  CPU 上限、fail-closed、hook 限流、密钥扫描、磁盘去抖、部署窗口、
  UNKNOWN 映射、`.pre-upgrade` 时间戳。
- 修掉两处**测试污染**：`api.auth` 新增的 `_hook_hits` 桶没被夹具清理，
  `/api/hook/` 用例打完 120+ 次后，后续用例全拿到 429 而不是 403/404
 （单跑绿、整跑红）。夹具改为三个桶一起清。
- 部署锁脚本改成**多行**、且只认 `out.splitlines()` 里**整行相等**的标记。
  （见下：一行式 `try:` 是语法错误，traceback 回显的源码里含 `SS_LOCK=BUSY`
  字样，被子串匹配当成"锁被占"，**部署被一个不存在的锁拦住**。）

**测试**：81.4全量 **1456 passed, 3 skipped**。

## [0.2.135] - 2026-11-25

###修复（自选股告警长期整体静默 —— 生产实测确认）

`jobs/intraday.py::_watch_hist` 原来是 `ORDER BY date DESC LIMIT 60`，
于是 `hist[-1]` 是**60 个交易日前**的那根。而
`core/intraday/watch_rules.py` 用「`hist[-1]` 是最近一根」来用：

- `abs(prev_close - hist[-1].close) > price_eps(0.011)` —— 除权对齐判据，
  **几乎必然成立** → 整只票 `return []`
- `_crossed()` 的 MA20 穿越基准
- `recent = hist[-extreme_days:]` 取最近 N 天

**生产实测**（sh600519，唯一一只自选股）：
最新收盘 1474.84、60 日前 1302.8，**差 172.04**；
`event_log` 里 `watch_rule` 事件总数 = **0**。
也就是说 pct / vol / near_extreme / ma20 四类告警**从来没触发过，一条日志都没有**。

**为什么测试全绿**：`tests/core/test_watch_rules.py` 直接按 ASC 造数据喂给
`eval_watch`，而端到端用例只插了 1 根 bar——DESC 顺序从没被覆盖。

- 改为「内层 DESC 限 60 → 外层 ASC」。**只把 DESC 换成 ASC 也是错的**：
  `ORDER BY date ASC LIMIT 60` 取的是**最早** 60 根（2019 年那批）。
- 补回归测试，对两种错法都有断言（实测变异：退回 DESC → 「必须升序，
  实际首=20250170」；纯 ASC → 「最后一根应是 20250170，实际 20250160」）。

###修复（部署压掉 23:30 备份 → 那晚永久丢失）

**这是真的丢了数据**。2026-11-24 的备份至今缺失：

```
部署时刻:   2026-11-24 23:30:14
备份时刻:   每日 23:30
新容器启动: 2026-11-24 23:33:12   （已错过 3 分钟）
残留证据:   08:55「清理中断备份 3个：.market-20261124.db.tmp(-shm/-wal)」
backups/:   最新是 market-20261123.db，11-24 没有
```

根因：`misfire_grace_time=3600` **只在「调度器在跑但被阻塞」时生效**。
进程在 23:30 那一刻不在，这次备份就永久丢了。这句判断在
`jobs/main.py` 的注释里**早就写着**，但当时只补了「备份陈旧**检查**」，
没有补「备份**本身**」——于是系统正确报警（次日 08:55 `backup_stale`
「33 小时前」），却**没有任何自愈动作**，46 小时过去备份仍是缺的。

- 新增 `jobs/backup.py::catch_up_missed_backup`，启动时补一次。
 **两个条件缺一不可**：
 ① 今天该有的文件不在**且**已过 23:30 → 覆盖「23:30 那一刻不在，几分钟后起来」；
 ② 最新备份已超过 29 小时 → 覆盖「23:00 关、次日 09:00 才起」（此时 ①
 永远不成立，但断档已经发生，必须立刻止血）。
- `BACKUP_HOUR`/`BACKUP_MINUTE` 提到 `jobs/backup.py` 做**单一定义**，
 `CronTrigger` 与补备判据共用，避免两处写死两个数字而分叉。
- 变异验证：关掉条件① → 「23:33 重启（错过 23:30），没有补备份」；
 关掉条件② → 「断档 33 小时后启动，却没有补备份」。

###修复（EOD 的 `skipped` 也写 marker → 该交易日永久失去补跑）

`eod_job` 原来在 `if not report.skipped` **之外**无条件写
`eod.<today>='done'`。而 `startup_recovery` 的补跑判据正是「marker 不存在」，
于是 `skipped`（上游快照还是上一交易日的 / 时间门未到 / 宇宙未回填）的
那一天**永远不会被补**：当天无 bar、无选股池，次日 `prev_day` 直接跨过去。
`startup_recovery` 自己对 `skipped` 就是不写 marker 的，两处口径相反。

- marker 写进 `if not report.skipped` 分支；`else` 记 warning（含 report）留痕。
- 用 AST 断言「写 marker 的语句处于该 `if` 的 body 内」，并额外断言
 `else` 里**没有**再写——变异（把 marker 移回 if 外）实测红在断言。

###其它

- `tests/jobs/test_main_scheduler.py` 的「文档时刻表 vs 代码」守卫改为
 **认常量**（`CronTrigger(hour=BACKUP_HOUR, ...)`）+ 跨文件解析
 `jobs/backup.py` 的模块级常量。只认字面量的话，把 23:30 提成常量会让
 守卫误报「23:30 不存在」——那是误报，不是文档失真。

**测试**：81.4全量 **1430 passed, 3 skipped**。

## [0.2.134] - 2026-11-24

###修复（评审发现的「失败时静默退化成一个看起来正常的数字」）

独立评审给 0 阻断 / 8 重要。最要命的三条都是同一个形状——**读不到
被伪装成 0**：

- `jobs/cleanup.py` 与 `api/routes.py`：`disk_used_pct` 读不到时
 **返回 `None`，不再返回 `0.0`**。0 的含义是"磁盘很空"，会让
 75/85/95 三档**全部不触发**，`push_log` 里只留"磁盘 0%"的假记录。
 新增 `disk_unknown`（P1）告警；api 侧同步返回 `null`。
- `host_disk_pct`：同样改为 `None`。`_capture` 把 stdout+stderr 拼接，
 ssh 报错若恰好有 6 个含数字 token 会被当 df 数据。现在要求
 **≥6 字段且第 5 个以 `%` 结尾**，失败打警告而不是静默跳过。
- `prune_old_images` 加 `SS_DONE` 哨兵：区分"删了 0 个"和"根本没跑成"
（ssh/docker 挂掉时两者都表现为 0）。

**清理失败不能再翻掉一次成功的部署**：ssh 卡住会抛 `TimeoutExpired`、
ssh 不在抛 `FileNotFoundError`；未捕获就是「部署成功却以非零码 +
traceback 结束」，CI/值班会去回滚一个健康的新版本。整段包
`try/except`，抽成 `_post_deploy_cleanup()`。

###优化（资源占用：修掉我自己引入的 CPU 尖峰）

- **构建缓存阈值 80 → 95**：`compose build api` 包含 pip 装
 pandas/numpy/TA-Lib + 两次 npm ci + vite build。清缓存会连带删掉
 这些层，下次部署要**满核几分钟**重建。本机痛点是**负载**
（实测 ssh 卡住、healthz 530），不是磁盘写满（常年还有 10G）——
**用 CPU 换磁盘是换错方向**。稳态 90% 绝不再清，回归测试锁住。
- `--filter until=72h` → `--keep-storage 5GB`：`until` 判**创建时间**，
 本机一天十几次发版，缓存几乎都是新的，实测返回 `Total: 0B`——
 磁盘越线时**一个字节都收不回来**。按总量封顶行为可预测。
- `core/db.py` 加 `PRAGMA temp_store = MEMORY`：默认用文件会把
 SQLite 中间结果写进 `$TMPDIR` = **容器可写层**（无监控），
 OOM/重启杀掉就残留。这是删掉 host 脚本后评审找到的真盲区。
- `api` 健康检查 30s → 60s：每次起一个 python 解释器约 80ms，
 30s 一天 2880 次≈4 分钟纯启动开销。

###修复（备份与告警）

- `_prune` 补**孤儿伴生文件**清扫：主体已不在的 `-shm`/`-wal`
 永远不会被 `market-*.db` 匹配到（81.4 上那对 32KB 就是这个形态）。
 顺序改成**先删伴生再删主体**，避免中途被杀再造孤儿。
 `keep < 1` 直接拒绝（否则会把刚写好的备份也删掉，还谎报成功）。
- `notify/templates.py` 补 `disk_p0/p1/p2/disk_unknown` 中文标题，
 否则手机推的是裸标识符。**这个漏项是测试抓到的**（扫描全部
 `.alert(...)` kind 对照标题表）。

###测试

- 换掉同义反复的镜像测试：**真跑一个假 docker**，断言 rmi 的
 恰好是本项目的 2 个（不碰别人的和无标签的）。
- 删掉恒真断言 `abs(got-85.5)>2`（该 fixture 下没有任何写法会得到 85.5）。
- 新增 api/jobs 同数据源一致性测试、清理失败不影响退出码、
 稳态 90% 不清缓存、孤儿伴生、`keep=0`、解析失败等用例。
- 修掉一处**测试污染**：直接给模块属性赋值而非 monkeypatch，
 导致"单跑通过、合跑失败"。

**测试**：81.4全量 **1423 passed, 3 skipped**。

## [0.2.133] - 2026-11-23

###决策（放弃「指出哪个容器在长」，并写清依据）

删掉 `host_disk_guard.sh` 前逐条核实了**它唯一不可替代的能力**到底值不值：

| 核实项 | 结论 |
|---|---|
| 本项目容器可写层 | **28.7kB ~ 41kB**（`docker ps -s` 实测） |
| 为什么这么小 | 写入全在 bind mount：`./data:/app/data`（api/jobs/agent/backfill）、`./ntfy/cache`；cloudflared 只挂只读 `cf/` |
| 容器内能否自查 | **不能**：`jobs` 没有 docker CLI，也看不到 `/var/lib/docker` |
| 日志会不会撑爆 | 全部服务挂 `x-logging`（50m × 3），**上限 150MB/服务** |

结论：**本项目不存在「容器可写层疯长」这个故障模式**。要写的东西都写在
bind mount 上，那部分由应用侧总量巡检覆盖，`data/backups` 还有 `keep=7`
保留策略兜着。

真正的风险形状是「**别的容器**在不相关的项目里长」——那种情况下
「哪个容器」有用，但要为此在 jobs 里挂 `/var/run/docker.sock`（等于给
root 权限）或引入 cAdvisor（多一个服务维护），代价明显大于收益。

- 决策与依据写进 `deploy/RUNBOOK.md`（含「将来 `docker ps -s` 出现 GB 级
 再引入 cAdvisor」的复评条件）。
-补 `test_every_service_caps_its_log_files`：**逐服务**断言挂了
 `x-logging` 且 `max-size`/`max-file` 非空。日志是容器唯一能自己长大的口子，
 漏一个就是无界增长。变异（删掉 agent 的 logging）→
 `AssertionError:这些服务没有日志上限，会无限增长：['agent']`。

**测试**：14条（deploy_script）。81.4全量 **1414 passed,3 skipped**。

## [0.2.132] - 2026-11-23

###变更（磁盘防护全部搬进容器，删除系统级脚本）

按「项目定位容器化部署」重排，**不再需要宿主机 crontab**：

- **删除 `scripts/host_disk_guard.sh`**。它唯一不可替代的能力是「指出**哪个**
 容器在长」，而这个信息对单项目部署没有决策价值——总量告警 + 部署时自动回收
已经够用，不值得为它引入宿主机级依赖。
- `host_disk_pct(host)`：从目标机读 `df -P /`，按 `used/(used+avail)` 算，
与 `jobs/cleanup.py` 同口径（ext4 保留块不算进分母）。
- `prune_build_cache(host, keep_hours=72)`：`docker builder prune -f
 --filter until=72h`。**构建缓存无法按项目筛**（buildx 缓存记录不带
compose 项目标签），所以会连带影响同机其它项目——代价只是它们下次构建
慢一点，不中断任何正在跑的东西。
- `DISK_RECLAIM_PCT = 80.0`：**只在磁盘越线时才回收**。平时清缓存纯属
自找麻烦（只会让下次构建变慢）；`until=72h` 保证最近三天缓存还在，
出错后立刻重建仍然快。
-接入位置：部署验收通过之后、镜像清理之后。失败时**不清理**。

**实测（81.4）**：`host_disk_pct` 返回 85.3%，与 `df` 的 85% 一致。

测试6条（df口径、读不到返回0、必须带until、越线才回收、未越线不动、
失败不回收）。变异3条全部红在断言：
 `if pct >= DISK_RECLAIM_PCT`→`if True` →「磁盘40%却清理了构建缓存」；
去掉 `--filter until=72h` →「没限定时间」；
分母改 `used+used` → `50.0` 对 `86.96` 差 36.96。

###清理（81.4本项目占用）

- 删 `data/market.db.pre-upgrade`（1.3GB）：这是我 11-22 测试
 `backup_before_upgrade` 时留下的副本。当时 8 个迁移**早已全部应用**，
 它保护不了任何东西，且 `data/backups/` 里已有同日的每日备份。
- 删 `data/backups/market-20261121.db-shm` / `-wal` 残留（32KB/0B）。
- **并修掉产生它们的根源**：`jobs/backup.py::_prune` 只 glob
 `market-*.db`，**匹配不到** `market-YYYYMMDD.db-shm`
（glob 要求以 `.db` 结尾，`*` 不会拐过 `-shm` 后缀），
 所以旧归档删掉后伴生文件永远留着——**每天一份、永不回收的无界增长**。
 现在主体删除时连带删 `-shm`/`-wal`/`-journal`；
 **保留那份的伴生必须留下**（删了会破坏归档），测试对此有断言。
 变异（去掉伴生删除）→ 红在断言，列出 4 个残留伴生文件。

**磁盘：91% → 90%，可用 8.8G → 10G。**

**测试**：60条（deploy）。81.4全量 **1412 passed,3 skipped**。

## [0.2.131] - 2026-11-23

###修复（v0.2.130的api口径改错了：分子也用了bavail）

- `api/routes.py` 的分子改成 `f_blocks - f_bfree`（**不含**保留块），
分母 `used + f_bavail`。实测与 jobs、与 `df` 三者对齐（84.69% / 84.69% / 85%）。
-补 `test_api_disk_used_pct_matches_df_on_ext4`，**造出保留块**来测；
变异（分子错 / 分母错）各自红在断言。

v0.2.130 部署后立刻核对生产，发现 api 报 **90.1%** 而 `df` 是 84.6%——
**改错了**。`(f_blocks - f_bavail)` 这个分子把 ext4 给 root 保留的 5%
（本机 5.02 GB）当成了"已用"。

-正确口径：`used = f_blocks - f_bfree`，`free = f_bavail`，
 两者都**不含**保留块；`used/(used+free)` 即 `df` 的 Use%。
-实测（81.4，真实 ext4）：jobs 84.69%、api 84.69%、`df` 85%
 →三者对齐，两公式差 **0.0000** 个百分点。

**为什么本机测不出来**：HFS/APFS 没有保留块（`f_bfree == f_bavail`），
三种写法在本机结果完全相同。这个错误**只有真实 ext4 才暴露**——
所以我必须在生产上核对产物，而不是相信本机测试全绿。

补测试 `test_api_disk_used_pct_matches_df_on_ext4`：**造出保留块**
（100G 盘 / used 80G / bavail 10G / 保留 10G），断言 ≈88.9%，
并分别断言**不等于** 90.0%（分子错）和 80.0%（分母错）。
变异验证：两个错法各自红在断言
 （`实际90.0%` / `实际80.0%`）。

**测试**：81.4全量 **1406 passed,3 skipped**。

## [0.2.130] - 2026-11-23

###修复（磁盘百分比口径错了4.3个点，P0永远到不了线）

查 81.4 的磁盘告警时发现两个数对不上：应用侧 `push_log` 报 **81%**、
`df` 报 **85%**，同一块盘。根因是 **ext4 给 root 保留 5% 空间**
（本机 5.02 GB / 98 GB），而两个实现都把保留区算进了分母：

| 实现 | 公式 | 实测 |
|---|---|---|
| `jobs/cleanup.py` | `used/total` | 80.3% |
| `api/routes.py` | `(blocks-bavail)/blocks` | 85.4% |
| `df`（基准） | `used/(used+free)` | 84.6% |

**后果不是数字难看，而是告警永远按时不触发**：P1（85%）要等 `df` 到 89.5%，
P0（95%）要等 `df` 到 **100.2%**——磁盘写满那一刻 P0 还没到线。
2026-11-21 那次撑爆正是这个形态（那次是"一天只查一次"，
这次是"口径不对"，结果一样）。

- `jobs/cleanup.py::disk_used_pct`：改成 `used/(used+free)`。
- `api/routes.py::_disk_used_pct`：分母改成 `f_bavail + (f_blocks - f_bfree)`。
- 两个公式现在**实测完全一致**（0.0000 个百分点差），也与 `df` 同口径，
 符合 RUNBOOK §571「与 df 一致（±2%）」的既有约定——此前两边都违反了。
-补测试 `test_disk_used_pct_matches_df_not_used_over_total`：
 模拟一块 100G 的盘（used 80G / free 10G / 保留 10G），
 断言结果 ≈88.9% 且**不等于** 80.0%。变异（改回 `used/total`）
 → `AssertionError:应该是 used/(used+free)=88.9%，实际80.0%`。

**测试**：55条（本文件）。81.4全量 **1405 passed,3 skipped**。

## [0.2.129] - 2026-11-22

###修复（每次发版留下的悬空镜像在偷磁盘）

盘点81.4磁盘：根分区87%（80G/98G），其中 `/var/lib/containerd` 占44G。
**直接归因到本项目**的泄漏：11个悬空镜像中有9个是 639MB 的
`com.docker.compose.project=stock-screener`（今天13次发版留下的，约5.7GB）。

- `prune_old_images(host)`：遍历悬空镜像，用
 `docker image inspect '{{index .Config.Labels "com.docker.compose.project"}}'`
 筛出 `stock-screener` 再 `docker rmi`。
- **不能无差别 `docker image prune -f`**：同机还跑着 WeKnora / selfhost /
 langbot；实测有一个 129MB 的 nginx 悬空镜像不是我们的，会被一起删掉。
- **绝不能 `prune -a`**：那会删掉 `stock-screener:rollback`
（带标签但没被容器引用），回滚能力随之消失。
-只在**验收通过之后**清理：失败时 rollback是刚需。

实测：9个本项目悬空镜像删除，两个非本项目的原样保留，
`latest`/`rollback` 标签镜像未受影响。

**这轮踩的坑**：第一版在 bash 模板里写了 `{{{{index .Config.Labels ...}}}}`
（四层花括号，pyflakes查不出），远端实际收到四层 → docker不认 →
删了0个。打印渲染后的命令才发现花括号层数不对，改成两层后成功。

测试5条（只筛本项目、不用 -a、没有就返回0、成功才清理、失败不清理）。
变异：部署后不清理 →红；把清理改成无差别 `prune -a` →
 `AssertionError:用了 -a，会删掉回滚镜像`。

**测试**：54条。81.4全量 **1404 passed,3 skipped**。

## [0.2.128] - 2026-11-22

###修复（上传/解压失败只有堆栈，没有一句人话）

盘点了 deploy.py 各环节的测试覆盖，发现上传阶段（mkdir/scp/解压）失败时
`_run`抛出的 `SystemExit` 直接炸出堆栈——退出码是对的（非零），
**但运维看到的是 traceback而不是「部署停在哪一步」**。此时容器还没动，
不存在回滚问题，缺的只是一句人话。

- 把 mkdir/scp/解压包进 `try/except SystemExit`：
 `**上传或解压失败，部署中止（容器未动，无需回滚）**：<原因>`，退出1。
 明确说明**无需回滚**，防止有人下意识去按 RUNBOOK §9.3.1 回滚——
代码根本还没换。
-两条测试：scp失败、解压失败，各自断言「有人话 +没继续 build」。
-变异验证：把 `print+return`改成 `raise` →两条测试同时红
 （`SystemExit`逃出 + `TypeError`）。

**顺带**：改这段时把解压调用整块弄丢过一次（语法仍合法、
测试却抓不住「少了一次 ssh」）。是从备份恢复整块再重写的，
并用 `grep -c "remote_extract_script"`确认两处调用都在。
教训：**对生成代码的重排必须以行为单元为单位，不能逐行替换**——
一行语法正确不代表一个调用还在。

**测试**：49条。81.4全量 **1399 passed,3 skipped**。

## [0.2.127] - 2026-11-22

###新增（不可逆迁移前自动备份）

RUNBOOK §9.2 只是**要求**运维升级前手工备份，而「记不记得」正是故障点。
现在 `deploy.py` 在构建之前自动做掉：

- `classify_migration(sql)`：把迁移归类为 `additive`（加表/加列/加索引，
 回退代码可安全跨过）或 `irreversible`。**irreversible 有两类**：
改结构（`DROP TABLE` / `RENAME TO`，如004重建表）和**改数据**
 （`UPDATE`/`DELETE`，如006修旧语义的行——表结构没动，
看起来「安全」，但旧代码回来面对的是被改写过的行）。
认不出来的语句一律按 irreversible——宁可多备份一次。
- `pending_risky_migrations(applied, schema_dir)`：还没应用且不可逆的迁移。
 `applied` 为 None（问不到远端）时**全部视为待应用**——
 宁可多备份一次，不要因为读不到就跳过。
- `backup_before_upgrade(host, target)`：在目标机用 sqlite3 的 **backup API**
生成 `data/market.db.pre-upgrade`。**不能用 `cp`**：WAL 下未 checkpoint
的事务在 `-wal` 里，`cp` 会缺数据甚至拷出半个页。
- `main()`：命中不可逆迁移 →先备份 →失败**中止部署**
 （`--skip-backup` 可绕过，风险自担）。备份必须在构建之前。

实测（81.4）：真实备份1.3GB 成功；`pending_risky_migrations` 对当前
8 个已应用迁移返回空，清空 applied 后正确指出004 和006。

测试10条（additive/rebuild/数据UPDATE/注释误判/只挑未应用的/None全算/
备份失败如实报/必须用 backup API/不可逆触发备份/备份失败中止）。
变异：备份失败不中止 →`assert0 !=0` 红；
认不出语句当 additive →`AssertionError: 'additive' == 'irreversible'`。
（把 DROP检查删掉是**等价变异**——兜底分支同样判不可逆，行为不变。）

**测试**：47条。81.4全量 **1397 passed,3 skipped**。

## [0.2.126] - 2026-11-22

###新增（回滚时检测数据库结构是否已前进）

这是我上一轮留下的设计债：**回滚只回代码，不回数据**。代码退回去了，
库却已经被新迁移改过——加列/加表通常无碍，但重建过表的
（`004_fetch_log_gap.sql` 就是 `CREATE新的 → DROP旧的 → RENAME`）
可能让旧代码读不了。以前这件事**完全没人知道**。

- `read_schema_version(host, target)`：以 `mode=ro` 只读打开远端
 `data/market.db`，取 `schema_version` 里的迁移名。用 python3 + sqlite3模块，
 不依赖宿主机有没有 sqlite3 CLI（容器里就没有）。
 **问不到返回 `None`，库是空的返回 `[]`**——两者必须区分，混了就会漏报。
- `schema_drift_warning(before, after)`：回滚后比对，只有真的多了迁移才返回告警；
 任一为 `None`（没问到）时不告警也不假装没问题。
- `main()`：部署前记一次，**两个回滚分支**回滚后各比一次，命中就打印
 「数据库结构已经前进，回滚只回代码、不回数据」并列出具体迁移名，
 指向 RUNBOOK §9.2 的 `data/market.db.pre-upgrade` 恢复流程。

实测（81.4）：能读出全部 8 个迁移；故意传多一个 `009_x.sql` 会正确告警。

测试7条（解析输出、问不到返回 None、无漂移静默、读不到静默、
告警点名具体迁移、回滚后有迁移要告警、无迁移不告警）。
变异验证：把两处 `if warn:`都改成 `if False:` →
 `AssertionError:没告警 schema漂移`；`return None`改成 `return []` →红在断言。

**测试**：37条。81.4全量 **1387 passed,3 skipped**。

## [0.2.125] - 2026-11-22

###修复（外部探活只验"有东西应答"，没验"是不是这一版"）

v0.2.124的外部探活要求 `"ok":true`，但这**证明不了新代码对外生效了**：
CDN缓存、另一个实例、回滚到旧版本，都会返回 `ok:true`。

- **按 tag推出期望版本一起比对**：`v0.2.125` →要求响应里含
 `"version":"0.2.125"`。没打 tag时（`--dry-run`/开发）为空，跳过该比对。
-实测：同一地址传 `expect_version="0.2.124"` → `OK`；
传 `"9.9.9"` → `FAIL:对外版本不是这次部署的（期望9.9.9）…`。
-补两条测试（版本一致 /版本过期）。变异：把 `if expect_version:`改成
 `if False:` →红在断言。

**测试**：30条。81.4全量 **1380 passed,3 skipped**。

## [0.2.124] - 2026-11-22

###修复（外部探活默认 UA被 Cloudflare判成爬虫）

- **显式设置 `User-Agent: stock-screener-deploy/1.0`**：Cloudflare把 urllib
的默认 UA当成爬虫拦掉，返回403，而服务本身是好的。
-补 `test_check_public_health_sends_a_real_user_agent`：断言 UA非空且不含
 `urllib`。变异（换回裸 `urlopen`）会红在断言。

v0.2.123上线后第一次带 `--public-url`部署，门禁立刻报：

```
**外部探活失败**：FAIL:请求失败 HTTPError: HTTP Error403: Forbidden
```

而同一条命令 `curl`是200。实测三者对比：

| UA |结果 |
|---|---|
| `curl/8.7.1`（curl默认） |200 |
| `Python-urllib/3.14` （urllib默认） | **403** |
| `stock-screener-deploy/1.0` |200 |

Cloudflare把默认 urllib UA当爬虫拦了。→显式设置
 `User-Agent: stock-screener-deploy/1.0`。

**这条值得记下来**：新加的检查第一次跑就报红，而**红的原因不是服务坏了**，
是检查自己不讨喜。如果不看 UA就直接信它，会得出"生产不可用"的错误结论——
和之前"0.8秒判成功"是同一类错误的两面：**结论比证据跑得快**。

补测试 `test_check_public_health_sends_a_real_user_agent`：
断言请求头里的 UA非空且不含 `urllib`。
变异验证：把 Request换回裸 `urlopen` →测试红在断言。

**测试**：28条。81.4全量 **1378 passed,3 skipped**。

## [0.2.123] - 2026-11-22

###新增（部署后从外部验一次 /healthz）

前几轮的门禁只验**容器内部**：容器 `Up (healthy)`、镜像对、日志无 traceback。
但 cloudflared挂了、DNS改了、证书过期时，**容器全是 healthy，用户一个都打不开**，
门禁照样报"验收通过"。这是三轮评审里唯一一直没补的实质缺口。

新增 `--public-url`（也可用环境变量 `SS_PUBLIC_HEALTHZ`，留空则跳过）：
验收通过后从**本机**探一次公网 `/healthz`，要求 HTTP200且响应体含 `"ok":true`。

两个刻意的取舍：

- **失败不回滚**。外部不可达多半是隧道/DNS/证书问题，回退代码解决不了，
反而会把刚验过的版本换掉。失败时打印「容器与新镜像都已验过；这次失败在
公网可达性」，**退出码3**（与 UNKNOWN同码，同为"不是代码的问题"）。
- **先记好镜像、再探外部**。镜像本身已通过容器侧验证，
外部不可达不影响"这个镜像好"的判断，所以 `.last_good_image`照写。

测试6条：SKIP、200+ok:true、502、200但内容是登录页、连接异常、
以及 main()在外部失败时**退出3且不回滚**。
变异验证：把失败分支改成 `if False:` → `AssertionError:外部探活失败应该退出3，实际0`。

**测试**：27条。81.4全量 **1377 passed,3 skipped**。

## [0.2.122] - 2026-11-22

###修复（第三轮评审：三项窄范围复核）

对 v0.2.121做的五项窄范围复核确认：**四项已修**（marker读取、Exited判据、
空镜像ID、UNKNOWN退出码3），六条变异全部红在断言。但仍有三个缺口：

- **`health: starting`被判成功**。[严重]
等健康的循环写作 `while [ now -lt deadline ]`；一旦进入这一步时 deadline
已过（`--settle`接近或超过 `--verify-wait`、或前面等 running耗掉了预算），
循环**一次都不执行**，`pending`停在初始值0 →直接判「健康检查通过」。
 →在末尾**直接再查一次**每个服务的状态，见 `*health:*`即判 FAIL，
不再依赖循环是否执行过。

- **回滚后的复验因空镜像 ID判 UNKNOWN**。[中等]
回滚分支调用 `verify_deployment(..., image_id=None)`，而 v0.2.121刚加了
「EXPECT为空直接判 UNKNOWN」——于是**每次回滚后的复验都白做**。
 →回滚会把 `latest`指回旧镜像，所以复验时**重新取一次**镜像 ID传入。

- **「回滚后必须复验」没有被测试钉住**。[中等]
原测试只在 `verify_deployment` **被调用时**才断言「此时已有 rollback」——
把那行调用整个删掉，断言根本不执行，20条照样全绿。
 →加 `verifies`计数器，最后断言 `assert verifies`。
变异验证：把复验调用改成 `back = "OK"` →
 `AssertionError:回滚后没有复验——把那行删掉测试必须红`。

另补一条真脚本测试 `test_real_script_fails_while_health_is_still_starting`：
假 docker让容器停在 `Up3 seconds (health: starting)`，脚本必须判 FAIL。

**测试**：21条。81.4全量 **1371 passed,3 skipped**。

## [0.2.121] - 2026-11-22

###修复（第二轮评审：marker从没被读出来过）

独立评审对 v0.2.120复核了上一轮那8条，结论是 **6条真修、1条半修、1条还是假的**。

- **【严重】`.last_good_image`写了却从没被读出来**。[假的]
 `snapshot_image`把**文件路径**直接递给 `docker image inspect`：
 ```bash
 docker image inspect /home/qdm/stock-screener/.last_good_image
 # → invalid reference format
 ```
所以回滚点**始终是「当前 latest」**，第5条根本没修上。
81.4实测确认：marker内容是对的（`sha256:fd9f…`），但 `inspect "$M"`失败、
 `inspect "$(cat $M)"`成功。
 →改成 `I=$(cat $M)`。**这条错我犯了三次**（先是没写成、再是格式错、现在是读了不认），
所以这次加的不是"断言字符串存在"，而是**用假 docker把真脚本跑起来**。

- **【严重】健康阶段用 `docker ps`（只看 running），容器消失会被当成"没有 healthcheck"**。[新引入]
 settle之后容器若退出或进入 Restarting，`docker ps -a`里才看得到，
而 `docker ps`里查不到 → `case`不匹配任何分支 →判定通过 → **假绿**。
 →全程改用 `docker ps -a`，并把 `Exited`/`Restarting`/`不存在`都判 FAIL；
结尾再复核一次 running。

- **【中等】settle吃掉了验收预算**：`deadline`在 settle之前算，
默认 `settle20 + wait90`实际只剩70秒，而 compose里 jobs `start_period`是300秒
 →冷启动稍慢就误判失败并自动回滚。
 →`deadline = start + settle + wait`。

- **【中等】UNKNOWN与 FAIL同用退出码1**：`up -d`已成功、代码已上线，
运维却可能当成"没部署"去手工回滚一个健康的新版本。
 →UNKNOWN用**退出码3**，文案明说「新代码已经上线」。

- **【中等】RUNBOOK教的 `--no-verify`不存在、`--settle0`语法错**
 →补回 `--no-verify`，RUNBOOK的写法改成 `--settle0`。

- **【中等】`--dir ~/x`下 marker写不进去且静默**：`_sh_quote`会把路径包进单引号，
 `~`不会被远端展开。→ `os.path.expanduser`；`record_good_image`改为**回读确认**并返回布尔。

- **【中等】`EXPECT`为空时镜像比对被静默跳过**，成功文案却照吹"跑的是新镜像"
 →取不到镜像 ID直接 `UNKNOWN`。

- **【中等】build/up失败回滚后不复验**（上一轮只修了一半）
 →回滚后同样跑一次验收。

- **【中等】`ntfy`/`cloudflared`的 `Restarting`仍会挡住部署**
 →`Restarting`/`unhealthy`也只对本次重建的服务算。

- **【轻微】三条恒真断言**：settle用实参20去断言 `-lt20`（改成硬编码照样绿）、
镜像比对只断言 `.Image`字符串存在（`!=`反转成 `==`照样绿）、
 marker只比字符串先后（读取坏掉照样绿）。
 →改成用不同参数值、喂错镜像 ID、断言 `$(cat $M)`；
并新增7条**用假 docker跑真脚本**的测试（镜像不符/无 ID/traceback/
 Exited/Restarting/unhealthy/正常），这类逻辑只有真跑 bash才钉得住。

**测试**：20条。六条变异全部红在断言——
settle写死、`grep -qx`→`-q`、镜像比较方向反转、删掉 UNKNOWN分支、
marker不读文件、build失败不回滚。

## [0.2.120] - 2026-11-21

###文档（RUNBOOK §9.3.1：把新的部署验收与回滚写清楚）

评审列的轻微项：`deploy/RUNBOOK.md`仍在教老的回滚法（`git checkout tag`
+ build），没写 `stock-screener:rollback`标签、没写 `.last_good_image`、
也没提醒 `docker image prune -a`会删掉回滚镜像。

新增 §9.3.1：

-完整七步流程（快照 →构建 →验收四段 →写好镜像 →失败回滚 →UNKNOWN只报警）；
- **为什么必须 settle与 health**：`up -d`返回时容器就是 `Up0 seconds`，
旧实现实测0.8秒就放行；
-手工排障开关（`--no-verify` / `--settle0 --verify-wait30`）；
-跨多个版本时的 `git checkout tag`老路（作为备用，而非默认）；
- **⚠️ `docker image prune -a`会删掉回滚镜像**：`-f`（不带 `-a`）
只删 dangling，`-a`会把没被容器引用的标签镜像一起清掉；
- **⚠️回滚只回代码，不回数据**：`migrate()`只前进没有 down，
跨大版本回滚前先按 §9.2备库。

## [0.2.119] - 2026-11-21

###修复（build/up失败时不回滚——Codex两次评审都点到的最后一条）

两份 Codex评审（`ctx_6d031784070c`、`ctx_664fbd49291b`）都在
7375928（v0.2.116）上独立指出：**部署中途失败根本不会回滚**。

`compose build` / `up -d --force-recreate`任一非零 → `_run`抛 `SystemExit`，
在**验收之前**就退出，而回滚只写在验收的 `else`分支里。后果：

- `up -d`半途失败 →留下「一半旧、一半新」的栈，**无人回退**；
- build失败 →容器还跑着旧镜像，但 `latest`已被半成品覆盖，
下一次部署的快照语义跟着乱。

→把 build/up包进 `try/except SystemExit`，失败时先回滚再返回非零，
并明确打印「构建或重启失败」与回退结果。

测试 `test_build_failure_also_rolls_back`，变异验证会红在断言：
把回滚调用改成 `(False, "变异：不回滚")` →
 `AssertionError:构建失败但没有回滚——会留下半新半旧的栈`。

###其余评审结论（已在0.2.117/0.2.118处理，此处仅记录核对结果）

- 「jobs第30秒崩溃前就误报成功」→0.2.117的 settle+health等待。
- 「预停的 ntfy或同名前缀容器导致误报失败」→0.2.117收窄 `Exited`。
- 「部分部署失败跳过回滚」→本轮修。
- 「回滚不复验」→0.2.117回滚后二次验收。
- 「源码在镜像内、当前 rollback不会被 `docker image prune -f`删除」
 →两位评审都核实过，与仓库 `host_disk_guard.sh`的建议一致
 （带标签镜像不属于 dangling）。

## [0.2.118] - 2026-11-21

###修复（0.2.117的两个洞：marker没写成、format转义错）

部署0.2.117后核对现场，发现两处**功能没生效**：

- **`_image_id`的 docker format多写了两层花括号**。[严重]
 `{{{{.Id}}}}`不是合法 format，docker直接报错返回空串 → `if image_id:`
恒假 → **`.last_good_image`从来没被写过**，快照一直是"当前 latest"
的老逻辑。等于 M3（回滚点被坏镜像污染）**根本没修上**，
却因为日志里没报错而看不出来。
 →改成 `{{.Id}}`。pyflakes查不出这类错（在字符串里），
补了两条测试钉住：合法 format +取不到时返回空串。

- **快照优先用 marker这条路径没有测试覆盖**。[中等]
补 `test_snapshot_prefers_the_recorded_good_image`：断言命令里
 `.last_good_image`出现在 `stock-screener:latest`之前——
顺序反了就会把坏镜像快照成回滚点。

实测（81.4）：`_image_id`现在能返回
 `sha256:b39ac247…`，`record_good_image`写入成功，
 `/home/qdm/stock-screener/.last_good_image`已存在。

## [0.2.117] - 2026-11-21

###修复（v0.2.116的验收门禁其实是空的）

Codex与独立评审同时指出：v0.2.116那道门禁**看起来在验收，实际上什么也没验。**

- **致命：等不到任何东西**。[严重]
原逻辑「三个服务都 `running`就 break」，而 `up -d`返回时容器状态就是
 `Up0 seconds`——**第一轮就成立，0.8秒返回 OK**，`sleep3`从未执行。
评审在81.4上实测：`real0m0.807s`。部署后30秒才崩的容器、
healthcheck不通过、配置装配错误，**一个都等不到**。`--since4m`
因此既不误报也无用处。

→改成四段式：**先无条件 settle20秒**（期间持续查
 `Restarting`/`unhealthy`）→等 `running` → **等 healthcheck变 `healthy`**
 （`start_period`300秒的 jobs也在内）→最后扫日志。
实测同一脚本：`real0m21.7s`，手工 `docker stop jobs`后0.24秒即
 `SS_VERIFY=FAIL:容器已退出`。

- **`Exited`判据扫到了不该扫的容器**。[严重]
`docker ps -a --filter name=stock-screener-`把 `ntfy`/`cloudflared`也扫进来，
而部署只重建 `api jobs agent`。用户为省内存手工 `stop ntfy`之后，
**每个版本都会被判失败并自动回滚**。
 → `Restarting`/`unhealthy`对所有容器算（硬故障），`Exited`只对本次
重建的服务算。

- **「没验成」被当成「部署坏了」**。[严重]
ssh抖动、脚本异常会返回空输出——原实现把它和"确认失败"一样**自动回滚**。
一次网络抖动就会把成功的部署切回旧镜像并 recreate生产容器。
 →区分 `FAIL:`与 `UNKNOWN:`，`UNKNOWN`只报警、**不自动回滚**。

- **从不校验"跑的到底是不是新镜像"**。[中等]
只查 `Names`/`Status` → build静默失败、`up`没 recreate都会判 OK。
 →构建后取镜像 ID，验收时逐个 `docker inspect .Image`比对。

- **回滚点可能被坏镜像污染**。[中等]
原来从"当前 latest"快照 rollback。若上次回滚失败，`latest`停在坏镜像上，
下一次快照就把**已知坏版本**变成回滚点。
 →目标机记 `.last_good_image`（验收通过才写），快照优先用它。

-回滚后重新验收一次（回滚本身也可能失败）；回滚失败区分
 `NO_IMAGE`/`TAG_FAILED`/`UP_FAILED`；`_capture`加600秒超时。

**测试**：6条，其中三条变异验证会红在断言——把 `settle`去掉、
把「整行等于 `SS_VERIFY=OK`」放宽成"包含"、把 `UNKNOWN`也回滚。
补的两条用例 `prefix SS_VERIFY=OK` / `SS_VERIFY=FAIL:上次是 SS_VERIFY=OK`
专堵评审发现的那个漏洞（原四个用例区分不出 `in`与 `startswith`）。

**顺带**：这轮写 `MARKER_TAGS`时又漏了定义，**pyflakes门禁当场报出
 `deploy.py:202: undefined name 'MARKER_TAGS'`**——v0.2.115加的那道闸
在这一轮抓到了第一个真实的同类错误。

## [0.2.116] - 2026-11-21

###新增（部署后自动验收 +失败回滚）

`deploy.py`原先重启完只打印一句「下一步请自行核对 /healthz」就 **exit0**。
v0.2.114那次 jobs容器重启循环，就是靠人盯 `docker ps`才发现的——
**部署是否成功，由人肉检查决定**，这本身就是个缺陷。

现在：

- **构建前自动快照**：把当前 `stock-screener:latest`打成
 `stock-screener:rollback`。不做这一步就没得回滚——`compose build`
会直接覆盖 latest，容器引用的也是同一 tag。
- **重启后自动验收**（默认最多等90秒，`--verify-wait`可调）：
 -有容器处于 `Restarting` / `Exited` / `unhealthy` →立刻失败，不等满；
 - `api`/`jobs`/`agent`必须都在 running；
 - **扫最近4分钟的启动日志，出现 `Traceback`即失败**。只看容器状态会被骗：
重启循环里它可能短暂显示 `Up`，真正判据是它有没有在异常里打滚。
- **验收不过自动回滚**：重新打 tag + `--force-recreate`，然后明确提示
 「仍需人工确认」。回滚失败（没有 rollback标签）也会说清楚。
- **只有明确见到 `SS_VERIFY=OK`才算成功**。ssh失败、脚本报错、输出被截断
一律不算——宁可误报失败，不可把故障当成功。

真实验证（81.4）：

-健康状态跑一遍 → `SS_VERIFY=OK`；
-手工 `docker stop` jobs再跑 → `SS_VERIFY=FAIL:容器异常
 stock-screener-jobs-1 Exited (137)`，随后恢复。

测试6条，两条变异验证会红在断言：把回滚改成 `if False`（「验收失败但没有回滚」）、
把「无结论」当成功（「输出 ''不该被判成功」）。

顺手修了一条旧测试的范围：`test_clean_check_precedes_any_remote_work`
原来在全文件里找第一个 `"ssh"`，新增的辅助函数定义在 `main()`之前、也含
`"ssh"`，于是把「定义位置」误当成「调用位置」。收窄到 `main()`内部——
安全属性（先检查、后传输）不变。

## [0.2.115] - 2026-11-21

###修复（v0.2.114造成了线上故障：jobs容器重启循环）

**这是我这一轮最严重的一次失误，必须写清楚。**

v0.2.114部署后，`jobs`容器立刻进入重启循环，生产中断约20分钟：

```
File "/app/jobs/main.py", line927, in main
 data_dir=data_dir,
NameError: name 'data_dir' is not defined
```

我给 `startup_recovery(...)`加实参时，**没有核对 `main()`作用域里到底有没有
这个变量**——那里根本没有。`startup_recovery`的 `data_dir`参数默认值就是
`"data"`（与生产 `/app/data`一致），本来不需要传。

**1349条测试全绿**，因为**没有任何测试调用 `main()`**——它是进程入口，
测试只覆盖它下面的函数。这一整类「入口函数里的名字写错/漏定义」，
现有测试体系是**结构性看不见**的。

修复与改进：

-去掉那条实参。
- **新增静态门禁 `tests/test_static_undefined_names.py`**：用 `pyflakes`
扫 `jobs/api/agent/core/notify/chat/scripts`下所有源文件，
出现 `undefined name`就红。已用「把这次崩溃原样加回去」验证：
门禁精确报出 `jobs/main.py:927:34: undefined name 'data_dir'`。
- `pyflakes==3.4.0`加进 `requirements-dev.txt`。

门禁顺带查出两处存量问题（运行期无害，但 `typing.get_type_hints()`会炸、
且读代码的人会以为名字可用）：

- `notify/dispatcher.py`：注解里用 `datetime`，但只导入了 `timedelta`。
- `agent/loop.py`：`pusher: "AgentPusher | None"`引用了未导入的名字，
改成 `TYPE_CHECKING`下导入（顶层导入会与 `agent/push.py`成环）。

**教训**：这一版我改动的是「启动路径」，却只用单元测试验证了被调用的函数
本身。启动路径、入口函数、配置装配这类代码，单测天然覆盖不到——
必须配合静态检查或真正启动一次。

## [0.2.114] - 2026-11-21

###修复（独立评审 + Codex复核 v0.2.113，四条都成立）

- **锁被占用时返回 `None`，会误发 P0**。[严重]
 `backup_db()`返回 `None`在 `backup_job`里的语义是「真失败」→发 **P0**。
而锁被占用是**部署重叠窗口里的正常现象**——评审实测跑出的告警原文是
 `('backup', '备份失败或完整性校验未通过', 'P0')`。讽刺的是：这把锁加进来
的前提就是"重叠确实可能发生"，于是一旦真发生，就必然产生一条最高级别假警报。
 →新增 `BackupBusy`异常，`backup_job`单独接住、只记 warning并 return。

- **`except OSError`太宽，会把"锁机制坏了"当成"别人在跑"**。[严重]
 `flock(LOCK_NB)`竞争抛的是 `BlockingIOError`；写成 `except OSError`
会把 `ENOLCK`/`EACCES`/`EIO`一起吞下——`BackupBusy`版本下后果更隐蔽：
**真故障变成静默跳过、零告警**。
 →收窄成 `except BlockingIOError`；`import fcntl`也从函数内提到模块顶部
 （放函数里的 `try`中时 `except OSError`接不住 ImportError）。

- **两条测试是假绿**。[中等]
① `BACKUP_STALE_H`那条只断言"常量落在区间"，不调函数——评审的存活变异
 （常量仍29、但函数默认参数写死 `stale_h=36`）**全绿**，真实阈值已退回 bug。
② `test_busy_backup_does_not_alert_p0`用 `inspect.getsource`文本匹配——评审
的变异 H（把 `except Exception`挪到 `except BackupBusy`前，子类被父类先接住）
**照样全绿**，行为已经退回发 P0。
 →都改成行为测试：构造"最新备份33.4h前"断言真的 `alert`；拿住锁后调真实的
 `backup_job`、断言 `dispatcher`一条告警都没有。两条都用变异验证会红在断言。

- **伴生文件零宽限，主体却守2小时**。[中等]
 `prune_orphan_tmp`第二遍清孤儿伴生时不看 mtime，而第一遍对主体守2小时——
契约不对称。正常流程虽是"先建主体再产生伴生"，但一旦出现"伴生已建、主体
还没建"的瞬间就会被误删。→第二遍也加 mtime宽限。

- **`misfire_grace_time=3600`并不能让重启后补跑**。[中等]
测试注释写着"晚一小时开机也要补跑"——**错的**。评审对 APScheduler实测：
 `now=00:10`时 next fire是次日23:30，不补跑。所以"进程在23:30不在"=那次
备份永久丢失；而陈旧检查只挂在08:55单次 cron上（`misfire_grace_time=300`），
容器若恰好那时不在，当天检查也丢。
 → `startup_recovery`里补一次备份陈旧检查（幂等、只读文件），并更正注释。

- **周末的自检会真去拉行情、发 ntfy、调 LLM**。[中等]
上一版为了让断档在周末也能发现，把 `self_check_job`改成每天——但
 `self_check()`本身会 `snapshot`一次行情、探 ntfy、跑一次 `max_tokens=1`
的 LLM。**为了修一个缺陷而扩大了另一个行为**。
 →拆开：`backup_health_job`（清半截 +陈旧告警）每天跑；`self_check`恢复
 mon-fri。

-顺手：`check_backup_freshness`删掉死参数 `keep`（函数体从不使用，
容易让人以为它管保留份数）。

## [0.2.113] - 2026-11-21

###修复（备份并发互斥）

- **两个备份进程会写同一个临时文件**。临时文件名由日期决定
 （`.market-YYYYMMDD.db.tmp`），两个进程同时跑会写同一个文件、
内容交错，而 `PRAGMA integrity_check`可能照样通过——**结果是一份
 "看着正常、其实混了两个事务"的备份**。这比直接失败更危险：
失败会被 P0告警抓到，混事务的成功不会。

 `APScheduler`的 `max_instances=1`只管**同一个调度器实例**，管不了
 "旧容器还没退、新容器已经起"的部署重叠窗口（`deploy.py`用
 `--force-recreate`通常没有重叠，但那是概率不是保证）。

 → `backup_db()`入口加 `flock`排他锁，拿不到就返回 `None`、
 **一个字节都不碰**。锁文件 `.backup.lock` **常驻不删**：删掉再建的瞬间，
两个进程会各拿到一把"路径相同但 inode不同"的锁，互斥就失效了。

测试：手工持锁后调 `backup_db()`，断言返回 `None`且没生成任何文件；
锁释放后能正常备份（证明拒绝的是"并发"而不是"永远坏掉"）。

 **一个操作教训**：这条改动我花了比预期长得多的时间，原因是我在这个会话里
反复用 heredoc /手打空格写代码，行首缩进被吃掉，导致补丁静默不生效
 （改动静默失败比报错更坑——测试还是绿的）。最后改用"用脚本按层级
计算缩进再生成文件"才稳定下来。

## [0.2.112] - 2026-11-21

###修复（独立评审 + Codex复核 v0.2.111，三条都成立）

- **陈旧阈值36h让单晚断档永远不报警**。[严重]
自检在次日08:55跑，备份在23:30——**单晚没跑成时 age恰好是33.4小时**，
我设的36小时它永远摸不到，连漏两晚（57.4h）才喊。等于对最常发生的
那种故障完全失明，而我还在上一版 CHANGELOG里说"靠08:55陈旧告警兜底"。
改成 **29h**：大于正常间隔（9.4h，否则天天误报）、小于单晚断档（33.4h）。

- **自检只在工作日跑**。[中等]
周五23:30若被中断，周六周日都不检查，到周一 age已80+小时——
会报，但**周末两天没有任何告警窗口**。数据库7×24在写，
备份检查没有理由跳过周末。改成每天08:55。

- **`prune_orphan_tmp`的 glob带尾随 `*`，会把伴生文件当成独立候选项**。[中等]
 `.market-*.db.tmp*`让 `-shm` / `-wal`各自按自己的 mtime判断：
主文件还新鲜（正在备份）、伴生文件却超过2小时，伴生会被**单独删掉**，
等于破坏正在进行的备份。改成只遍历主体 `.tmp`，伴生跟着主体走；
另加第二遍扫描清理"主体已不存在的孤儿伴生"。

- **两条测试假绿**（评审用变异抓出，我原来的测试不红）：
① `removed`列表把"删失败"也记成成功——日志会声称清了但磁盘上还在；
② `backup_db()`起点的清理没有测试经过真实入口。
各补一条测试，四个变异（时间过滤、伴生删除、返回值真实性、起点清理）
现在都红在断言本身。

**校准后的认识**：`-shm`/`-wal`在正常备份流程里**不会产生**（目标库是
DELETE journal，`dest.close()`后就没有了）——它们只在备份中途被 SIGKILL、
目标连接没正常关闭时才残留。所以这两个分支是中断场景的专用防御。

## [0.2.111] - 2026-11-21

### 修复（备份"消失"的真因：进程被中断，半截文件藏起来了）

上一版我判断 11-20 的备份是"部署压在 23:30、cron 被静默跳过"。**这个判断是错的**。

现场看 `data/backups/`（`ls -la`，不是 `ls`）：

```
.market-20261115.db.tmp   1.2 GB   11-15 23:30
.market-20261120.db.tmp   1.3 GB   11-20 23:30
.market-20261121.db.tmp   1.3 GB   11-21 13:52   ← 我手工那次
```

11-20 **跑了**，只是停在"写临时文件"阶段——部署重启把进程杀了，
`PRAGMA integrity_check` 之后的原子改名永远没发生。手工复现改名步骤是成功的，
所以不是改名逻辑的问题，是**进程被中断**。

- **半截备份永远不会被清掉**。它们必须以 `.` 开头（这样 `_prune` 的
  `market-*.db` 不会把它们当成正式备份），代价是**任何清理逻辑都匹配不到**，
  `ls` 也不显示。三份共 ~3.9 GB 白占，谁都不知道。
  → 新增 `prune_orphan_tmp()`：起点和每天 08:55 的自检里各跑一次，
  只清 **2 小时以上**的（不碰可能正在写的那份）；伴生的 `-shm`/`-wal`
  **跟着主体一起删，不看它们自己的 mtime**——主体都没了，它们已经是孤儿，
  而 SQLite 关闭时可能重写 shm，按 mtime 判会被当成"新鲜"漏掉。

- **`backup_db()` 开头也先清一次**，免得历史遗留的同名 `.tmp` 和新的一起混着。

- **诚实更正**：上一版 CHANGELOG 里"备份只有 `misfire_grace_time=3600` 兜底，
  部署压在 23:30 时会静默跳过"这段结论**没有证据支持**，真实机制是
  "跑了、没写完、被中断"。保留陈旧告警（它确实抓到了这次问题），
  但把归因改对。

**这个 bug 的形状值得记一笔**：隐藏文件 + 原子改名的失败模式 = 外部只能看见
"今天没有备份"，看不出"备份写了一半"。如果不是手工去 `ls -la`，
它会一直看起来像"cron 没跑"。

## [0.2.110] - 2026-11-21

### 修复（Codex 复核的剩余三项）

- **CI 的 pytest 也没挂 `--tmpfs`**。上一版只改了 `Makefile test` 与
  `test-integration`，`.github/workflows/ci.yml` 仍是裸 `docker run`。
  CI runner 虽然一次性，但 tmpfs 让临时文件**物理上不可能落到磁盘**
  （`size=2g` 再封顶）。`exec` 同样不能省：`--tmpfs` 默认 `noexec`。

- **`host_disk_guard.sh` 的可回收空间指引会把人带反方向**。
  它把所有 Reclaimable 加成一个总数、统一建议 `docker builder prune`——
  而**停着的容器**（事故里那 230 GB 的 `ss-dev-run` 正是这一类）
  `builder prune` 一个字节也回收不了。改成按来源分开报：
  Build Cache → `builder prune`、Images → `image prune`、
  Containers → `container prune`（并提示会删掉所有已停止容器）、
  Local Volumes → `volume prune`。

- **`conftest` 的临时目录守卫只覆盖了 `mkdtemp`**。`mkstemp()` 与
  `NamedTemporaryFile(delete=False)` 同样没有自动清理语义，将来有人在测试里
  用了就又在 /tmp 里堆。现在两个入口一起收进 `tmp_path`，并各加一条测试
  （取消重定向就红）。

- `test_remote.sh` 的打包排除项补上 `.firecrawl` / `.superpowers` /
  `.tmp_smoke` / `.mypy_cache` / `.ruff_cache`，别把本地工具的缓存传上去。

## [0.2.109] - 2026-11-21

### 修复（Codex 复核 2026-11-21 磁盘修复，四条都成立）

- **`disk_job` 没持 `loops.lock`**。[严重]
  我在注释里写着"只读 `shutil.disk_usage`，不需要锁"——**这是错的**：
  越阈值时会走 `dispatcher.alert()` → `_push_log()` 的 `BEGIN IMMEDIATE`，
  写的是**同一个 conn** 的 `push_log`。磁盘越线的那一刻正赶上
  tick/心跳/快照线程，事务状态就乱了。这正是 `cleanup_job` 上面那段注释
  早写下的同一个理由。现在 `disk_job` 和 08:55 的备份陈旧检查都在写锁里。

- **三档磁盘告警共用 `kind="disk"`，升级会被自己的去重压掉**。[严重]
  `Dispatcher.alert` 的 30 分钟冷却判据是 `kind + 时间`，**不看 level**。
  75% 先发一条 P2 之后，10 分钟内升到 95% 的 P0 会被"同 kind 刚发过"
  直接吞掉——**最该升级的那次反而发不出去**。改成分档 kind
  （`disk_p2` / `disk_p1` / `disk_p0`），各自去重。

- **`host_disk_guard.sh` 用了 GNU 专有的 `df --output=pcent`**。[中等]
  macOS 的 `df` 没有这个选项，取值静默变成 0——在**事故所在的那个平台**上
  读成 0%，而磁盘已经写满。改成 POSIX 的 `df -P`。同时在文件头写明：
  这个脚本面向 Linux 宿主机；Docker Desktop for Mac 的 `df /` 看不到那
  294 GB 虚拟磁盘，那种场景要查 Docker Desktop 自己的镜像文件。

- **`test_remote.sh` 并发不安全**。[中等]
  固定用 `/tmp/ss-src.tar` 和 `/tmp/ss-runner.sh`，两次同时跑会互相覆盖源码包；
  `ARGS="$*"` 还会把带空格的参数（`-k "a b"`）拆坏。
  改成**本地生成 RUNID 并写死进 runner**（远端自己生成过一次，名字对不上，
  实测报 `tar: Cannot open`），tar/runner/WORK 全部唯一，参数用 bash 数组
  逐项 `%q` 转义后拼接。

- `make test-integration` 也补上 `--tmpfs`（原来只有 `make test` 有）。
- RUNBOOK §12 的清理指引补上 `image prune` / `volume prune`，并写明
  "看 Reclaimable 那一列，不止 Build Cache"。

## [0.2.108] - 2026-11-21

### 修复（2026-11-21 磁盘写满事故）

本机 Docker Desktop 虚拟磁盘写满（294.65/294.68 GB），PostgreSQL 起不来：
`could not write lock file "postmaster.pid": No space left on device`。
元凶是**项目自己的长期测试容器** `ss-dev-run`（`sleep infinity`，从不删除）：
`docker system df` 显示容器占用 **230.4 GB**、其中 230.2 GB 可回收，
而镜像只有 654 MB；`docker diff` 在它的 `/tmp` 下统计到 **33 万个变更路径**。

根因两条，各修一条。**没有连续采样，所以不声称知道每一 GB 的归属**——
能确认的是数据类型（临时 SQLite、审计快照、变异副本、`__pycache__`）和
"长期复用 + 从不清理"这个工作方式。

- **测试写出来的临时目录从不清理**。`tests/chat/test_session.py` 12 处、
  `tests/jobs/test_main_scheduler.py` 1 处直接 `tempfile.mkdtemp()`。
  → `tests/conftest.py` 加 autouse fixture，把 `tempfile.mkdtemp` 重定向到
  本用例的 `tmp_path`（pytest 自动清理）。**用猴补而不是逐个改调用点**：
  以后新写的测试自动受约束，不靠人记得。`tests/test_no_manual_internal_state.py`
  里加一条测试钉住它（取消重定向就红）。

- **容器长期不删**。`make test` 用的是 `docker run --rm`，但日常是
  `docker exec` 进一个 `sleep infinity` 的容器，**绕过了清理**。
  → `Makefile` 的 `test` 加 `--tmpfs /tmp:rw,exec,size=2g`：临时文件进内存、
  随容器销毁、容量封顶。**`exec` 不能省**——Docker 的 `--tmpfs` 默认 `noexec`，
  `/tmp` 里的脚本执行不了，`tests/test_release_script.py` 的 fake git 正好在那，
  会静默退回真实 git 然后报 `fatal: not a git repository`（实测 4 条假红）。
  → 新增 `scripts/test_remote.sh`：只在测试机上跑，必须 `--rm`、每次独立目录、
  **不 `docker exec` 进生产容器**（往生产容器写文件正是这次事故的形状）。

### 修复（监控前移：原来一天只查一次）

- **磁盘巡检从"每日 16:00 一次"改成"每 10 分钟一次"**。原来它埋在
  `cleanup` 里、阈值 85/95；而磁盘从 70% 涨到 100% 只花了几个小时，
  等 cleanup 跑起来已经晚了。现在 `jobs/cleanup.py::check_disk()` 独立成任务
  （`jobs/main.py` 的 `disk_job`，**与交易日和盘中窗口无关**——磁盘不看开市），
  三档 75/85/95，75% 就提前喊。监控的是**宿主机根分区**：容器里 `df /`
  看到的就是宿主机文件系统，Docker 的 `/var/lib/docker` 也在上面。

- **新增 `scripts/host_disk_guard.sh`（宿主机侧）**。容器里看不到
  `/var/lib/docker` 的可写层明细（该目录在容器内是空的），所以"某个容器在偷偷长"
  这个信号只能在宿主机拿。它盯三件事：分区水位、**单容器可写层**、
  Docker 可回收空间。**只报告，不自动删**——删什么属于运维决策。

- **备份漏跑会被喊出来**。2026-11-21 实测的真实缺口：`data/backups/` 里有
  11-13/14/16/17/18/19，独独缺 **11-20**——那晚连着几次部署，jobs 容器在
  23:30 前后被重建，而备份只有 `misfire_grace_time=3600` 兜底，过了就静默跳过。
  7 份保留位出现空洞，直到真要恢复的那天才发现。
  → `check_backup_freshness()` 在每天 08:55 的自检里查最新备份时间，
  超 36 小时告警。

- **dev 镜像补 `git`**：`tests/test_version.py` / `test_release_script.py` /
  `test_scan_secrets.py` 都要读 git 历史与 tag，缺 git 不是跳过而是假红。

- **`deploy/RUNBOOK.md` 加 §12.1**：测试容器不许长期留守，附事故数字与
  两条硬规矩（`--rm`；临时文件落 tmpfs / `tmp_path`）。

**没有做的事**：没有连续空间采样，也没有删除前各目录的大小清单，
所以**无法给出真实增长曲线、也不能断言 230 GB 全部来自 `/tmp`**。

### 清理（死代码，审计清单里的低危项）

- **`core/db.py::connect_lite()`**：零调用。`api/main.py` 走的是
  `make_conn_factory(migrate_each=False)`，其内部仍 `connect()`——
  这个"只建连接不 migrate"的独立入口是重复品。

- **`core/snapcache.py::invalidate()`**：零调用（连测试都没有）。缓存按 `as_of` 自失效。

- **`agent/main.py::next_session_start()`** + 它的孤儿测试：零生产调用
  （真用的是 `in_session()` / `should_wake()`）。

- **`agent/push.py` 里 `ttl_h = getattr(...)`**：赋值后从不使用，下一行自己重算 `ttl`。

- **`jobs/main.py` 的死导入 `EodReport`**：函数体里一次都没用，`# noqa: F401` 把它盖住了。

- **`agent/schedule.py` 的误导性 `# noqa: F401`**：`SH` 在 `:127` 真被用了，
  注释却说"`Deps` 组装点需要"，让 `SH` 看起来是"已授权未使用"。

**两个刻意留下的**（并补了说明，免得下次又被当死代码删掉）：

- `core/adjust.py::day_factor()` / `factors_at()`：生产不调（`rebuild_adj` 内联了
  同样算法），但 `tests/core/test_adjust.py` 用它们逐条校验复权数学——
  删掉等于删掉那组断言。
- `core/db.py::write_retry()`：**确实没有生产调用者**，而模块 docstring 写着
  "写事务重试"。这是一处**已知缺口**：`notify/dispatcher.py` 的 `BEGIN IMMEDIATE`
  是裸调用，撞锁时靠 `busy_timeout=5000` 兜着，兜不住就抛。
  接线要重构那段事务，风险大于收益，留待单独一轮——这里写明是为了
  不让"文档说有重试"继续误导。

## [0.2.107] - 2026-11-21

### 修复

- **预算门控的输出侧写死了 `MAX_TOKENS`，与实发量脱钩**（Codex 复核）。
  `agent/main.py::_budget_exhausted_now()` 判的是 `rem_out < MAX_TOKENS`（2000），
  而请求真正发的是 `_max_out(deps)`（读 `llm.max_tokens.tools`）。
  配置改成 1000、余额剩 1500 时，请求**发得出去**却被判成"额度耗尽"，
  agent 整个下午不再开口——正是这次事故要消灭的那种"还有额度却停摆"。
  现在两侧都走 `_max_out(deps)`。

- **`jobs/main.py` 两处 `trust_env=False` 也接线**。上一版只接了行情/LLM/推送/MCP
  四个出口，自检与上游探活这两处仍是字面量——同样会让
  `direct_connection` 变成"配置说走代理、实际只有一部分听"。

## [0.2.106] - 2026-11-21

### 修复

- **配置漂移检查的判据反了**。`_DRIFT_KEYS` 的判据是"键存在与否"，
  把"已删除"的 `own_history_trim` 加进去之后，**删干净了反而报漂移**
  （实测 deploy 直接打出 `[配置漂移] config/agent.yaml 里没有 own_history_trim`）。
  "应该存在"和"应该不存在"是两种判据，不能共用同一张表。

## [0.2.105] - 2026-11-21

### 修复（死配置审计：看起来配了，其实永远不生效）

一次全量审计（枚举 `config/*.yaml` 全部叶子键 → 统计非测试代码里的读取点）
找出 7 处"配置写了但没有任何读取点"。这类假象最费时间：按注释去改配置，
改完毫无变化，还以为是别的原因。

- **`agent.yaml::heartbeat.*`（5 键）**。真值原本写死在 `agent/schedule.py`，
  且与配置**数值恰好相等**（2/5/10 min ↔ 120/300/600 s）——改了不生效却完全
  看不出来。现在 `next_heartbeat(now, hb)` 接 `cfg.agent.heartbeat`，时段表也读配置。

- **`llm.yaml::llm.max_tokens.plain/tools`**。三个 `llm.chat()` 调用点全部显式
  传值，而 `default_max_tokens()` 只在 `max_tokens` 为假时才生效。遇到
  「空正文 / `finish_reason=length` / JSON 截断」时第一反应就是调这个值。
  顺带把 `plain` 从 800 改成 **1000**（代码实际一直用 1000）——接线不能顺带
  改运行时行为，否则重写路径会被悄悄截断。

- **`agent.yaml::context.sector_events / own_history`**。真值写死在
  `agent/context.py`，而**同一段的 `context.max_chars` 却是真读的**。
  `own_history_trim` **删掉了**：唯一没接线的键，也没有明确语义。

- **`llm.yaml::llm.thinking`**。注入的是字面量 `"disabled"`，读不到配置。

- **`notify.yaml::p1_score`**。旧行为是"非 P0 的突破一律 P1"，60 分闸门不存在
  （直接放大 `p1_merge` 的合并压力）。现在接线，默认 `0 = 不设闸门`，
  与旧行为完全一致。

- **`strategy.yaml::direct_connection`**。**四处出口全部接线**（行情 Http、
  LLM、推送 ntfy、MCP westock）。原来只接行情层一处，是半接线：配置说
  "走代理"却只有行情层听，比完全没有这个开关更误导（本项目专门有 §6.2）。

- **`ToolRegistry._returns` 删掉**。跨回合**无界累加**：`begin_turn()` 生产零调用
  （`agent/loop.py` 早改用回合级局部变量），但 `invoke()` 仍每次 append 一份
  完整信封，`_cache` 有 256 上限它没有。agent 进程里注册表是全程单例，
  一个交易日的 K 线/板块历史全在里面。

### 修复（第七轮评审：接线了但**生产调用点**没接，第 8 次假绿）

派生函数本身都测到了，但**生产调用点**没有一条测试经过。把调用点改回旧写法，
测试**全绿**——正是这一整天反复出现的那个形状：

| 变异（改回旧写法） | 结果 |
|---|---|
| `main.py` 三处 `next_heartbeat(now, self._hb)` → `next_heartbeat(now)` | 72 个测试绿 |
| `loop.py` 四个 `_max_out(deps)` → 常量 | 101 个测试绿 |
| `chat/session.py` `out_tokens` → `MAX_TOKENS` | 17 个测试绿 |
| `context.py` 两处调用点 → 不传 cfg | 11 个测试绿 |

四条都补了走**真实入口**（`_once()` / `ask()` / `render_event()`）的端到端断言，
现在每个变异都红在断言本身。

- **`next_heartbeat` 加配置异常兜底**。`core/config.py` 只校验这五个键"存在"、
  不校验类型，所以 `tight_min: "2min"`、`tight:` 留空都写得进去；而 `_once()`
  在推进心跳前**裸调用**它、`run()` 又吞异常重试——一次类型错误就会让
  心跳/闹钟/开盘回合全部停摆（v0.2.93 修过同一形状）。现在退回默认节奏并记 warning。

- **`_max_out` 加正数 + 整数校验**。`tools: -5` 会发出非法 `max_tokens`，
  `tools: yes` 会被 `int(True)` 变成 1（每次空正文 → 一直重试），
  `tools: 2.9` 会被截成 2。

- **`_EMPTY_LLM` 的 `plain: 800 → 1000`**，与 `config/llm.yaml` 对齐：
  llm.yaml 缺失时重写路径会从 1000 变成 800。

### 运维（站点配置必须手工同步，否则行为变了却不自知）

`config/{agent,llm,notify}.yaml` 在部署时**保留在目标机**（这是对的：站点有自己的
LLM 端点与预算）。但这一版把三处**从死配置变成了真生效**，站点还拿旧值就会
立刻出现行为差异：

| 键 | 站点旧值 | 仓库新值 | 不同步的后果 |
|---|---|---|---|
| `llm.max_tokens.plain` | 800 | 1000 | 重写路径被悄悄截断 |
| `notify.p1_score` | 60 | 0 | 60 分以下的突破被降级成 P2 |
| `agent.context.own_history_trim` | 存在 | 已删 | 死键，读不到也无害 |

生产已同步，并把这三处加进 `deploy.py::_DRIFT_KEYS`——以后升级会主动提示差异。

**教训**：`release.py ... 2>&1 | tail -1 && deploy` 里管道的退出码是 `tail` 的，
release 失败被吞掉、deploy 照样跑了一遍（版本号没变、代码已新）。
以后 release 必须单独检查退出码与版本号。

## [0.2.104] - 2026-11-20

### 修复

- **澄清第七轮评审（Codex）的一条"严重风险"：那是口径混用，不是真的低估**。

  Codex 报：`_est_in` 用 `len(str(c))` 抓字符，而 `str()` 比紧凑 JSON 长约
  1.9~8.8%，所以实测最大 `0.530` 按真实 JSON 字符应修正到 `0.54~0.58`，
  0.7 只剩 21~30% 余量，可能穿过真实花费上限。

  但**这个推论把两个分母混用了**：实测比值是 `real_in / (est_old/1.2)`，
  而 `est_old = chars_str * 1.2` ⇒ 比值分母是 **`chars_str`**；新估算
  `est_new = chars_str * 0.7` 的分母**同样是 `chars_str`**。`str()` 膨胀在反推和
  估算里同比例出现，完全抵消。按 `chars_json` 口径折算，两边一起乘 1.088，
  比值不变（0.530/0.7 = 0.757）。

  新增 `test_repr_inflation_cancels_out_in_the_reverse_derivation` 用真实
  `ToolRegistry` 钉住这个抵消关系，防止将来真有人按错误口径把系数调小。

- **新增预扣余量探针 `budget_under_reserved`**（`api/watchdog.py`）。系数是按
  11-20 的实测校准的，但样本之外永远可能更糟——中文密集的长段落 token/字符
  会高于 JSON 密集的。这条规则取最近 50 次调用的 `real_in / reserve_in` 中位值，
  超过 85% 就报警，提示调高系数。**它不会自动改系数**（钱的事不自动决定），
  只负责别让余量悄悄漂掉。

- **系数抽到 `llm/budget.py::CHARS_TO_TOKENS`，agent 与 chat 两池共用**。
  原来 `agent/loop.py` 与 `chat/session.py` 各写一份 `1.2`，改一处就会漂——
  第六轮评审正是靠"chat 池比值偏低"发现探针会被稀释的。探针查询也限定
  `pool='agent'`，否则 chat 行会把中位值往下拉，agent 真低估时反而报不出来。

- **`AlertKind` 补上 `budget_under_reserved`**：不带标题时手机上收到的是裸
  标识符（`notify/templates.py` 的注释点名过这个 bug 类）。补上后
  `test_all_production_alert_kinds_have_chinese_titles` 也能覆盖到它。

- **探针加 `ts <= now` 上界**：churn 规则当初就是踩了这个坑才加注释的，
  离线回放时"未来"的行会把复盘结论带偏。

- **修 `config/llm.yaml` 里漏改的 `字符数×1.2`**（那段注释本身就是"重算额度"
  的作业指导，留着旧系数会让下一次调额度的人按错的算）。同时写清楚：
  **输出维度才是容量瓶颈**——每次 attempt 固定预留 `MAX_TOKENS=2000`、实测真实
  输出只有约 230，所以改后的实际容量是 **1.4~1.5 倍**（不是理论上的 1.7 倍）。

## [0.2.103] - 2026-11-20

### 修复

- **字符→token 系数系统性高估 3.1 倍，暴跌日额度半天用完**（v0.2.103）。

  11-20 5.5M 输入额度在 **13:27** 就烧完了，之后两小时 agent 停摆。
  查下来不是"额度配少了"，而是**估算公式错了**：

  旧实现 `chars * 1.2`（"中文 1 字 ≈ 1 token，再留 20%"）。用生产
  `token_audit` 的 247 条真实 `prompt_tokens` 反推，实际比值是

      中位 0.383   P90 0.475   P99 0.511   最大 0.530   token/字符

  因为上下文里**大半是 JSON**（工具 schema、工具返回），JSON 的引号、逗号、
  括号 tokenize 得极省，实测只有 ~0.43 token/字符；中文正文也不到 1。
  所以 1.2 高估 3.1 倍：真实只花了 1.87M，5.5M 就见底了。

  换成 **0.7**——对实测最大值 0.530 仍留 **32% 余量**（预扣必须偏高：低估会让
  真实花费突破额度上限，而扣减没有退款路径）。同样的额度现在覆盖约 **1.7 倍**的回合。

- **`MIN_TURN_EST_IN` 跟随系数同步缩放**（12000 → 7000）。两者口径不一致时，
  "最小回合放得下"的判据就和估算脱钩：系数改小后若忘了同步，还有额度却会被
  判成用完。

- **新增 `tests/agent/test_est_calibration.py`**：把"永不低估"和"不要高估 3 倍以上"
  两条线用实测数据钉住，并强制 `MIN_TURN_EST_IN` 与系数同步。
  变异验证：系数改 0.4 → 红在"低于实测最大"；改回 1.2 → 红在"高估 3.13 倍"。

## [0.2.102] - 2026-11-20

### 防复发

- **机械检查禁止"手设生产路径推导出来的状态"**。今天 7 次假绿里 **5 次**是同一个
  形状：在测试里直接给对象的内部字段赋值，跳过生产路径，于是测试验证的是
  "我设的那个值"，不是"代码算出来的值"。第 7 次尤其说明问题——写它的前一小时
  我还在批判同一件事，还是犯了。这不是"下次注意"能防住的。

  新增 `tests/test_no_manual_internal_state.py`：用 **AST**（不是正则，避免误伤
  注释与文档字符串）扫描 `tests/` 下所有赋值，出现 `budget_exhausted` /
  `_quota_exhausted` / `_budget_fail_streak` / `_budget_cancels` /
  `_rounds_cancels` 的直接赋值就报错。变异验证：恢复一行手设立刻转红。

- **清掉两处遗漏**：`test_budget_exhausted_skips_event_poll_entirely`
  （第 7 次假绿的本体）与 `test_budget_exhausted_flag_and_reset`，
  都改成从 `llm_budget` 出发。

- **`test_budget_cancelled_alarms_notify_the_user_once` 原来手写 `main._run`
  替身再手动 `+= 1`**，测的是"我加的那个数"，改成走真实 `_run()`。

- **`env` fixture 的 `Budget` 补上 `clock=clock`**：原来省略后用 `SystemClock`，
  写库与读库算的不是同一个自然日——测试与生产同时被这个不一致坑过。

## [0.2.101] - 2026-11-20

### 修复（Codex 第五轮报告的补充两项）

- **放弃便签会挤掉 P1 行情提醒**（Codex 实测）。`notify.daily_cap` 那份额度是给
  **行情信号**的（`notify/dispatcher.py` 明确它用来保证 P1/P0 不被淹没），
  而 `agent_note` 把它也算进去。Codex 实测：先写 59 条 accepted，再发一条 P2
  放弃便签，随后派发的 **P1 就被 suppressed**（P0 与运维告警仍放行）。

  现在运维性便签（`_flush_give_up` 那两条）**豁免总额度**，但仍受
  `channel_b_daily_cap`(15) 约束——不存在刷屏风险，而普通便签不受影响。

- **事件被静默丢弃，用户看不到**（Codex 实测）。跳过 `poll()` 后事件保持 `new`，
  下一交易日由 housekeeping 标 `expired`——**不会有永久残留**（这点 Codex 确认了），
  但用户完全不知道今天有 N 条盘中信号没被处理。现在合并通知里带上这个数。

- **测试自身也是假绿**：这条额度测试第一版只填到 `cap-1`，`>= cap` 判断不成立，
  豁免与否都放行——变异「不豁免」照样绿。改成填满 `cap` 后，「不豁免」立刻转红。

## [0.2.100] - 2026-11-20

### 修复（第五轮复核：第 7 次假绿 + 预算判定重构）

- **预算判定原来是纯内存状态，进程重启就清零 —— 第 7 次假绿**。

  第五轮评审实测：`_quota_exhausted` 全仓只有赋值、**没有任何持久化**，
  而**每次盘中部署都会重启**。重启后第一轮 `_once()` 照常 poll 出 8 个事件组、
  每组跑一个回合、各写一行 `rounds=0, latency_ms=0, decision=error`
  ——正是 11-20 14:16–14:20 那 8 行。**v0.2.99 的 poll 门控因此形同虚设。**

  测试为什么没抓到：`test_budget_exhausted_skips_event_poll_entirely` 直接手设
  `main._quota_exhausted = True`，绕过了生产路径——这正是本仓库自己批判过
  无数次的"手设状态"形状。

  修法：**判定完全由库推导**（`_budget_exhausted_now()` 读
  `llm_budget.remaining()`），重启后第一次调用就是真相。
  内存 latch 与连续失败计数全部删掉。变异验证：改回不读库，立刻写出 10 行空转。

- **`MIN_TURN_EST_IN = 2000` 与真实需求差 4.5 倍**。评审实测最小的心跳回合
  （`tools` 6793 字符 + `SYSTEM_PROMPT` 603 字符）`_est_in` = **8911**。
  改成 **12000**（实测 +35% 余量），并新增测试把常量与真实估算**绑在一起**：
  将来提示词或工具 schema 变胖，常量跟不上就立刻转红。

- **预检与轮内检查用两套口径，差 11 倍**。预检 `ceil(chars/2)+800` = 807，
  轮内 `_est_in` = 8911。预检几乎永远放行，真正的门一直是轮内那个。
  **预检已删除**——它既口径错又冗余，`run_turn()` 第一轮开头就做同样的判断。

- **心跳回合与开盘回合没有预算门控**。latch 之后它们照跑，每次在预检里失败、
  写一行空转。截至 11-20 收盘共 160 行空转里，除了事件那 8 行还有心跳的。
  现在两者都门控。

- **被放弃的闹钟一声不响**。11-20 那天因额度不足取消 35 条、加上轮数用尽，
  用户以为"30 分钟后回来看"，实际什么都没有。

  现在**按原因合并**成至多两条（额度不足 / 模型未收敛），每天每个原因最多一条。
  不自逐条推的理由：`channel_b_daily_cap` 只有 15、`notify.daily_cap` 60，
  35 条里 20 条会被压掉，而被压时 `_audit_cap()` 还会再写一行 `agent_turn`
  ——通知失败又制造噪声，还会挤占真正重要的行情提醒额度。
  具体是哪些闹钟，`agent_schedule.cancelled_reason` + `target` 都记着，日志页可查。

- **`_cancel_alarm` 不看 rowcount**，通知无条件发。现在"只推一次"靠取消的
  原子性（`rowcount == 1`），不靠"现在正好不会重入"。

- **`_note_seq` 把个股拼成 `note-index-`**：`_notify_alarm_give_up` 不再把
  `shXXXXXX` 传给 seq 构造（标的已经写在正文里）。

### 一致性

- `Budget` 构造时统一传入同一个 `clock`（`agent/main.py`、`chat/session.py`）。
  原来省略后用的是 `SystemClock`，与调用方的 `clock` 可能不是同一个"自然日"。

## [0.2.99] - 2026-11-20

### 修复

- **预算耗尽时连 `attention.poll()` 都不该做**（v0.2.99，生产实测）。

  `poll()` 会把事件从 `new` 标成 `escalated`，然后每组跑一个回合。预算耗尽时
  那些回合必然在预检里返回 `rounds=0, latency_ms=0` 的 error——白写一行。
  实测 11-20 14:20:42 一轮 poll 返回 8 组，**同一秒写了 8 行**空转。

  更关键的是**状态污染**：跳过 `_run` 但已经标了 `escalated`，事件就卡在一个
  "已升级但没人跑"的中间态，之后又被回收到 `pending_retry`、再失败转 `gave_up`，
  一条事件折腾两轮。现在预算耗尽直接不 poll——事件保持 `new`，
  跨日由 `heartbeat_housekeeping()` 标 `expired`，语义干净。

## [0.2.98] - 2026-11-20

### 修复

- **额度尾巴上的空转要在几次之后停下来**（v0.2.98，生产实测）。

  v0.2.97 部署后 13:48–13:58 仍看到 4 行 `rounds=0, latency_ms=0, decision=error`：
  额度剩 9056 / 5.5M，典型请求要 21888，于是每个心跳/事件回合各写一行。
  每 5 分钟一行不算事故，但正是 11-19 那种记录形状。

  **不能靠调大 `MIN_TURN_EST_IN` 解决**：那会把 Codex 实测的场景
  （剩 41366、大请求 41367、但心跳只需 8913 完全跑得动）又锁成"今天没了"。

  判定必须看**连续性**：`BUDGET_FAIL_STREAK_TO_LATCH = 3`。
  连续 3 次都因额度失败 → 认定这段尾巴里什么都跑不动，停止尝试；
  中途只要有一次成功（哪怕最小的心跳）→ 计数清零，不 latch。
  而"连最小请求都放不下"仍然**立即** latch——那说明无论上下文多小都跑不动。

## [0.2.97] - 2026-11-20

### 修复（v0.2.96 上线后当场发现的回归，生产实测）

- **「本次请求超出剩余额度」退回重试 → 10 分钟写 105 行空转**。

  v0.2.96 为了修「差 1 个 token 锁死整天」，把这条 why 和「额度真用完」分开了，
  但当时仍走 `release_alarm` 退回重试。而**额度只会更少**，同一条闹钟隔 60 秒
  再跑必然还是太大。

  上线后当场在生产看到后果：11-20 13:20–13:40（额度剩 9056 / 5.5M），
  35 条闹钟各退回 3 次，写了 **105 行 `rounds=0, latency_ms=0, decision=error`**
  ——正是 11-19 事故的记录形状，只是有 3 次上限兜着不会无限转。
  但 10 分钟 105 行照样把 churn 的 5 分钟快线顶到 **114**（阈值 120，只差 6）。

  正确处理：**这条闹钟今天跑不动了，直接取消并记原因**。
  不 latch 整天（心跳/事件上下文更小，可能还跑得动），也不重试。

### 自我纠正

这是今天第 2 个"修一个问题引入另一个"的例子（第 1 个是 v0.2.95 的
`_quota_exhausted` 差点把"额度用完"清掉）。共同点：**改的是分类，
但没同步改分类之后的动作**。分类对了、动作还是旧的，等于没改。

## [0.2.96] - 2026-11-20

### 修复（第四轮独立复核 + Codex 评审：第 6 次假绿 + 1 严重 + 3 中等）

- **单次请求比剩余额度大一点点，会把整天锁死**（Codex 实测，严重）。

  合并事件组请求需要 **41367** 输入 token，剩余 **41366** —— 差 1 个 token。
  原实现把这种情况也记成 `BUDGET_EXHAUSTED_WHY`，`agent/main.py` 于是 latch 出
  `_quota_exhausted=True`，**当天剩余所有回访都被跳过**。

  可实际上紧接着只需 8913 token 的心跳、单票回访完全跑得动。
  Codex 用「同进程新建 `AgentMain` 实例」做对照：latch 一清，那条闹钟立刻完成。

  修法：拆成两条 why——
  · `BUDGET_EXHAUSTED_WHY`：连最小请求（`MIN_TURN_EST_IN`）都放不下 → 真没了
  · `BUDGET_TOO_LARGE_WHY`：这次太大，但还有额度 → 只影响这一回合

- **第 6 次假绿：`test_budget_exhausted_flag_follows_the_db_flag` 没跑到它声称的 `_run()`**。

  第二个 `_once()` 一个回合都没跑（心跳未到期、无闹钟、无事件、open 已 done），
  断言只验到 `_once()` 开头那行重算。把 `_run` 恢复成旧写法仍然**全绿**。

  顺带发现那段"镜像"本身是**冗余的**：`_once()` 在闹钟与开盘回合之前
  已经无条件重算一次，`_run` 里再写一遍永远被覆盖。**与其留一段无覆盖的代码
  冒充修复，不如删掉**——现在 `_once()` 那行是唯一判定点。测试补了
  「两次 `_once()` 之间必须真有回合」的断言。

- **`test_budget_exhausted_does_not_rerun_alarms` 的变异红是 `AttributeError`**。
  手写的 `_Deps` 没有 `render`/`llm`，门一坏 `_run` 立刻炸在 `deps.render` 上，
  断言根本没执行。改成真实 `Deps`，现在红在断言本身。

- **`test_long_turn_does_not_cause_back_to_back_heartbeats` 的 `max()` 变异存活**。
  测试固定在 10:30（单档内，回合 180s < 间隔 300s），既跨不到分档也造不出"变短"。
  而 `next_heartbeat` 的分档**不是单调的**：13:29 属 `_LOOSE`(600s) 档 → 13:39；
  回合一跑跨进 13:30 后的 `_NORMAL`(300s) 档 → 13:37，**比原计划还早**。
  新增 `test_heartbeat_reanchor_never_shortens_the_planned_gap` 钉在分档边界上。

- **churn 只有「滚动一小时 >1000」一条判据，低速 runaway 发现太晚**。
  每分钟 20 个（≈4800/日，正常日 131 行的 36 倍）要跑满 48 分钟才越线，
  而 `_alert` 还有 `REPEAT_S=1800` 去重。新增 **5 分钟快线**
  `AGENT_TURN_CHURN_PER_5MIN = 120`（暴跌日实测峰值约 20），与小时线并联。

- **`test_normal_turn_volume_does_not_alarm` 的上界覆盖也是假的**：
  169 正常行 + 500 未来行 = 669 < 1000，删掉 `ts <= now` 照样绿。
  未来行加到 1200，现在删上界立刻转红。

- **"轮数用尽"静默取消，用户不知道这次回访没发生**（评审建议，已采纳）。
  闹钟是用户看得见的承诺（「我 30 分钟后回来看」），`agent_turn` 里那行只有
  运维在看。现在补一条低优先级便签，带标的与原始关注点；每条闹钟只推一次，
  推送失败也不影响回合链路。同时新增 `agent_schedule.cancelled_reason`
  （迁移 008），事后能区分「按时没跑成」和「跑了没跑出来」。

- **心跳 detail 把"额度用完"和"记账写库坏了"混在一起**。watchdog 用
  `budget_exhausted` 静音 `agent_stalled`/`llm_down`，而记账读失败也会
  fail-closed 置 True——那会把「卡死」的告警一起静音。现在分开报。

## [0.2.95] - 2026-11-20

### 修复（第三轮独立复核：第 5 次假绿 + 2 中等 + 1 轻微）

- **S1 `budget_exhausted` 是只在跨日清零的 latch——第 5 次假绿**。

  上一版修好了 `llm/budget.py` 的探测自愈，但评审发现还差一层：`agent/main.py`
  读到库标志就置 `self.budget_exhausted = True`，却**从没有 `else: False`**，
  而它只在跨日复位。于是记一次写锁抖动把它置上，即使探测已自愈、库标志已被删掉，
  这里仍恒为 True，**当天剩下的时间所有闹钟都被跳过**（回访一条都不跑）。

  评审实测的完整链条：标志已清 → `deduct()` 返回 True → `_once()` 再跑一轮
  → `budget_exhausted` 仍 True → 闹钟仍 `pending`。

  上一版测试标题写「self heals」，实际只覆盖到 `Budget` 内部（库标志清了），
  没覆盖到这个 latch——所以是假绿。

  修法：把标志判定抽成 `_budget_unavailable()`，**每轮 `_once()` 都重读一次**
  （放在闹钟循环之前）。只在 `_run()` 里做镜像会**死锁**：预算不可用时闹钟被跳过、
  开盘回合当天已跑过、心跳还没到期 → 这一轮根本不会 `_run` → 标志永远没机会复位。

  实测变异：去掉每轮重读，测试立刻转红。

- **M2 `attention.poll()` 仍排在推进之前，它一炸整个调度停摆**。

  v0.2.93 修了 `heartbeat_housekeeping()`，但 `poll()` 位置更早、后果更重：
  它内部 `if not events: return []` 会提前返回，一旦写库失败抛异常，
  `_once()` 整体抛给 `run()` 吞掉 → 心跳、闹钟、开盘回合**全部停摆**。
  已包 `try/except`：事件处理失败只该影响事件，不该影响调度。

- **M3 饿死不变量**。评审怀疑失败的闹钟退避后仍是最早的，会把后面的饿死。
  实测**不成立**（退避把 `due_at` 推到 `now+60`，未退避的积压一定排在前面），
  但这是靠"碰巧"成立的安全性质。已加测试钉住；把退避调成 0 立刻复现饿死。
- **L2** 探测窗口本身没有测试。上一版测试手工把 `_last_probe_at` 清成 None
  跳过等待，所以把 `WRITE_FAILED_PROBE_S` 改成 `1e9`（等于永不探测）时**照样绿**。
  已补 `test_probe_window_is_actually_respected`，用字面量 31 秒钉住窗口。

### 阈值调整

- **`AGENT_TURN_CHURN_PER_HOUR` 200 → 1000**。这个值改过两次，两次都是样本选错：

  | 版本 | 依据 | 问题 |
  |---|---|---|
  | 50 | 正常日峰值恰好 50 | 等于拿踩线当阈值 |
  | 200 | 正常日峰值的 4 倍 | **只量了正常日** |
  | **1000** | 正常日 50 / 暴跌日 169 / 事故日 17998 | 落中间 |

  11-20 是暴跌日（下跌家数 5400+），模型每 2~6 分钟排一条回访闹钟，45 分钟建了
  96 条，实测滚动一小时 **169 个回合**——离 200 只剩 16% 余量，再忙一点就误报。
  **暴跌日恰恰是模型最活跃、最该被监控的一天，不能在那天误报。**

  1000 对暴跌日是 5.9 倍余量，对事故量是 1/18，中间隔着两个数量级。

## [0.2.94] - 2026-11-20

### 修复

- **shadow 观测期的事件会永久沉底——142 条僵尸行的根因**。

  `poll()` 的 shadow 分支只写 `decision='shadow'` 的回合行，**不改 `agent_status`**，
  事件就一直停在 `new`。而 `poll()` 的查询是 `trade_date = 今天`——
  **次日它们再也取不到**，于是既不 `done` 也不 `suppressed` 地沉底。
  `jobs/cleanup.py` 要 30 天后才删，期间按 `agent_status` 做的统计全是僵尸。

  生产实测完全对得上：

  | 日期 | shadow 回合 | 残留 `new` |
  |---|---|---|
  | 11-10 | 29 | 1 |
  | 11-11 | 128 | 63 |
  | 11-12 | 89 | 24 |
  | 11-13 | 83 | 17 |
  | 11-16 | 78 | 12 |
  | 11-17 | 93 | 25 |
  | 11-18（中途关 shadow） | 32 | **0** |
  | 11-19 起 | 0 | **0** |

  shadow 期每天都积，关掉之后归零，但先前那 **142 条**已经沉底。

  修法：shadow 分支写回合行之后**必须标记状态**，用独立状态 `shadowed`
  （不是 `done`——它们在 shadow 期被观测过但没处理，记成 done 会让"处理率"说谎）。

  另外补一道保险：`heartbeat_housekeeping()` 把**跨日的 `new`** 显式标成 `expired`。
  即使将来又有别的路径漏标，也不会无限期停在"待处理"上。

## [0.2.93] - 2026-11-20

### 修复（第二轮独立复核：1 严重 + 5 中等 + 2 轻微）

- **S-a `budget.write_failed` 标志仍然修不死——这是第 4 次假绿**。

  上一版把自动清除挂在 `_try_deduct()` 里，而生产唯一入口 `deduct()` 在调用它
  **之前**就短路了：

  ```python
  if self._write_failed(): return False   # 永远到不了 _try_deduct
  try: return self._try_deduct(...)
  ```

  清除逻辑因此是**生产上永远执行不到的死代码**。一次 SQLite 写锁抖动置上
  `budget.write_failed:agent` 之后标志永久生效（`job_state` 的键没有日期，跨日也不清），
  后果是当天剩下的时间**所有闹钟都被跳过**、`agent/loop.py` 连推送都直接 return、
  watchdog 两条规则因同一标志静音，而 `preflight.py` 还会因此**拦住下一次部署**。

  上一版的测试之所以绿，是因为它**手工绕过短路**去调 `_try_deduct`，注释还自我解释说
  "那正是「写已恢复」的场景"。那不成立：绕过短路就绕过了生产里唯一的恢复路径。

  现在改成定期放行探测（`WRITE_FAILED_PROBE_S = 30`）：连续失败时仍 fail-closed，
  但每 30 秒真试一次，写恢复正常就自愈。测试改走 `deduct()`。

- **M-a 家务失败会让整个调度停摆**。`heartbeat_housekeeping()` 要写两张表并 commit，
  正是"磁盘满/写锁超时"会炸的地方。上一版把下一跳推进放在它之后，它一炸 `_once()`
  整体抛给 `run()` 吞掉——心跳、闹钟、开盘回合**全部停摆**，不写垃圾行但比写了更糟。
  已挪到最后，并给它自己包 `try/except`（家务是尽力而为的维护，不该能让调度停摆）。

- **M-b 长回合导致心跳背靠背**。`nxt` 是用 `_once()` **开头**的 `now` 算的，回合跑几分钟后
  它已经过期，下一轮立刻又是一次心跳。评审实测同一段墙钟心跳回合数 **+37%~62%**，
  全是白花的 token。已改为回合结束后按墙钟再补正一次（`next_heartbeat` 是纯函数不抛异常，
  不会重新引入 S2）。

- **M-c "轮数用尽"不该退回重试**。它与"回合超时"是两回事：超时是外部慢、重试有意义；
  轮数用尽是**模型自己在 6 轮内没有收敛**，隔 60 秒再跑必然同样用尽。实测退回的代价是
  3 次重试 ≈ **18 轮 LLM 调用**烧在同一条没收敛的闹钟上，最后转 `cancelled` 且无通知。
  现在直接 `cancelled`，明确记为放弃。

- **M-d 同日积压闹钟会在单轮里全部跑掉**。按日期作废保住了"同日迟到"（对），但失去了
  原来"同日过期 1 小时"顺手提供的泄压：宕机到 13:00 恢复时十几条同时到期，
  `LIMIT 10` 一口取出 10 条，串行最坏 **100 分钟**连续回合。已加单轮上限 2 条。

- **M-e 告警标题测试对 7/16 个生产 kind 是盲的**。正则只认 `.alert("...")`，
  而 `agent_down`/`agent_stalled`/`llm_down`/`agent_churn`/`jobs_down` 走
  `dispatcher.alert(kind, ...)`（kind 是变量）、`upstream_dead`/`snapshot_crash`
  走 `on_alert("...")`。实测删掉这两族的标题测试照样绿。正则已扩成三种写法。

- **L-b L3 的口径统一没有测试**。改回 `now - 3d` 全量仍绿（原测试用 5 天前数据，
  两种口径都过）。已补一条**卡在两种口径之间**的样本（3 天前 05:00）钉住。

- **L-d** 两处手工赋值 `run_with_deadline` 改成 `monkeypatch`，避免断言失败时补丁泄漏到后续测试。

### 自我纠正

第二轮评审抓到的第 4 次假绿，形状与前 3 次完全一致：**测试没跑生产路径**。
已经把"测试必须走生产入口"当成本轮的验收标准——S-a 的测试改走 `deduct()` 后，
把实现改回短路版本立刻转红。

## [0.2.92] - 2026-11-20

### 测试

- **钉住「次日回访不被 `sweep_stale_alarms()` 误杀」**。

  盘后 15:10 排的「明天开盘再看」（`due_at` 是次日 09:20）是最常见的一种合法闹钟。
  按日期判据 `substr(due_at,1,10) < 今天` 时它自然保留——今天还是排闹钟那天。
  但如果有人把判据改成「只保留今天到期的」，次日回访就会被静默作废，
  而这一条模型会一直在排。

  变异验证：把判据改成 `substr(due_at,1,10) != 今天`，测试立刻转红。
  另核查生产库 `due_at` 全部是 ISO 文本（`typeof='text'`、格式异常 0 条），
  `substr` 取日期稳定。

## [0.2.91] - 2026-11-20

### 修复

- **L2 超时回合把闹钟永久留在 `fired`**。原来只有 `error`/`invalid` 会退回，
  超时回合不走那条分支——模型说的"我 30 分钟后回来看"就静默作废。
  `release_alarm` 的 docstring 本来就写明了"LLM 挂掉/预算耗尽/**超时**"，
  实现只覆盖了前两种。现已补上（有 60 秒退避 + 3 次上限，不会成环）。

- **L3 清理任务与 `sweep_stale_alarms()` 对"过期"的定义不一致**。
  cleanup 用 `due_at < now - 3d`，sweep 用"日期更早"——一条 4 天前到期、
  但昨天才被作废的闹钟会多活一天，排查时两边对不上。统一成按日期。

## [0.2.90] - 2026-11-20

### 修复（独立评审的 3 个严重 + 5 个中等）

- **S2 心跳回合在记账后炸一次，下一跳就永远不推进 → 每 2 秒重跑（最严重）**。

  `_run` 的顺序是「跑回合 → 记日志 → 推送/确认卡/排闹钟」，任何一步抛异常都会被
  `run()` 吞掉。原来 `self.next_heartbeat_at = nxt` 写在 `await self._run(...)`
  **之后**，于是日志写成功之后炸一次，`now >= next_heartbeat_at` 就恒成立。
  实测 6 轮 → `agent_turn` 逐轮 +1 且 `next_hb` 一直不动，与 11-19 那 17998 行
  **完全同型**。已改为在跑回合**之前**推进。

  同一形态还存在于**开盘回合**：`_open_turn_done()` 靠"当天有没有 open 行"判断，
  而记账失败时那一行永远写不出来 → 每 2 秒重跑。已加当日尝试上限（3 次）。

- **M1 预算耗尽取消闹钟的路径没有断言级测试**。把 `exhausted` 改回 `False`
  （恢复事故形态）跑全量仍全绿——原测试只覆盖"跳过闹钟循环"，走不到
  `_run` 里的取消分支。已补走真实 `_run` 的测试。

- **M3 `sweep_stale_alarms()` 误杀最该跑的闹钟**。上一版判据是"过期超过 1 小时"，
  会杀掉「昨天排的、今天 09:20 的回访」（进程 10:30 才拉起）和「09:30 的正常回访」
  （盘中重启后 10:40 恢复）——这两类日期都是今天、语境完全有效。改成按**日期**判断。
  顺带修掉 `max(当天零点, now-1h)` 里那个**死分支**（盘中 `now-1h` 恒晚于零点，
  写着两条判据实际只有一条在跑）。

- **M5 `budget.write_failed:agent` 一次写锁抖动就永久生效**。
  `clear_write_failed()` 在生产里零调用者，标志置上后 `agent/main.py` 会读成
  `budget_exhausted=True` → **当天剩下的所有闹钟都被跳过**，watchdog 两条规则
  还会因同一标志静音。已改为成功扣减时自动清除。

- **M4 告警中文标题只覆盖 7 个 kind**。实测 `disk`/`backup`/`members`/`archive`/
  `self_check`/`westock_renew`/`agent_main_crash`/`confirm`/`pool` 在手机上
  显示的仍是裸标识符。补齐 17 个，并加测试**扫源码里所有 `alert("<kind>")` 调用点**
  逐个核对（只测一个 kind 的话，删到只剩它照样绿）。

- **L1** 异常路径的 `release_alarm` 未传注入时钟，已补 `now=`。

### 自我纠正

评审的两条"假绿"都指向同一个根因：**我写的测试没跑到被测代码**。

- S2 测试用会抛异常的 LLM，但异常被 `run_with_deadline` 转成 `error` 结果，
  记账照常成功，`_run` 正常返回 → 把实现改回旧版照样绿。改成"记账后
  `schedule_alarm` 抛异常"才真正踩到那条路径。
- 开盘回合测试同理，改成让 `journal_record` 本身失败。

教训：**让测试失败在正确的位置**，而不是"只要能红就行"。

## [0.2.89] - 2026-11-20

### 修复

- **补齐"每条每 2 秒重跑的路径"的核实**，并把事件重试的防线测对。

  排查了 `_once()` 里所有可能被反复执行的路径：

  | 路径 | 终止条件 | 结论 |
  |---|---|---|
  | 闹钟 `due_alarms()` | 之前**没有**终止条件 | 事故来源，已修（退避+次数上限+跨日作废） |
  | 事件 `_pending_retry()` | `journal.record` 两振出局 → `gave_up` | 安全 |
  | 开盘回合 `_open_turn_done()` | 当日有 `open` 行即不再跑 | 安全 |
  | `close_sweep()` | `_close_done()` 落 `job_state` | 安全 |

  实测：预算耗尽下连跑 5 轮，开盘回合只在第 1 轮执行、之后 4 轮均 0 行；
  `close_sweep` 4 次均 0 行；事件路径 10 条事件最多 20 个回合后全部 `gave_up`。

  **同时修掉一条自己写错的测试**：第一版叫
  `test_event_retry_path_is_bounded_by_heartbeat`，断言"事件重试只在心跳分支里跑"。
  把 `_pending_retry()` 挪到心跳分支外之后测试照样绿——因为真正拦住循环的是
  `gave_up`，不是心跳节奏。**测错了防线就是假绿**。改成
  `test_event_retry_terminates_via_gave_up`，直接断言收敛到 `gave_up`
  且回合数有上界；去掉 `gave_up` 分支立刻转红。

## [0.2.88] - 2026-11-20

### 修复

- **抽方法时把 `conn.commit()` 一起搬走了，housekeeping 的两条 UPDATE 全部失效**。

  重构 `heartbeat_housekeeping()` 时新增了 `sweep_stale_alarms()`，把原函数末尾的
  `conn.commit()` 挪进了新方法，**旧函数就此失去提交**。后果：

  - `escalated` 事件永远不会回收成 `pending_retry`（升级后卡死在 escalated）；
  - 确认卡永远不会标记 `expired`（24 小时后仍显示可点，点下去是过期票据）。

  而且**没有任何报错**——两条 UPDATE 在事务里悬着，连接关闭时被隐式回滚。

  生产暂未受影响（当时 `escalated` 与 `pending` 卡各 0 条），但这是个会静默积累的坑。

  顺带说明为什么普通断言抓不到：同一连接里能查到未提交的数据。测试必须**另开一个
  连接**去查才算数。已加 `test_housekeeping_actually_persists`，变异验证有效
  （去掉 commit → 立刻转红）。

## [0.2.87] - 2026-11-20

### 修复

- **`agent_churn` 被 `budget_exhausted` 静音——把最该报的空转挡掉了**。

  另外两条规则（`agent_stalled`、`llm_down`）都带 `not _budget_exhausted(conn)`，
  照抄过来看着很一致，其实是**反的**：

  - 预算耗尽能解释「没有回合」「没有成功回合」→ 那是预期停止，不该报；
  - 预算耗尽**解释不了「一小时内 18000 个回合」**。

  2026-11-19 那次事故的根因**就是预算耗尽**：闹钟回合在预检里立刻返回 error
  （`rounds=0`、0 毫秒）→ 记账 → 闹钟退回 → 2 秒后再来。加这个 gate 等于
  「最该报的那种空转恰好被永久静音」，而它偏偏就是当天真实发生的那种。

  已去掉 gate，并加双向测试：预算耗尽时空转照样报 P0（变异回 gate 立刻转红）。

## [0.2.86] - 2026-11-20

### 修复

- **上一版的 `agent_churn` 阈值会误报正常日，而且统计窗口少了上界**。

  两个错叠在一起，都用生产库干跑抓出来的：

  **① SQL 少了 `ts <= now` 上界。** 没有它就不是"滚动一小时"，而是"这一小时之后的
  所有行"。实时运行时 `now` 就是当下，看不出区别；但 `now` 一旦是注入的
  （回放/干跑/测试用 `FixedClock`），统计量会把"未来"算进来：

  ```
  干跑 11-18 14:30：无上界 = 18066，有上界 = 45
  ```

  线上永远不触发，却会让所有离线排查得出相反结论。

  **② 阈值 50 太低。** 实测六个正常交易日：全天 78–131 个，逐小时峰值 27–48，
  **滚动一小时最大正好 50**（11-18 午后）。写 50 就是拿正常日最忙的那小时踩线。
  改成 200（正常峰值的 4 倍、事故量的 1/90，中间两个数量级空隙）。

  修正后用生产库干跑六个时点，全部正确：

  | 时点 | churn | 小时回合数 |
  |---|---|---|
  | 事故日 11-19 14:30 | 报 | 7504 |
  | 事故日 11-19 15:04 | 报 | 17349 |
  | 正常日 11-18 14:30（最忙） | 不报 | ≤50 |
  | 正常日 11-18 10:30 | 不报 | ≤50 |
  | 正常日 11-17 14:30 | 不报 | ≤50 |
  | 正常日 11-16 09:45 | 不报 | ≤50 |

  测试也补了双向覆盖：暴增要报、正常量不许报，且**在窗口之外塞 500 行**
  验证上界生效（去掉上界这条测试立刻转红）。

## [0.2.85] - 2026-11-20

### 修复

- **空转死循环没有任何告警能发现（新增 watchdog 规则 5）**。

  2026-11-19 的空转事故里，原有四条规则一条都没报：

  | 规则 | 为什么没报 |
  |---|---|
  | `agent_down` | 心跳正常，进程活得好好的 |
  | `agent_stalled` | 恰恰相反——回合多得离谱 |
  | `llm_down` | 11:24 报过一次后进入 30 分钟去重；"连续失败"升级成"疯狂失败"没有阈值 |
  | `jobs_down` | jobs 完全正常 |

  这类故障的特征是**量级异常**（实测一天 17998 行，正常日 78–131 行），
  所以判据也必须是量级：一小时内超过 `AGENT_TURN_CHURN_PER_HOUR`（50）个回合
  就报 `agent_churn`（P0）。50 是"绝对不正常"的低水位，正常日一小时约 20 个，
  双向都有测试钉住（暴增要报、正常量不能报）。

- **告警标题直接用了代码标识符**。`render_alert(kind, ...)` 把 `kind` 当标题，
  手机上收到的是 "agent_churn"、"llm_down"——那是给代码看的，不是给人看的。
  现在有 `_ALERT_TITLE` 映射（"agent 空转死循环"、"LLM 连续不可用"……），
  并加测试把 `AlertKind` 与映射绑在一起：新增规则忘配标题会被拦下。

## [0.2.84] - 2026-11-20

### 修复

- **跨日闹钟作废放错了地方，生产上根本不会执行**（自查发现的假绿）。

  上一版把它挂在 `heartbeat_housekeeping()` 里，而那个方法**只在心跳到期时跑**。
  进程 09:15 启动时 `next_heartbeat(now)` 给的下一跳是 **09:17**，于是最初 2 分钟
  里作废没发生、而闹钟循环每 2 秒都在跑——昨天的闹钟照样开跑。

  真实路径复现（不手动设 `next_heartbeat_at`，走真实初始化）：

  ```
  修复前：闹钟状态 [(1,'pending',1),(2,'pending',1)]  agent_turn 2 行（都是 alarm/error）
  修复后：闹钟状态 [(1,'cancelled',0),(2,'cancelled',0)]  agent_turn 0 行
  ```

  测试之所以是假绿，是因为它手动把 `next_heartbeat_at` 设成了"立即到期"，
  把自己的前提造得比生产更宽松。现在改成不设、走真实初始化。

  同时把它抽成独立方法 `sweep_stale_alarms(now)`，在闹钟循环**之前**按
  `ALARM_SWEEP_EVERY_S`（60 秒）周期执行。

- **同日过分过期的闹钟没有作废**。只按"跨交易日"作废会漏掉一个常见场景：
  当天宕机两小时，恢复时十条 09:30 的闹钟同时到期，连跑十回合烧光预算。
  语义上也不对——闹钟都是"过 N 分钟回来看看"，拖一小时以上看到的已是另一段行情。

  现在加第二条判据：同日过期超过 `ALARM_MAX_STALE_S`（1 小时）也作废。
  边界双向卡住：迟到 30 分钟照常跑（正常的网络/排队抖动），迟到 61 分钟作废。

  这里自己写错过一次：两条判据的阈值该取 `max`（越界即作废，取并集上界），
  先写成 `min` 让同日那条永远失效——零点总是早于"现在减一小时"。
  是测试把这个写错抓出来的。

## [0.2.83] - 2026-11-19

### 变更

- **预算耗尽时跳过闹钟改为「只跳过闹钟」，不再 `return` 整个 `_once()`**。
  原来的 early return 会顺手跳过函数尾部**开盘回合**的处理——今天没踩到
  （开盘回合在 09:26 之前就完成了），但只要以后往尾部加东西就会踩到。
  改成 `for alarm in ([] if self.budget_exhausted else due_alarms(...))`，
  作用域只覆盖闹钟循环，语义也更准。

## [0.2.82] - 2026-11-19

### 修复

- **跨交易日的 pending 闹钟会隔天补跑，且一次性全部到期**。

  闹钟的语义是"过 N 分钟回来看看这个价位"——价位语境当天有效。隔天补跑昨天
  没跑成的回访，看到的是新一天的行情、回答的是昨天的问题。

  更糟的是它们**一次性全部到期**：agent 停摆半天后，积累的十几条闹钟会在同一秒
  被 `due_alarms()` 全部取出，连跑十几个回合烧光预算，再推一串过时判断给用户。
  `jobs/cleanup.py` 只处理 **3 天前**的，救不了"昨天积压、今天开盘"这个最常见
  的情形。

  现场：2026-11-19 收盘时 16 条 pending 全部过期 2–3 小时，最早那条是 11-18 排的
  ——次日开盘会同时到期。现在 `heartbeat_housekeeping()` 按交易日收口，
  跨日的 pending 直接转 `cancelled`（留痕，可事后回答"为什么没跑"）。

## [0.2.81] - 2026-11-19

### 修复

- **退避表的第 3 档永远取不到（死配置）**。`max_attempts: 3` = 首次 + 2 次重试，
  只有 2 次退避会真的发生，而配置写了 `[1.0, 3.0, 8.0]`——那个 8 秒档是死的。
  收成 `[1.0, 3.0]`，并加测试钉住「退避档位数 == 重试次数」。
  这类"看着配了其实没生效"的坑本周已经是第三次（`retry_on`、
  `agent.yaml` 回合预算、本条）。

- **补充「实际请求带的超时」测试**。原来只断言 `default_timeout()` 的返回值，
  但函数返回对、传参路径断掉的话测试照样绿，线上行为完全不同。
  现在检查 `request.extensions["timeout"]["read"]`，并断言它既不是 0
  （立刻超时）也不是 `None`（永久挂住）。

### 修复

- **闹钟失败重试变成死循环，一天写了 17885 行空转记录（生产事故）**。

  `release_alarm()` 把失败的闹钟退回 `pending`，`due_alarms()` 返回所有
  `due_at <= now` 的行，而 `_once()` 每 2 秒轮询一次。于是一条**已过期**的闹钟
  陷入「到期 → 认领 → 失败 → 退回 → 2 秒后又到期」的循环。

  只要失败原因当天不可能恢复，这个循环就会一直转到收盘。最典型的就是
  **「agent 日预算已用完」**——额度耗尽了，但闹钟还在每 2 秒重试一次必败的回合。

  现场数据：14:00 起 error 行数每分钟 300 行，收盘时累计 **17885 行**
  `agent_turn`，全是 `rounds=0, latency_ms=0`（根本没调模型）。日志页被彻底冲垮，
  真正的告警淹没在里面。

  三处一起修：
  1. `release_alarm()` 记次并退避（`due_at` +60 秒，必须大于 2 秒轮询间隔），
     超过 3 次转 `cancelled` 而不是无限退回；
  2. **预算耗尽时直接跳过闹钟循环**——那天不可能再跑了，跳过而不是 cancel；
  3. 预算耗尽导致的回合失败**直接取消闹钟**，不进重试。

  新增迁移 `007_alarm_attempts.sql`（`agent_schedule.attempts`）。

## [0.2.80] - 2026-11-19

### 变更

- **LLM 超时与重试全面改成可配置，默认不设业务上限**（代码里保留兜底安全网）。

  起因是一次生产故障：上游网关排队时段**每轮要 23–31 秒**（连只回 1 个 token
  的请求也要 23 秒——不是生成慢，是排队），而配置是「单次 20 秒 + 回合 25 秒」。
  一个回合至少要两次 LLM 调用，25 < 20×2，于是每一次 attempt 都超时：
  实盘一天 21/21 个真实回合 timeout，44 次 attempt 没有一次拿到回包。

  现在：
  | 参数 | 位置 | 默认 |
  |---|---|---|
  | 单次调用超时 | `llm.yaml: timeouts.agent_call_s` / `chat_s` | `0`（不设业务上限） |
  | 回合时长 | `agent.yaml: event.deadline_s` / `heartbeat_turn.deadline_s` | `0`（不设业务上限） |
  | 回合轮数 | `agent.yaml: event.rounds` / `heartbeat_turn.rounds` | 3 / 6 |
  | 重试次数与退避 | `llm.yaml: retry.max_attempts` / `backoff` | 3 次 / `[1,3,8]` 秒 |
  | 重试/放弃的状态码 | `llm.yaml: retry.retry_on` / `never_retry` | 429,5xx / 400,401,403,404 |

  **"不设业务上限"不等于完全不设限**：代码里有兜底安全网
  （单次 300 秒、回合 600 秒），只有"上游彻底不回包"才会撞上。
  没有它，一次永久挂住会让 agent 整体停摆、心跳跟着断。
  兜底回合时长刻意小于 watchdog 的判死阈值（15 分钟），
  让回合自己收尾记一行 `timeout`，而不是被外部判成"进程卡死"。

### 修复

- **`agent.yaml` 的 `event` / `heartbeat_turn` 是死配置**。真正的回合预算写死在
  `agent/main.py` 的常量里，运维改 YAML 完全不会有任何效果——而故障排查时
  第一反应恰恰是查 YAML，拿到的是假信息。现在启动时从配置读，改完重启生效。

- **`retry.retry_on` 没有任何读取点**。`classify()` 用的是写死的状态码集合，
  `never_retry` 也没接线——两个键写在 YAML 里，改了什么都不会发生。
  现在两个键都真正生效；同时把 `timeout`/`empty_body`/`http_000` 三个符号项
  从 `retry_on` 里删掉——它们永远重试、不可关闭，写在配置里只是噪音。

- **429（限流）被判成不可重试**。限流是"稍后再来"，不是"你错了"。

- **退避表从 2 档加到 3 档**（`[1.0, 3.0, 8.0]` 秒 ±30% 抖动）。原来的
  `[0.5, 1.0]` 对网关排队太急，容易在同一个拥塞窗口里连续撞三次。

- **正常响应不能被状态码判成致命错误**。这是本次改造中自己引入又修掉的回归：
  新的状态码判定把非 5xx 一律当 fatal，而正常响应的 `status` 也是 200 ——
  所有成功响应都被定性为致命错误，"截断翻倍重试""空响应重试"两条路径静默消失，
  而 focused 测试全绿。已补 2xx/3xx 豁免与对应正控测试。

## [0.2.79] - 2026-11-19

### 修复

- **事件回合 deadline 90 秒仍是零余量**。上一版按「2 轮 × 40 秒」给了 90 秒，
  但实测**每轮要 30 秒**（随网关负载在 23–31 秒浮动），事件回合典型跑 3 轮
  ——90 秒恰好是零余量，上线后 11:06 与 11:10 两个回合都精确卡在 90.00 秒超时。

  提到 120 秒（可容纳 4 轮）。心跳/闹钟同为 120 秒：密集期心跳间隔就是 120 秒，
  再长会让心跳互相堆积。不变式测试仍钉住「deadline ≥ 单次超时 × 2」。

## [0.2.78] - 2026-11-19

### 修复

- **agent 整个交易日 100% 回合超时（生产故障）**。中转站在开盘繁忙时段单次
  调用要 23 秒以上——连只回 1 个 token 的请求也是 23 秒（不是生成慢，是网关排队；
  网络本身 DNS 9ms / TCP 229ms / TLS 155ms 都正常，换模型也不改善，
  三个模型实测 25–30 秒）。

  而配置是 `agent_call_s: 20`、事件回合 `deadline_s: 25`：
  **25 < 20×2**，一个回合至少要两次 LLM 调用（先决定查什么、拿到工具结果再出
  决策），deadline 只够一次——每轮都死在"还没等回来就到点"上。

  2026-11-19 当天 21/21 个真实回合全部 timeout，44 次 attempt 没有一次拿到回包
  （`token_audit.real_in` 全是 NULL），最后一个成功回合停在 11-18 15:05。

  实测**每轮 30 秒**（随网关负载在 23–31 秒浮动），按「deadline ≥ 单次调用
  超时 × 轮数」重配：`agent_call_s: 40`、事件回合 120 秒（4 轮）、
  心跳/闹钟 120 秒（密集期心跳间隔就是 120 秒，再长会让心跳互相堆积）。
  新增不变式测试钉住「deadline ≥ 单次超时 × 2」这条关系。

- **`config/agent.yaml` 的 `event`/`heartbeat_turn` 是死配置**。
  两段的 `rounds`/`deadline_s` 全仓无读取点，真正的回合预算写死在
  `agent/main.py` 的常量里——改 YAML 不会有任何效果。保留键名（必填键契约），
  但标注"未生效"并指明真实位置。**这次的坑就是它挖的**：
  故障时我第一反应是查 YAML，而 YAML 里那两行数字根本不生效。

## [0.2.77] - 2026-11-19

### 变更

- **公开仓库里不再写「本机花了多少钱」**。这一版把 CHANGELOG、`config/llm.yaml`
  注释和预算测试里绑定本机账号的读数（每日花费、精确 token 数、单次预扣均值）
  换成了**可复现的核算方法**：一天几个回合、每回合几次 attempt、保守预扣是
  真实用量的几倍，以及怎么自己从 `token_audit` 表算出来。

  工程结论一条没丢（"按心跳真实节奏估回合数""别用每回合一个请求去估额度"
  这些教训都留着），但别人 clone 之后不会读到一份属于别人账单的快照，
  预算测试也不会因为心跳节奏不同而误报。

  另外顺手删掉了代码注释与 RUNBOOK 里几处"某年某月某日上午 11 条..."式的
  生产事故复述——保留了现象与结论，去掉具体日期。

## [0.2.76] - 2026-11-19

### 新增

- **推送历史页**（`GET /api/push-log` + 工作日志页下方新增一区）。
  原来 `push_log` 的 `body`/`error`/`status` **没有任何读接口**，前端也没有对应页面：
  "今天那条为什么没收到"只能 SSH 进库查，失败/被抑制/审计行在网页上全不可见，
  只有一枚"推送通道异常"的通道级徽标（而它要连续失败 3 次才亮）。
  现在每条推送的最终状态、失败原因、级别、是否第二段都能直接看。

### 修复

- **同一 kind 的运维告警没有冷却**。`agent/main.py` 的主循环约 2 秒一轮，
  `except Exception` 里调 `alert`；异常持续时每个周期都推一条 P1，
  几分钟就把 `notify.daily_cap` 烧穿——额度一满，**真正的告警也被压掉**。
  watchdog 自己早就有 30 分钟冷却，这条路径没有。现在同 kind 30 分钟内只发一次
  （被额度压掉的也算"已尝试"，否则额度满时反而每 2 秒重试）。

- **收盘摘要把"没发出去的审计行"算成已推送**。`confirm_result` 是确认回调里
  直接以 accepted 插入、**从未发送**的行；`_capped()` 用 `NON_PUSH_KINDS`
  排除了它们，收盘摘要却只按 `status='accepted'` 数——同一份语义两处口径不一致。
  抽成 `build_close_stats()`，两处共用一份口径。

- **确认卡没发出去却留着 `pending`**。`api/routes.py` 的确认入口只校验
  `confirm-{turn_id}` 这行存在、不看 status——留着一个用户根本没看到的 pending，
  24 小时内仍可被执行，还一直占着"待确认"让模型空等。
  现在发送失败就置 `expired`（与 `chat/session.py` 的同类路径一致）。

- **沉默回合的确认卡没有理由**。`speak=false` 时不推正文，确认卡是用户唯一看到的
  东西，而卡上只有一行"加入自选 sh600519"——用户不知道该不该点，只能无脑忽略。
  现在把 `why_silent`/正文作为「理由」附在卡上。

- **闹钟的 `created_at` 绕过注入时钟**。`due_at` 取 `clock.now()`，
  `created_at` 却是 `datetime.now(SH)`。生产用 `SystemClock` 无碍，但 `FixedClock`
  下两个字段会差出真实日期，`get_own_history` 按 `created_at` 筛当日就查不到
  模型自己刚排的闹钟。现在 `now` 可从外部传入。

## [0.2.75] - 2026-11-18

### 修复

下面四条来自对 Anthropic《Building always-on agents》常驻 Agent 五部件模型的
独立评审（Codex + 子代理各评一遍，结论一致）。

- **P0 运维告警被日额度挡住**（v0.2.69 引入的回归）。把额度判定从 `_route`
  移进 `_push_rows` 事务时，`enforce_cap` 默认值取成了 `True`，于是 `alert()`
  这条**从未被 `_route` 管过**的路径开始受 `notify.daily_cap` 限制：
  日额度一满，通道故障/磁盘告警会被静默 `suppressed`，用户永远收不到。
  v0.2.68 的 `_push_rows` 里压根没有额度检查。

  现在只有行情信号（含合并批次）受额度约束，告警/开盘简报/收盘摘要不受。

- **agent 侧推送失败是黑洞**。`api/routes.py` 只认 `kind='push_channel_down'`
  这一种行，而它只有 `Dispatcher._channel_down()` 会写。agent 走的是
  `AgentPusher` 这条独立路径（通道 B 便签、事件第二段、确认卡），失败时只把行标
  `failed`——于是 agent 整天发不出去，首页仍显示"推送正常"，用户只能靠
  手机没响自己猜。现在 `AgentPusher` 也有失败计数与留痕。

- **确认执行之后模型不知道**。`hook_agent_confirm` 把 `pending_status` 置
  `confirmed`、写一条 `confirm_result` 审计行就结束，agent 侧**零个读取方**，
  也不产生可唤醒事件。后果：手机端只有一个 toast；模型下一轮仍以为"还没办"，
  很可能**再提一次同样的 `pending_action`**（又多一张确认卡）；也无法收尾
  （"加进自选后我盯着它"）。

  现在已确认的操作会渲染进上下文（`【已确认执行】`段），`get_own_history`
  也带上了 `pending_status`/`pending_action`。不新增 LLM 调用、不改回合模型。

- **闹钟回合失败即永久丢失**。`after_alarm` 在跑回合**之前**就把状态认领成
  `fired`（防止重复回合），而 `_run` 失败时没有任何回滚：LLM 挂掉、预算耗尽、
  甚至协程被取消，模型说的"我 30 分钟后回来看"就静默作废，连一行日志都没有。
  清理任务只处理 3 天前的 pending，不会补跑。

  现在 `error`/`invalid` 判定与**抛异常**两条路径都会把闹钟退回 `pending`。

- **`config/agent.yaml` 的 `memory` 三键是死配置**。`judgment_days`/`per_target`/
  `ignored_focus` 全仓无读取点，`ignored_focus: 0.7` 尤其误导——看着像
  "按记忆抑制焦点"，实际没有。保留键名（改动会破坏必填键契约），
  但标注"未生效"并说明记忆的真实覆盖范围。

## [0.2.74] - 2026-11-18

### 修复

- **轮次中途预算耗尽被记成 `timeout`**。`run_turn` 里每轮的额度检查用的是
  `TurnDecision.timeout("agent 日预算已用完")`，而入口层的预检用的是 `error`——
  同一件事两种记法，把「回合超时率」这个告警口径搞脏了。
  实测某天上午 11 条 `timeout` 里有 **10 条其实是预算耗尽**
  （耗时 0–5 秒，根本没等），真实超时率被虚报成 51%（实际约 5%）。

  统一为 `error`（预算耗尽不是"慢"，是配置/额度问题）。

- **agent 日预算第三次不够用**：`3000000` 在当天 **14:20 就打满**。
  当日 14:10 的实测：一个交易日累积 attempt 数是往两版的 2 倍以上，
  **保守预扣约为真实用量的 3 倍**（口径见 `config/llm.yaml` 注释），
  原档位在收盘前就见了底。

  调整为 `[5500000, 700000]`。预算是"够用"的兜底，不是花钱目标。

  预算测试也一并改写：原来用"每回合 N 个 token"这种拍脑袋的常数，
  现在跟着 `next_heartbeat` 实时算心跳次数、再乘实测的每回合 attempt 数
  ——档位低于实际需求就会红，但不绑定任何一次具体的生产读数。

## [0.2.73] - 2026-11-18

### 修复

- **股息游标遇到"首只永远拿不到"就钉死**。`advance_actions_cursor` 原来
  `if idx == begin: nxt = codes[begin]`——每轮返回 39/40、缺的永远是起点那只时，
  游标原地不动，后面 5500 多只票一次都不会被请求。生产实测游标走到 1213/5583
  就几乎停滞，之前"全市场约 7 小时跑完一圈"的结论**没有连续推进的证据**。

  现在连续 `STUCK_ROUNDS`（3）轮拿不到就顺延一位；被跳过的票仍留在滚动范围内，
  下一圈会再被请求，不会被静默记成完成。

- **只加自选、没进候选池的票，竞价高低开被静音**（v0.2.70 回归）。
  `jobs/main.py` 把 `tick.focus_of()` 传给竞价，而它只查 `_pool_rows()`，
  自选票被判 `outside`——高开 +5% 也不产生任何事件。
  新增 `auction_scope_of()`（池内**或**自选都算重点）供竞价使用；
  `focus_of()` 保持原语义（`detector._focus()` 判 salience 权重时自选走 `det.watch`，
  两者不是一回事）。

- **agent 日预算再次不够用**。按实际调度复算：一个交易日的心跳是
  **68 次**（`agent/schedule.py`：09:15–10:00 与 14:30–15:00 每 2 分钟、
  其余 5 分钟、两段午间 10 分钟），远超注释里写的 34 次；叠加事件与闹钟
  约 103 个回合，前两版档位都撑不到收盘。调整为 `[3000000, 390000]`。

- **升级会把"正式模式"和"旧预算"反向地同时打开**。`deploy.py` 的保留名单里有
  `config/llm.yaml` 却没有 `config/agent.yaml`：常规升级后 `shadow_mode`
  被覆盖成仓库值、预算却仍是站点旧值——v0.2.71→v0.2.72 上线当天 10:42
  预算打满就是这么发生的；回滚同理，版本退回去配置不会退。

  现在 `agent.yaml` 一并保留，并新增部署后的配置漂移提示（打印站点配置里
  `shadow_mode` / `agent:` 所在行），避免"仓库改了默认值、站点还在跑旧的"却无人知晓。

## [0.2.72] - 2026-11-18

### 修复

- **agent 日预算太小，正式模式下开盘半小时就耗尽**。切换正式模式后
  10:42 就出现「agent 日预算已用完」，之后所有唤醒直接 timeout。

  查下来 `llm_budget` 记的是**保守预扣**（每个 attempt 发请求前扣
  「字符数×1.2 + max_tokens」，不退款），实际约为真实用量的 3 倍左右。
  所以原档位只够跑十几二十个回合，而一个交易日光心跳就有几十次。

  调整 `config/llm.yaml` 的 `budget.agent` 为 `[1500000, 200000]`：
  按每回合的预扣量反推可覆盖的回合数。这一步的估算仍然偏保守，
  后续两版继续修正（见 0.2.73 / 0.2.74）。

## [0.2.71] - 2026-11-18

### 变更

- **agent 切到正式模式**（`config/agent.yaml` 的 `attention.shadow_mode: false`）。
  shadow 观测期为 11-10 ~ 11-18 共 7 个交易日，超过 RUNBOOK 要求的 3 天；
  `preflight.py --for-shadow` 自检通过。通道 B（agent 主动开口）从"只记录"
  改为真实调用 LLM 并推送，通道 A（确定性信号）与通道 C（运维告警）行为不变。

  切之前先修掉了让标定失效的噪声源（见上一版：竞价异动对全市场报高低开，
  导致 97.6% 抑制率），否则正式模式会几乎不推。

## [0.2.70] - 2026-11-18

### 修复

- **股息数据覆盖太慢**：滚动刷新原为每 20 分钟一轮，全市场 5583 只、每轮 40 只，
  跑完一圈要 4.5 天。实测单轮只花 1.2 秒（4 批 × 10 只），
  而**加大每轮批数适得其反**——同轮连发 6/8/10 批都会触发限频、退避 90 秒后
  只收到 13/46/30 只，吞吐反而慢 40 倍。真正的瓶颈是"多久跑一次"，所以把间隔
  压到 2 分钟：一天约 310 轮，全市场约 7 小时跑完一圈。

- **竞价异动把事件流淹了**：`eval_auction` 对全市场所有票报"高低开 ±2%"，
  而 A 股每天天然有几百只涨跌超过 2%。实测 11-17 一天 860 条 `auction_anomaly`，
  其中**没有一条**是量能异常、**没有一条**属于自选——全是纯噪声。
  它把 `salience` 分布整个压死（抑制率 97.6%），阈值怎么调都调不到
  RUNBOOK 要求的 20–40% 区间，agent 切正式模式后会几乎不推。

  现在**池外票只报竞价量异常，高低开只对池内/自选报**。全市场扫描能力保留
  （`auction_daily` 照常写，竞价均量基线继续攒），噪声砍掉。

## [0.2.69] - 2026-11-18

### 修复

- **推送日额度检查与预留不原子**（D1）。`_capped()` 在 `BEGIN IMMEDIATE`
  **之前**执行，jobs 的通道 A 与 agent 的通道 B 会各自读到 59/60 然后都放行，
  实际发到 61——SQLite 的写串行化救不了"事务外已经做出的额度决定"。
  现在额度判断移进认领事务（写锁到手后再读）；超限仍写 `suppressed` 审计行，
  P0 豁免通过 `enforce_cap=False` 透传。agent 侧 `agent_note` 同样处理，
  发送动作移到事务提交之后（网络调用不该占写锁）。
  注：`attention.shadow_mode: true` 期间通道 B 不推送，所以此前不会触发；
  这是启用 agent 正式模式前必须解决的前置问题。

- **测试没有隔离外部依赖**（T1）。api fixture 不注入 `llm` 时 `ChatSession`
  会自建 `LlmClient` 去连真实地址；自检与续期用例也存在真实出站/默认路径写入。
  - `create_app` 增加 `chat_llm` 注入口，api fixture 改用 `OfflineChatLlm`
  - 续期用例显式传临时 `westock_env_path`（原来会写默认的 `/state/westock.env`）
  - `test_renew_job_uses_injected_runner` 增加"真的续期成功"断言
    （原来续期失败也照样绿）
  - 新增 autouse 的 `_no_real_network` 守卫：默认禁止非本机地址的 socket 连接，
    以后新引入的真实出站会直接报错而不是悄悄拖慢整套

## [0.2.68] - 2026-11-18

### 修复

跳出 EOD 数据块做的全维度审查（安全 / 数据一致性 / 可靠性 / 代码质量 /
测试质量 / 文档一致性）的结果。安全维度无实质发现。

- **LLM 自检用错模型名**：探测请求硬编码 `model="probe"`。凡是"正常调用可用、
  但不存在叫 probe 的模型"的端点（OpenAI 兼容网关很常见）都会被自检判成故障，
  08:55 那次自检就推假告警。改为取配置里的真实模型。

- **续期脚本在写锁内等最长 600 秒**：`westock_renew_job` 把整段包在
  `with write_lock` 里，而脚本要等扫码。续期期间每秒的 tick、心跳、快照缓存
  全部停摆，watchdog 会判死——而独立健康文件照常刷新，Docker 看着是绿的，
  业务其实已经停了。现在锁只包住写 `app_secret` 的那几句，脚本在锁外跑。

- **LLM 故障的时间门槛会失效**：没有成功回合时，基线取"窗口内最早的失败"，
  最早的失败一滑出窗口基线就跳到更新的失败、门槛重新计时。实测 10:00/10:05
  两次失败时 10:10 判 true，10:10:01 就变回 false——真故障里告警自己消失。
  改为以"最近一次成功之后的第一条失败"为故障起点（没有成功记录时取全历史第一条）。

- **闹钟的标的与备注没有进模型请求**：`Trigger` 上一直挂着 `target`/`note`，
  但渲染器从没读过，不同闹钟产生完全相同的请求，模型不知道要回访哪只票。
  现在声明为正式字段并渲染进 messages。

- **首页在午休后误标红**：`/api/status` 的活动年龄只看最后一回合，
  watchdog 却按"本时段起点"算。午休正常休眠后 13:00 开盘会算出 100 分钟，
  把正常状态标红。两端统一为 `max(最后一回合, 本时段起点)`。

- **RUNBOOK 两处与实现矛盾**：轮换 HMAC 密钥原写"只重启 api"，但 jobs/agent
  各自长期持有 `Dispatcher` 并缓存旧密钥，只重启 api 会让新通知的免密链接
  全部验签失败——改为三个服务一起重启。默认验收原要求先出现 Cloudflare Access
  邮箱验证，与现行 Basic 方案矛盾，改为直接 curl `/healthz`。

## [0.2.67] - 2026-11-17

### 修复

- **回填 CLI 的汇总行没有 `gap_unfilled`**。这个计数非零表示"有待补记录、
  但这次上游没补到目标日"（marker 被保留，下次还会来补）。不打印的话，
  运维看到 `done` 涨了会以为缺口已经补上，其实还差那几天。

## [0.2.66] - 2026-11-17

### 修复

- **老库升级时旧格式的 `gap_pending` 记录仍会被误清**。上一版把写入语义统一成
  "缺口目标日"，但升级前落下的行存的还是 `last_date`；删 marker 前的检查
  对着那个日期一看"有 bar"，照样误删，旧洞失去续跑入口。

  新增迁移 `006_gap_pending_target_day.sql`：判别很干净——目标日按定义没有 bar、
  旧格式的 `last_date` 按定义有 bar，所以"该日有 bar"的行就是旧格式，
  推进到下一个交易日。生产库该表当前 0 行，这条迁移对全新库是空操作，
  保的是"老库升级"这条路径。

- **人工 backfill 收到非空但没覆盖目标日的返回时也会清 pending**。
  `_log(ok)` 一写，下次续跑就跳过它了。现在清 marker 前确认目标日已补到，
  否则计数到 `gap_unfilled` 并保留记录。

- 同步 `005_gap_pending.sql` 里过期的字段注释（仍写着"该日之前有空洞"）。

## [0.2.65] - 2026-11-17

### 修复

- **`gap_pending.missing_through` 的语义与消费方式不一致**（既有问题）。
  生产路径写入的是 `last_date`——"缺口**之前**已有的最后一根"，而 `_clear_gap`
  检查的是"这一天有没有 bar"。于是补上本轮 T-1 之后 `_clear_gap` 恒为真，
  把代表**旧洞**的 marker 一起删掉：旧洞失去默认续跑入口，而后续正常 EOD
  只看最新末根，再也不会把它建回来。

  现在语义统一为"缺口的目标日"（即"这一天没拿到"），所有调用点传 `prev_day`。
  `backfill` 只关心 code 在不在表里，不受影响。

- **新鲜快照的部分补数漏建 pending**（v0.2.64 引入）。加了 T-1 校验后，
  "写进去了 bar 但 T-1 仍缺"的分支只加了 `filled`，没记错误、没留 marker，
  第二遍又因 `filled` 跳过该票——历史缺口失去续跑入口。现在这个分支同时
  记错误 + 留 marker + 让 adj 重建。

## [0.2.64] - 2026-11-17

### 修复

- **补上新 T-1 会误清旧缺口的 marker**（既有问题）。`_clear_gap` 原来按 code
  整条删除，可 `missing_through` 记的是**旧洞**缺到哪一天：补上本轮 T-1 之后，
  旧洞失去默认续跑入口——后续正常 EOD 只看最新末根，旧洞再也不会被建回来。
  现在删 marker 前先确认 `missing_through` 那一天确实有 bar，否则保留给 backfill。
  （不能拿"该股 bar 数 vs 全市场交易日数"来比：长期停牌、上市较晚的票天然偏少，
  那样会永远保留 marker、每轮重试。）

- **部分补数写了 raw 却不重建 adj**（v0.2.63 引入）。加了 T-1 校验后，
  "补到了更早的日期但没补到 T-1"的分支会 `continue`，那条已落库的 bar 没进
  `filled`，于是 raw 比 adj 多一根——`_evaluate` 会以 `no_adj` 把这只票整只淘汰。
  现在已落库的 bar 一律进 `filled`，adj 跟着重建。

## [0.2.63] - 2026-11-17

### 修复

终审复核（v0.2.62）指出、由"缺口补拉移出事务"重构暴露的两个 P1：

- **非空旧日线仍被当成缺口补齐**。上游"成功"时若只回一根更早的旧日线
  （限频时常见），`_write_gap_bars` 写入了东西就返回 True，于是 T-1 仍缺、
  待补标记却被清掉——默认 backfill 见到旧 `ok` 记录就跳过，缺口永久丢失。
  现在写入后必须确认**目标 T-1 确实存在**才算补齐。

- **第二遍失败分支留下未提交的隐式事务**。第二遍不在第一遍的 `with conn` 里，
  失败分支要 `_log` / `_defer_gap`（写库），不开事务就一直悬着；它只在后面
  `rebuild_adj` 恰好也写库时才被顺带提交。没有 append、没有成功补拉的轮次里
  那笔事务始终不提交：启动恢复时 EOD 用独立连接，这个悬空事务会让它自己
  后面的 marker 写入撞锁（`database is locked`），记录在别的连接看来也不存在。
  现在第二遍整段包进 `with conn`。

## [0.2.62] - 2026-11-17

### 修复

终审复核（v0.2.61）指出的两个 P1 回归——都是把我"事务外预取"那一步接上时
引入的：

- **预取顺序与写入配额错序**：预扫描按自己的顺序选前 `GAP_MAX`，
  两遍循环又各自重新认领配额，结果预取到的票被写入阶段挡掉（**请求白花、
  数据白拿**），而真正没请求到的票又没留下待补标记——默认 backfill 见到
  旧 `ok` 记录就跳过它，历史缺口再也补不回来。

  现在**配额一次定完**：预扫描按"第一遍（快照新鲜且缺口）→ 第二遍
  （快照缺行/陈旧）"排好候选、按 `GAP_MAX` 截断，两遍循环只消费这份计划，
  不再自行认领。并且只在"本遍确实会处理它"时才消费配额，
  第二遍不会被第一遍误占。

- **取数失败/空结果被当成补拉成功**：`_fetch_gap_bars` 返回 `None`（网络失败）
  或空数组（上游成功但没给 bar，限频/停牌/指数代码都会这样）时，
  原来照样计入 `gaps` 并 `_clear_gap`——待补标记被删掉，缺口从默认续跑范围
  里消失。现在 `_write_gap_bars` 返回"是否真的写入了 bar"，没写入就记错误、
  留待补标记，不进 `filled`。

## [0.2.61] - 2026-11-17

### 修复

终审复核（v0.2.59）指出的根本问题：

- **缺口补拉的 HTTP 仍在 EOD 长事务里**。上一版加了 `_fetch_gap_bars` /
  `_write_gap_bars` 但**没有接上**——`run_eod` 的两遍循环都还在事务内调用
  `_backfill_gap`。EOD 事务本来就要写几千行、跑 1～3 分钟，再塞进 30 根 K 线的
  请求，SQLite 的单写者锁被占得更久：快照线程和请求记账等满 `busy_timeout`
  就 `database is locked`，而记账钩子还会把这种失败静默吞掉。

  现在分两段：**事务外**判缺口并取数（预扫描，跳过停牌股、按 `GAP_MAX`
  与循环同序收集），**事务内只写**。并补了测试：在 `fetch_kline_daily`
  被调用那一刻断言 `conn.in_transaction` 为假。

- 清理两处 `if True:` 死分支，并修正"补跑 EOD / 写 marker 都在 loops.lock 内"
  这类与实际实现矛盾的注释（EOD 已改用独立连接）。

## [0.2.60] - 2026-11-17

### 修复

- **EOD 独立连接的 `busy_timeout` 从 5 秒提到 60 秒**。EOD 一个事务要写几千行、
  跑 1～3 分钟，期间快照线程会在另一条连接上要写锁；5 秒一过就
  `database is locked`，EOD 输掉这场竞争，整轮回滚后重跑还是同样结果。

## [0.2.59] - 2026-11-17

### 修复

终审复核（v0.2.58）发现的问题：

- **严重（v0.2.58 引入）：EOD 补跑解锁后与快照线程共用连接，事务交错**。
  上一版把 EOD 从写锁里移出来是对的，但没换连接：共享连接是
  `check_same_thread=False`，多线程能调用却**共用同一个事务状态**——EOD 写了一半
  时快照线程在同一个连接上 `with conn` 提交，会把 EOD 的**半成品事务一起提交**。
  实测后果：第一根 bar 残留、第二根回滚、`daily_bars_adj` 为空。
  现在 EOD 补跑用**独立连接**，两边事务彻底隔离；marker 仍走共享连接由写锁保护。

- **错配块仍会被解析（M3 未完整修复）**。上一版只把错配块挡在确认集之外，
  解析遍历的仍是原始 `data`：块内 `code` 与键不一致的条目会按块内 `code` 落库，
  **覆盖那只票的既有派息**——而它可能根本不在本次请求里。
  现在只解析已确认的块。

## [0.2.58] - 2026-11-17

### 修复

- **启动恢复的 EOD 补跑整个跑在写锁里**，而 `run_eod` 会取全市场快照、
  还可能回填缺口——一个交易日十几秒到几十秒，这段时间里每秒一次的盘中
  tick 全部被堵死。现在联网部分在锁外执行，只有写 marker 那一小段持锁。
  （连接是 `check_same_thread=False` + WAL，`run_eod` 自己的写入自带事务，
  不需要这把锁保护。）

## [0.2.57] - 2026-11-17

### 修复

- **多股票块的块内 `code` 与外层键不一致时会误确认或写错票**。
  解析动作用的是块内 `code`，确认用的是外层键：两者不一致时会出现
  "确认了 A、数据写到 B"或"确认了 A 但一行没写"，A 就被游标永久跳过。
  现在要求块内 `code` 存在、有效且与外层键一致。

## [0.2.56] - 2026-11-17

### 修复

- **两个 `resume_dates` 口径分叉**。`core/strategy/pool.py::_resume_dates` 按
  "`end_date` 之后第一个交易日"算（bisect，容忍 `end_date` 落在休市日），
  而 `jobs/eod.py::resume_dates` 要求 `end_date == 上一交易日`（严格相等）。
  同一只票在两个模块里的"复牌日"可能不一致，尤其对旧版本按自然日 −1
  写下的遗留数据。现在 `eod.resume_dates` 统一到同一口径，并加了一条
  **跨模块一致性**测试（周末 `end_date`，断言两侧认同一天）。

## [0.2.55] - 2026-11-17

### 修复

Codex 复核 v0.2.54 后指出的四处遗留问题：

- **零成交量的占位 bar 被当成复牌证据**。停牌期间有的数据源仍会给一根
  `volume=0 / amount=0` 的 bar（`core/datasource/tencent.py` 接受零量），
  拿它闭合停牌区间会**伪造复牌日**，还把停牌天数少算
  ——`suspended_250` 偏低，本该剔除的票留在池里。
  现在要求 `volume > 0`；只看 volume 不看 amount（部分源本来没有成交额）。

- **缺 `plans` 的多股票块仍被确认**。`_action_of` 只认
  `{"code","plans":[…]}`，但"确认"用的是更松的判定：缺 plans 的字典、
  任意列表都进了 done 集合。现在确认集与可解析结构**同一套判定**。

- **重试保留分支仍按字段数比较**（`len(best)`）。单股票结构有 3 个字段，
  于是"2 只的响应"比不过"1 只的响应"，更好的重试结果被丢弃。
  改成用 `best_count`（股票数）比较。

- **`advance_actions_cursor` 的三个直接调用边界**：`start=None` 时回到开头
  重算（连续三参调用会原地踏步）、空确认集直接归零（应停在起点）、
  退市起点回退到索引 0（应按二分定位）。三处都按 `actions_cursor` 的口径修正。

## [0.2.54] - 2026-11-17

### 修复

- **"未定复牌"的停牌区间永不闭合**。接口只给"当前正在停牌"的快照：某只票
  复牌后就不在返回里了，而刷新只更新本轮返回的行，那条 `end_date IS NULL`
  的记录永远留着——于是**复牌之后的每个交易日都被算成停牌**，
  `stock_stats.suspended_250` 虚高、`resume_cooldown` 永不触发。

  现在按**真实成交证据**闭合：有该股晚于停牌起始日的 `daily_bars` 才算复牌，
  `end_date` 取复牌后第一根 bar 的前一个交易日。
  **不能凭"这次快照里没有它"闭合**——限频、数据源失败时同样没有它，
  那样等于伪造复牌日。这个区分专门有测试钉住。

## [0.2.53] - 2026-11-17

### 修复

Codex 回归审查（以 v0.2.50 为基准）发现的 1 项严重 + 3 项中等：

- **严重：启动恢复对不可重入锁重复取锁，真实刷新会死锁**。`startup_recovery`
  先 `with write_lock` 再调回调，而回调遵循 `f(lock=None)` 契约、会在写库那一步
  自己取同一把锁；生产锁是 `threading.Lock()`（不可重入），同线程二次取得就死锁。
  触发条件是「`refresh_actions.last` 缺失或满 7 天 + 任一刷新返回成功数据」。

  后果很严重：`sched.start()` 在恢复之后，主线程一卡调度器永远起不来；
  而心跳线程是提前起的，容器健康标记照样绿——**服务看着正常，实际什么定时任务
  都不跑**。上线后观察到的"滚动任务从不触发"很可能就是这个（当时误判为日志问题）。

  修法不是换成 `RLock`（那只是掩盖外层锁作用域错误，还会保留"锁内联网"），
  而是把锁**传进去**由回调自己在写库时持有，盖戳那一小段单独加锁。
  测试用**真实的 `threading.Lock`** + 10 秒超时守护，变异回旧写法会红。

- **业务失败信封里带 data 时失败标记丢失**：`_unwrap` 在检查 `ok` 之前执行，
  于是 `{"ok":false,"msg":"服务限频","data":{…}}` 被当成"这些票没分红"，
  静默丢数据还推进游标。改成先查业务成功标记再剥壳。

- **`{"sh600000": null}` 之类的坏结构被算成"确认处理"**，游标据此永久跳过这只票。
  现在校验股票块结构（`plans` 必须是列表），坏结构不计入确认集。

- **单股票结构按字段数计票**：`{code,end,plans}` 有 3 个字段，`len()` 算成 3 票，
  于是"请求 4 只只回 1 只"被误判成够数，跳过了"不足一半就退避"的判断。
  改成按 `_codes_in()` 数**股票**。

## [0.2.52] - 2026-11-17

### 修复

- **`_call_with_lock` 会把锁塞给 `*args` 形式的回调**。原来只看"参数列表非空"，
  于是 `def f(*args)` 也被当成"能接锁"，那个回调拿到的是一把锁而不是它以为的
  参数。改成判断是否存在**可接收位置参数**的形参（`POSITIONAL_ONLY` /
  `POSITIONAL_OR_KEYWORD`），并加测试钉住三种形态（`*args` / `lock=None` / 零参）。

## [0.2.51] - 2026-11-17

### 修复

第三轮回归审查（独立子代理）在 0.2.50 上发现的两个真缺陷 + 若干文案问题：

- **严重：停牌 `end_date` 的交易日历换算在生产路径上从未生效**。
  0.2.44 给 `store_suspensions()` 加了 `cal` 参数，0.2.47 把停牌拆成
  `fetch_suspensions()` + `store_suspensions()` 时**忘了传 cal**，
  于是退化成"复牌日 − 1 天"的自然日近似：周一复牌落成周日
  （实测 2026-10-12 → `20261011`，应为 `20261009`），
  而 `resume_dates()` 要求 `end_date == 上一交易日`，`resume_cooldown`
  永远迟一天，节后首日迟得更多。

  更糟的是旧测试把这个错误口径钉成了断言（"0903 → 0902"），所以它一直是绿的。
  现在生产路径传 `TradingCalendar`，并把测试改成**从生产钩子走一遍**
  （`make_refresh_hooks` → `_suspensions`），变异验证不传 cal 时会红。

- **严重：游标按 `max(done)` 推进，会跳过中间残缺段**。限频时常出现
  "开头几段正常、中间某段只回 1 只、后面又正常"，按最大值推进就把中间
  没确认的 9 只整段跳过，要等滚动一整圈（约 3 天）才再遇到——
  而 0.2.44 加 `done` 集合的初衷正是"没出现的票留到下一轮"。

  现在只推进"从本轮起点开始的连续确认前缀"，遇到断点就停在那里。

- **`_trace()` 并发写丢行**：滚动任务和启动恢复会同时调用它，
  而"读-改-写"三步不是原子的。实测 4 线程 × 40 行只保留 144/160 行。
  加 `_TRACE_LOCK` 并补并发测试。

- **`_call_with_lock` 会把锁塞进 `*args` 回调**：原来只看"参数列表非空"，
  于是 `def f(*args)` 也被当成"能接锁"，拿到的是一把锁而不是它以为的参数。
  改成判断是否存在可接收位置参数的形参。

- **文案与实现矛盾**：`ACTIONS_MAX_REQUESTS` 的注释写"6 个请求/60 只"
  （实际 4 个批次、每批 10 只，且批次内还会重试，真实请求上限是 4×3+1=13）；
  `suspension()` 的 docstring 开头说"`end_date` 是最后一个停牌日"、
  正文又说"原样返回复牌日"，自相矛盾。均按实现改正。
  `FakeWestockCli.suspension()` 的返回值语义也和真实实现不同
  （喂的是已换算的值），补了说明。

## [0.2.50] - 2026-11-16

### 新增

- 收到 `SIGUSR1` 时把所有线程的调用栈打到 stderr（`faulthandler.register`）。
  容器里没有 gdb / py-spy，而宿主机日志管道又不通，进程卡住时原本没有任何
  手段看到它卡在哪——只能靠猜和反复重启。

## [0.2.49] - 2026-11-16

### 修复

- 滚动刷新的 trace 行加上 `mcp` 的实际类型，便于判断"跳过"是因为客户端没接上
  还是能力缺失。

## [0.2.48] - 2026-11-16

### 修复

- **滚动刷新的执行痕迹只写进（可能为空的）容器日志，排障时无从下手**。
  生产环境 `docker logs` 连 `logger.info` 都不落（宿主机日志管道问题，与代码无关），
  于是"这轮到底跑没跑、是跳过了还是失败了"完全看不见。现在每轮往
  `/tmp/actions_refresh.trace` 追加一行（开始 / 跳过原因 / 写入行数 / 异常），
  保留最近 200 行。

## [0.2.47] - 2026-11-16

### 修复

- **停牌刷新仍然在写锁里联网**。上一轮把除权拆成了 `fetch_actions()`（锁外）+
  `store_actions()`（锁内），但停牌那边调用的 `refresh_suspensions()` 是
  「取数 + 写库」一体的，仍然整个跑在 `with lock` 里——一次 HTTP 就能把每秒的
  tick 堵住好几秒，撞限频时更久。现在拆出 `fetch_suspensions()`，
  两个刷新都遵守「锁外取数、锁内写库」。

## [0.2.46] - 2026-11-16

### 修复

- **启动恢复期间容器被判 unhealthy**。心跳线程（`_touch_loop`）原来在
  `sched.start()` 之后才起，而 `startup_recovery` 就在它前面——滚动刷新要拉
  几分钟除权数据，`/tmp/jobs_healthy` 一直不刷新，而 compose 的
  `start_period` 只有 300 秒、healthcheck 判的是「2 分钟内 touch 过」。
  线上实测 `Up 13 minutes (unhealthy)`、`FailingStreak=9`。
  现在心跳线程在启动恢复**之前**起，并加测试钉住这个顺序。

## [0.2.45] - 2026-11-16

### 修复

- **停牌失败会把除权一起拖下水**。启动补跑里两个刷新的结果被绑在一起判失败：
  停牌只有 1 个请求、撞上限频的概率不低，一旦它失败就整次不盖时间戳，
  于是分红已经写进库的数据**每次启动都要重拉一遍**，持续烧配额、持续撞限频
  ——上线后实测正是这个状态（除权覆盖 740 只、停牌 0 行、时间戳空）。

  现在只按除权是否失败决定盖不盖戳，停牌失败单独记 WARNING 下次再试。

- **每轮分红请求数从 6 降到 4**：停牌那 1 个请求也要占上游额度，
  6 + 1 = 7 已经贴着「连发 8–10 个就限频」的线，实测启动时有相当概率
  把停牌挤成限频失败。4 + 1 = 5 留足余量（代价是每轮覆盖的票少 20 只）。

## [0.2.44] - 2026-11-16

### 修复

Codex 第二轮审查（以 0.2.41 为基准、并复核到 0.2.42）确认/新增的问题：

- **严重：停牌写入仍无共享写锁**。上一轮只把除权写入放进了锁内；`refine_susp`
  在 `startup_recovery` 与滚动任务里都还在锁外直接写 `suspension_calendar`，
  与 tick 共用同一条 `check_same_thread=False` 的连接。Codex 用真实 SQLite
  复现了「第一条 INSERT 后被心跳提前提交、刷新回滚后仍留 1 行」。现在两个回调
  统一按 `f(lock=None)` 契约在写库那一步持锁；零参回调由 `_call_with_lock()`
  兼容（用 `inspect` 判断，不用 `try/except TypeError` 那种会吞掉内部错误的写法）。
- **`end_date` 语义写反了：上游给的是复牌日，下游要的是「最后一个停牌日」。**
  `core/strategy/pool.py::_resume_dates` 用 `bisect_right` 取 `end` 之后第一个
  交易日，`jobs/eod.py::resume_dates` 按 `end_date == 上一交易日` 找复牌股——
  两者都要求 `end_date` 是停牌的最后一天。原来原样写入复牌日，策略会**晚一个
  交易日**才识别到复牌。现在写库时按交易日历往前推一个交易日（`store_suspensions`），
  无日历时退回「复牌日 − 1 天」的自然日近似；未定复牌仍存 NULL。
- **写入官方除权事件后没有重建复权表**。滚动刷新常在 EOD 之后或次日盘前跑，
  这段时间里所有读 `daily_bars_adj` 的消费者拿到的仍是按旧事件算的价格
  （Codex 实测：新增每股派息 0.5 后，复权表没变）。现在 `store_actions` 写完
  就对受影响的代码调 `rebuild_adj(code=…)`——它用 `actions_hash` 判断，
  未变的票会跳过，很便宜。
- **游标按整段推进，把没拿到数据的代码也跳过了**。`batch_dividend` 现在返回
  `(数据, 确实收到响应的 code 集合)`，游标只按第二项推进；限频伪装成空/残缺
  数据时，没出现的票会留到下一轮重试，而不是等滚动一圈（约 3 天）后才再遇到。
- **内层业务失败 / 坏结构仍被当成成功的零行**。`data_dividend` 的内层
  `{"ok":false,"msg":"服务限频"}` 现在也算失败（返回 `None`），不再被当成
  「这些票没分红」。
- **限量调用时「全部尝试失败」仍可能返回 `{}`**：`total_chunks` 按全部 codes 算
  会把预算外未尝试的批次也算进去。改成按"是否有任何代码被确认处理"判定。
- **文档与实现矛盾**：`batch_dividend` 注释说送股/转增「已经是每股口径、不用
  再除 10」，实际三个字段都除以 10；模块 docstring 说「没有周期任务」，
  实际已有两个 cron。均按实现改正，并补了一条**非零送股比例**的测试
  （审查指出送股的 /10 之前完全没被验证——去掉它 8 个测试仍全绿）。

## [0.2.43] - 2026-11-16

### 修复

审查（独立子代理 + Codex）在 0.2.42 上发现的问题，逐条修：

- **严重：生产路径的退避代码从未执行过**。`self.sleep = sleep`（默认 `None`），
  而生产实例不传这个参数，于是 `if self.sleep:` 永远为假——0.2.38 宣称的
  「撞限频后退避 2 秒」在线上一次都没发生。改成默认 `time.sleep`，
  并补一条「不注入 sleep 时必须是 `time.sleep`」的测试（原来的测试全都注入了
  假 sleep，所以一直绿）。
- **严重：同一天「既派现又送转」的多条 plan 被静默覆盖**。落库是
  `INSERT OR REPLACE`，主键 `(code, ex_date, source)`；MCP 路径逐条写、
  后一条盖前一条，而 CLI 路径一直有 `_aggregate` 求和。实测两条 plan
  （0.58 现金；0.2 现金 + 0.3 送转）落库只剩 `(0.2, 0.3)`，
  正确应为 `(0.78, 0.3)`——复权因子 `(P−Σ现金)/(1+Σ送转)` 直接算错，
  而且 `written` 虚高一倍。现按 `ex_date` 聚合，与 CLI 同口径。
- **中等：停牌的业务层失败被当成「今天没人停牌」**。上游限流返回
  `{"ok":false,"msg":"服务限频"}`，JSON-RPC 外层仍是成功；只判外层就会把
  限频记成空表成功。现在业务层 `ok=false` 也返回 `None`。
- **中等：游标在「一条都没写入」时仍前移**。限频伪装成空数据时，这等于
  把这 60 只永久跳过（滚动一圈约 3 天）。改成只有确实写入才前移。
- **中等：整轮退避会超过调度器的 `misfire_grace_time`**。
  6 个 chunk × (30+60) 秒 = 540 秒 > 300 秒，下一次触发会被静默丢弃。
  加单轮退避总预算（默认 120 秒）。
- **中等：定时任务有两次落在盘中**（09:30、15:00），与会抢行情限频额度的
  设计意图矛盾。改成 `6-8,16-23` 点 + 开盘前 09:00/09:10 + 收盘后 15:20/15:40，
  周末同样处理，并加「所有触发点都不在 09:15–15:10」的测试。
- **轻微：未定复牌日存成了「今天」**。接口用 `19000101` 表示"还没定"，
  原来换成今天，等于告诉下游"今天复牌"——`_resume_dates()` 用 `bisect_right`
  取 end 之后的第一个交易日，`resume_cooldown` 会被提前触发 1–2 天，
  且那条记录每天被新值覆盖、永远清不掉。表结构 `end_date` 本可为 NULL、
  下游也认 `None = 仍在停牌`，改存 NULL。
- **轻微：停牌刷新写库没持锁**（与 tick 共享同一个 conn）；**WARNING 文案**
  说「返回 None」但可能只是停牌侧失败；**清理**不再使用的 `refresh_actions` 导入，
  并给 `eod.refresh_actions` 标注「仅测试用，勿用于生产调度」。

## [0.2.42] - 2026-11-16

### 修复

- **滚动刷新的网络请求原本在写锁内，会把每秒的 tick 全堵住**：一次取数要发
  6 个请求还带退避（撞限频时 30 秒起）。现在拆成 `fetch_actions()`（锁外）+
  `store_actions()`（锁内），并用
  `test_actions_refresh_job_fetches_outside_the_write_lock` 钉住这个不变式
  （`test_members_job_does_not_fetch_inside_the_lock` 记录过同类故障：
  心跳等锁超 30 秒导致 `maximum number of running instances reached`）。

## [0.2.41] - 2026-11-16

### 文档

- README 补上除权数据的**滚动覆盖**说明：上游限频很紧，首装当天只覆盖一小部分
  属于正常现象，几天后才滚完全市场。不写清楚的话，用户看到
  `corporate_actions` 行数少会以为功能坏了。

## [0.2.40] - 2026-11-16

### 修复

- **一次调用拉不完全市场，这是上游的硬限制，不是重试能解决的**：
  同一请求里塞 200 只票只返回 46 只、400 只只返回 21 只；10 只一批才稳定
  满额；连发到第 8–10 个请求就开始返回空 `ok=True` 数据，冷却约 60 秒恢复。
  也就是说 5583 只票不可能一次拉完——原来的"一次性全量重拉"注定只覆盖开头
  一小段（实测入库 245 只）。

  改成**游标 + 周期推进**：每轮最多 6 个请求（60 只），游标存在
  `job_state('refresh_actions.next')`，每 30 分钟推进一段，走到末尾绕回开头，
  几天滚完一遍全市场。避开盘中时段（行情请求优先）。默认批次从 50 改成 10，
  退避从 2 秒改成 30 秒。

## [0.2.39] - 2026-11-15

### 修复

- **限频的伪装是「`ok=True` + 空数据」，靠报错是抓不到的**：实测连续调用
  `data_dividend`，返回的票数是 50 → 17 → 0 → 1 → 0…，全程 `ok=True`、
  不报错，冷却一两分钟才恢复。原实现只看 `ok`，于是这些空返回被当成
  「这些票十年内没分红」——`corporate_actions` 只写进了 174 只票（全库 5583 只），
  而数据缺失是静默的。

  现在按**条数**判定：返回票数不足请求数一半就当限频，退避后重试（最多 3 次，
  退避 5/10/15 秒），取到最多的那次结果。B 股、新股本来就可能没有分红记录，
  所以是"不足一半"才重试，不是"必须全中"。

## [0.2.38] - 2026-11-15

### 修复

- **撞上限频后没有退避，且两个刷新的顺序是反的**：

  失败的批次原本只是跳过，紧接着就把剩下的批次继续硬发——连续 100 多个请求
  本来就贴着上游限频线。现在失败后**退避 2 秒**（前 3 次）再继续。

  更关键的是**顺序**：原先先分红（100 多个请求）、后停牌（1 个请求）。等分红
  打完，停牌请求正好撞上限频返回 `isError`，整次刷新被判失败、时间戳不盖，
  下次启动从头再来一遍。改成**先停牌、后分红**。

## [0.2.37] - 2026-11-15

### 修复

- **上游「服务限频」会让整个除权刷新白跑**：5000 多只票要发 100 多个请求，
  中间撞到限频很正常（实测第 112 个批次后的停牌请求就返回
  `isError: 高级选股异常 msg=服务限频`）。原实现「一个批次失败就 `return None`」，
  于是每次启动都从头重来、再撞一遍限频，永远跑不完。

  现在按批次容错：失败的批次记 WARNING 并跳过，其余照常入库；
  只有**全部批次都失败**才返回 `None`（那才是真的没取到数据，不该盖时间戳）。

## [0.2.36] - 2026-11-15

### 修复

- **刷新失败也照常盖「已完成」时间戳，导致 7 天内不再重试**：`startup_recovery`
  在调用 `refresh_actions()` 后无条件写 `refresh_actions.last`，于是"取数失败但
  没抛异常"（账号没配、上游改结构、解析错）会被按「距上次 ≥7 天」推到下周。
  0.2.33 上线时正是这样：时间戳落下，`corporate_actions` 官方行一直是 0，
  而整条链路不报错。

  现在**失败与成功用 None/数字区分**：`batch_dividend()` / `suspension()` 取数失败
  返回 `None`，成功返回列表或条数；只有成功才盖戳，失败打 WARNING 让下次启动重试。

  **`0` 和 `None` 必须分开**：停牌返回 0 行是正常结果（今天没人停牌），
  如果因为 0 就不盖戳，等于把「7 天一次」变成「每次启动全量重拉一次」。

## [0.2.35] - 2026-11-15

### 修复

- **刷新写了 0 行也照常盖「已完成」时间戳**：`startup_recovery` 在调用
  `refresh_actions()` 后无条件写 `refresh_actions.last`，于是"取数失败但没抛异常"
  （账号没配、上游改结构、返回空）会被按「距上次 ≥7 天」推到下周才重试。
  0.2.33 上线时正是这样：写 0 行、时间戳落下，`corporate_actions` 官方行一直是 0，
  而整条链路不报错。

  现在只有**确实写了行**才盖戳，否则打 WARNING 并让下次启动重试。
  `None` 表示"回调没报错也没给数字"（老测试桩如此），不能当成 0。

## [0.2.34] - 2026-11-15

### 修复

- **0.2.33 的除权/停牌取数实际上一条都没写进库**：`_rpc()` 返回的
  `data` 里还包着一层 `{"ok":true,"data":{…}}`，业务数据在里层，于是把 `True`
  和字典当成了两个"股票块"去遍历，结果静默取不到任何数据。

  根因值得记一笔：**测试 fixture 用了平铺数据，真接口是两层的**。单测全绿、
  线上为空——这类"假数据比真数据简单"的漏法最容易骗过自己。fixture 现在照
  真实接口的形状写（`_wrapped()`），把 `_unwrap()` 拿掉会让测试变红。

  部署 0.2.33 后实测：`corporate_actions` 官方来源 0 行、`suspension_calendar`
  0 行——这个数字是发现问题的唯一线索，因为整条链路不报错。

## [0.2.33] - 2026-11-15

### 新增

- **除权除息与停牌取数真正可用**：`WestockMcp` 补上 `batch_dividend()` /
  `suspension()`，数据来自 MCP 的 `data_dividend` / `data_suspension`。
  至此启动日志里最后一条 WARNING（「除权/停牌周刷新未接线」）消失，
  `corporate_actions` 不再只有快照反推的 `snapshot_implied` 行。

  实现细节全部来自**实测抓包**，不是照文档猜的：
  - `cashDiviRMB` / `bonusShareRatio` / `tranAddShareRatio` 是「每 **10 股**」刻度，
    落库要除以 10。证据是同一行的 `dividendPlan` 文本：`cashDiviRMB='5.80'`
    对应 `10派5.800元转4.000股`、`tranAddShareRatio='4.00'` 对应「转 4.000 股」。
    不除 10 会让每股现金虚高十倍、复权因子整个算错。
  - 多只票的返回是**以 code 为键的字典**，单只票是 `{code, plans}`——
    两种结构都出现过，只按一种解析会让多只票一条都取不到（而且看起来像
    "这段时间没有分红"，最难发现）。
  - 只保留 `procedure='方案实施'` 的行：预案里也会填除权日，只按"有没有除权日"
    过滤漏得掉，会让还没实施的方案提前影响复权因子。
  - 停牌接口用 `resumptionDate=19000101` 表示"还没定复牌日"，落库换成查询区间
    右端；原样存 1900 年的话记录永远清不掉、也永远命中不了复牌判定。


### 文档

- **清掉 docstring 首行的 `Task N：` 出处前缀**（53 个文件）。这些编号指向未随仓库
  公开的私有设计文档，对新读者只是噪音。只处理"模块/函数 docstring 第一行以标签
  开头"这一种形态——**句子中间的引用（`交给 Task 2 的重试分类`）刻意不动**：
  删掉编号会变成「交给 重试分类」，语义就破了，机器改不了。那些靠
  `docs/architecture.md` 末尾的编号对照表兜底。

## [0.2.32] - 2026-11-15

### 新增

- **GitHub Actions CI**（`.github/workflows/ci.yml`），四个 job：
  - `backend`：用 `deploy/Dockerfile` 的 dev 阶段构建后跑全量 pytest——
    与本地 `make test`、与生产镜像是**同一份定义**，不会出现「CI 绿、线上红」。
  - `version`：`tests/test_version.py`，纯文本检查不依赖镜像。
  - `frontend`：`npm ci` + `tsc --noEmit` + `vitest` + `build`。
  - `secrets`：扫工作区与**全部历史**，规则在 `scripts/scan_secrets.py`。
- `scripts/scan_secrets.py`：按类型判定的敏感信息扫描器（私网地址用 `ipaddress`
  解析、令牌按各家前缀精确匹配）。**没有写成一条大正则**——版本号 `10.4.3`
  和私网地址在文本上长得一样，靠正则加边界既会误报也会漏报；误报几次之后
  这条门禁就没人看了。配套 `tests/test_scan_secrets.py` 正反两面都钉住。

## [0.2.31] - 2026-11-14

### 修复

- **发版脚本会把作者真实邮箱写进历史**：`release.py` 的 `_git()` 用当前 shell 的
  git 配置，开发者本机配的是真名 + 真邮箱，于是每跑一次发版就往历史里多写一个
  真实邮箱——推到公开仓库后永久可见。现在显式 `-c user.name/user.email` 钉死。
  （连带修了假 git 测试桩：它假设子命令在 `$1`，加上 `-c` 之后全部失灵。）
- **`wb_client.json` / `wb_session.json` 没写进文档**：compose 单文件挂载这两个
  路径，宿主机文件不存在时 docker 会建一个同名**空目录**顶替，容器里读写它
  直接 `EISDIR`。这个坑只在别人机器上出现（作者本机文件早就有）。现在 README/
  RUNBOOK 的 `touch` 列表补上，并在启动时检查"是目录"并报 ERROR。
- **`docs/strategy.md` 说「其余因子三市一致」是错的**：B6/B9/min_amount_20d 在
  `config/strategy.yaml` 里都按市场分档。补齐表并改成"以 yaml 为准"。
  同时补上 C2 的分时段阈值（09:45–10:00 用 2.5，之后 1.5）。
- **RUNBOOK 的 `export UID GID` 在 bash 下必炸**（`readonly variable`），而 README
  专门写过这个坑。改成注释说明。
- **RUNBOOK 的回滚落点指向不存在的 `v0.1.0` / `de56875`**：公开仓库的历史是重建
  过的，这两个对象不在其中。改成"以 `git tag --list` 里最早的 `v0.2.x` 为准"。
- **`api/main.py` 4 个死 import**（`SourceRegistry`/`SinaSource`/`TencentSource`/
  `Http`/`RateLimiter`）与 `docs/architecture.md` 的「api 不碰上游」自相矛盾；
  `import os` 还排在用它的函数之后。一并整理到顶部 import 区。
- **前端工具链有 high/critical 漏洞**：vite 5.4.11 / esbuild 0.21.5 / vitest 2.1.8
  （含 esbuild dev server 任意读、vitest RCE 类公告）。升到 vite 8 / vitest 5 /
  plugin-react 6 / jsdom 31，`npm audit` 归零，45 个前端测试 + 构建 + tsc 全通过。

### 文档

- README 顶部加「部署前必读」：不设口令时所有接口不鉴权、而默认部署是公网可达的；
  数据源非官方；westock 脚本需用本人账号。
- README 补「其他已知限制」：交易日历内置到 2027、上游接口无版本协商、单实例设计。
- `docs/architecture.md` 末尾加**历史编号对照表**：代码注释里的 `D18`/`Task 6`/
  `§7.2.6` 来自未公开的私有设计文档，这张表把高频编号一次解释掉。
- 清理代码与注释里的本机痕迹：`/tmp/screener_*` 路径、真机型号 UA、
  个股观测记录、带具体时间戳的运行故障描述，统一改成通用表述。
- `scripts/wb_auth_headless.mjs` 文件头补明确的使用边界（仅本人账号、不得多账号）。

## [0.2.30] - 2026-11-14

### 修复

- **匿名与口令失败共用限流桶，会互相污染**：网页首屏十几个匿名 401 会把 IP
  顶到封锁，用户随后就算输对也进不来；反过来攻击者猜到第 9 次时用户只刷新了
  一次页面就被锁死。现在拆成两个独立的桶（匿名 200 次/120 秒，带口令
  10 次/900 秒），认证成功时两个都清。
- **被限流时返回 429 让登录入口消失**：429 不带 `WWW-Authenticate`，
  iOS Safari / Chrome 不会弹原生登录框，用户只看到一坨 JSON。匿名请求改为
  一律 401 + challenge（保留背压，但不进业务），只有带口令的猜口令才给 429。
- **`/api/chat` 没有同源校验**：它每调一次都花 LLM 预算，`/api/watch` 早有
  这道校验。补上后顺带发现一个更隐蔽的问题——本文件有
  `from __future__ import annotations`，`Request` 若在函数内导入，
  FastAPI 解析不到类型，会把它当成**必填查询参数 `?request=`**，
  导致每个请求 422、同源校验永远不执行。
- **取不到签名密钥只打 warning**：密钥失效时所有免密链接和 ntfy 按钮同时
  失灵，用户看到"点了没反应"，而 `/healthz` 不碰鉴权、报不出这个错。
  提级为 ERROR 并计数（每 100 次再报一次，避免刷屏）。
- **访问日志的非元组形状会明文放行票据**：过滤器只处理 5 元组，uvicorn 若
  改了参数结构，票据就明文进 `docker logs` 且没有测试会发现。现在非元组
  走整串打码。

### 测试

- hook 守卫从 AST/字符串检查改成**运行时正反双向验证**：真发请求，无票不能
  是 200（否则等于公开接口）也不能是 401（否则按钮点不动），带合法票据不能
  是 403。AST 版本漏得掉 `Depends(...)`、`try/except`、辅助函数三种写法。

## [0.2.29] - 2026-11-14

### 修复

- **网页首屏会把用户锁在外面**：网页首屏并行请求十几个 `/api/*`，用户换手机、
  清 Cookie、或从通知点进一个没有票据的页面，就会连收十几次 401。这些匿名 401
  原先用"猜口令"那档（10 次）计数，打开首页就被锁 15 分钟——正是免密功能要
  解决的问题本身。现在匿名请求走独立宽松档（200 次 / 120 秒），带口令的仍是
  10 次 / 900 秒。匿名请求不带口令、猜不出口令，放宽不削弱防护。
- **已登录时地址栏里的票据不抹**：先判 Cookie 会让"已登录 + 带票据的链接"
  直接 200，票据就留在地址栏和历史里（30 天可用）。现在只要 URL 带有效票据
  就 302 抹掉，并加 `Cache-Control: no-store`。
- **api 每个请求都跑一次数据库迁移**：`api/main.py` 用的是
  `make_conn_factory()` 默认值（`migrate_each=True`），而 `core/db.py` 的注释
  明确要求 api 用 `False`。每个未鉴权的公网请求都会建连接并 `BEGIN IMMEDIATE`，
  跟 jobs 的写事务抢锁。改为启动时迁移一次。
- **hook 守卫测试是假绿**：`test_hook_buttons_work_without_browser_credentials`
  用 GET 打两条 POST 路由，拿到的是 SPA 兜底的 404，断言 `in (403, 404)` 恰好
  放过。现在改用 POST，并断言响应体不是中间件的口令提示。

## [0.2.28] - 2026-11-14

### 修复

- **有效会话不再被口令限流连累**：手机上口令手滑几次会被锁 15 分钟，期间连点
  通知链接都进不来——而那正是这个功能要解决的问题。现在判定顺序是
  「已认证凭据（会话 Cookie / 推送票据）优先，不看封锁；没有凭据才算猜口令、
  才限流」。限流仍然完整地挡 Basic 猜口令。
- **失败达到阈值时当次就返回 429**：原先第 10 次失败给 401、第 11 次才 429，
  封锁在统计上永远晚一轮，也白送攻击者一次尝试机会。
- **带 `..` 的路径不借前缀放行**：`/api/hook/../api/status` 落在放行前缀下，
  虽然路由器实测返回 404，但「前缀放行」不该依赖「路由器恰好不规范化路径」。

## [0.2.27] - 2026-11-14

### 修复

- **免密登录第一版根本没生效**：票据换来的 Cookie 只写了 `ss_session`，
  而 Cookie 校验还要 `ss_session_exp`（到期时刻只存在于 URL 的 `exp` 里），
  于是 302 之后的下一次请求又弹 Basic。旧测试只断言「302 + Set-Cookie 里有
  ss_session」，所以全绿。现在两枚 Cookie 一起写，有效期取票据自己的 `exp`。
- **ntfy 的「标记已读」「确认执行」被中间件拦成 401**：这两个按钮是 ntfy
  服务端直接 POST，没有浏览器、没有 Cookie、也不弹口令框；全局 Basic 门槛让
  路由自己的 HMAC 验签根本没机会跑，功能一直是死的（生产库证据：94 条信号
  `read_at` 全为空）。现放行 `/api/hook/` 前缀——两个路由都强制 `verify()`
  + 过期判断，purpose 写进签名原文，三类票据互不通用。
- **百分号编码的查询键绕过日志打码**：`?%74=<票据>` 会被鉴权层解码成 `t` 并
  成功验签，而日志按原文比对 `%74` 不打码，等于把有效票据写进 `docker logs`。
  现按解码后的键比对。
- **任意非空 `?t=` 都能跳过失败计数**：`?t=garbage` 可无限试探而不进限流表。
  现只放过「签名有效但已过期」的票据。
- **`SS_SESSION_DAYS` 只影响一半**：Cookie 有效期读它，推送链接里写死 30 天，
  RUNBOOK 说的「可改」等于没生效。现为单一来源。

## [0.2.26] - 2026-11-14

### 新增

- **推送链接免密登录**：以前从 ntfy 点开任何一条推送都要先过一遍手机浏览器的
  原生口令框（HTTP Basic 凭据不被可靠记住），很难用。现在通知里的每个链接都带
  签名票据 `?t=<签名>&exp=<到期>`，点开即进；首次进入后下发 `HttpOnly` 会话
  Cookie，之后刷新、换页都不再询问。
  **裸敲域名（没有票据、也没有 Cookie）仍然要口令**——这是设计要保留的边界。
  票据 purpose 独立，与「标记已读」「确认执行」两类票据互不通用；
  访问日志里的 `t`/`exp` 自动打码，避免把凭据写进 `docker logs`。

### 修复

- **免密登录第一版根本没生效**：票据分支只写了 `ss_session` Cookie，而 Cookie
  校验还要 `ss_session_exp`（到期时刻只存在于 URL 的 `exp` 里），于是 302 之后
  的下一次请求又弹 Basic。测试当时只断言「302 + Set-Cookie 里有 ss_session」，
  所以全绿。现在两枚 Cookie 一起写，有效期取票据自己的 `exp`。
- **ntfy 的「标记已读」「确认执行」被中间件拦成 401**：这两个按钮是 ntfy
  服务端直接 POST，没有浏览器、没有 Cookie、也不会弹口令框；全局 Basic 门槛
  让路由自己的 HMAC 验签根本没机会跑，功能一直是死的（生产库证据：94 条信号
  `read_at` 全为空）。现放行 `/api/hook/` 前缀——两个路由都强制
  `verify()` + 过期判断，purpose 写进签名原文，三类票据互不通用。
- **百分号编码的查询键绕过日志打码**：`?%74=<票据>` 会被鉴权层解码成 `t` 并
  成功验签，而日志按原文比对 `%74` 不打码——等于把有效票据写进 `docker logs`。
  现按解码后的键比对。
- **任意非空 `?t=` 都能跳过失败计数**：上一版只看 `t` 是否非空，于是
  `?t=garbage` 可以无限试探而不进限流表。现只放过「签名有效但已过期」的票据。
- 票据有效期改为单一来源：`SS_SESSION_DAYS` 原先只影响 Cookie，推送链接里
  写死 30 天，RUNBOOK 说的「可改」等于没生效。

## [0.2.25] - 2026-11-13

### 修复

- **部署脚本 `--exclude` 让首次部署丢失配置**：它无条件匹配归档成员，不看目标文件
  是否存在，首次部署时 `config/notify.yaml` 与 `deploy/ntfy/server.yml` **一份都不写入**
  → 容器 `ConfigError` 起不来、ntfy 单文件挂载失败。改用 GNU tar 的
  `--skip-old-files`（不覆盖已存在的文件），并先校验远端 tar 支持该选项。
  补上真跑 tar 的测试（原守卫只做字符串断言，这个缺陷带着全绿测试通过了）。
- `.dockerignore` 排除 `.tmp_smoke/` 与 `.firecrawl/`：它们是 gitignore 的，但
  `Dockerfile` 用 `COPY . /app`，本地构建会把审查 spec（含真实域名/IP）打进镜像。
  `.gitignore` / `.dockerignore` 一并覆盖 `.env.local` 等变体。
- **构建失败被 `| tail -1` 吞掉**：管道退出码取 `tail`，构建失败仍会用**旧镜像**
  重建容器并报告成功。远端命令加 `set -o pipefail`。
- 首次部署不再要求目标机已有 `data/market.db`（新建机器会直接被挡住）。
- API 侧占位域名告警定义了却没调用；现接在 `app` 装配前，并按**调用点**守卫。
- 网页口令告警从 `jobs` 移到 `api`：`web.env` 按 compose 只挂给 api，
  jobs 里永远读不到 `SS_WEB_PASS`，原实现每次启动都发一条恒真的误报。
- CHANGELOG 有两个 `[Unreleased]`（其一为空），`make release` 直接拒绝。
- README：修正 `cf/config.yml` 路径、补目录与环境变量文件前置、回填前先构建镜像、
  本机调试改用容器内访问、公网启动命令补 `cloudflared`。
- `docs/strategy.md` 补三市场阈值差异（主板 B2 12% / 创业板、科创板 24%），
  `pool_low` 分级改为 P1；清理指向已删除文档的引用与「周日 20:00」这类不存在的任务描述。

### 修复

- **`config/llm.yaml` 默认端点指向一个第三方中转站**：新用户不改就会把自己的
  API key 发过去。改为 `https://api.example.com/v1` 占位，并注明可用
  `LLM_BASE_URL` 环境变量覆盖。该文件同时加入部署的保护清单——用户直接改 yaml
  配端点时，覆盖回去等于把 key 发到占位地址。
- **`set -o pipefail` 在 dash 下直接失败**：SSH 登录 shell 未必是 bash，
  `set -o pipefail` 报 `Illegal option` 并以 2 退出，整个部署中止。
  改为显式 `bash -c` 并正确加引号。
- **README 从零执行的顺序**：`cp` 到 `cf/config.yml` 之前要先 `mkdir cf`；
  写 `~/.config/stock-screener/*.env` 之前要先建目录（含 `state/`）。
- **`export UID=$(id -u)` 会报 `readonly variable`**（bash 里 UID 是只读的）。
- **RUNBOOK 的 westock 凭据路径**：compose 只把 `state/` 挂到 `/state`，
  而代码读 `/state/westock.env`；按原手册放在 `~/.config/stock-screener/` 根目录
  容器读不到。
- 测试 fixture 里的私网地址改用 RFC 5737 保留地址，
  避免暴露内网布局。
- README 明确写出「除权除息与停牌日历当前不可用」——客户端尚未实现
  `batch_dividend()` / `suspension()`，这与「没配账号」是两回事。

### 新增

- **公开化改造**：README 重写为通用项目介绍（安装、配置、数据源、部署）；
  设计文档精炼为 `docs/strategy.md`（策略与因子定义）与 `docs/architecture.md`
  （服务拓扑、数据流、关键设计决策）。内部开发过程记录不再随仓库分发。
- 占位域名检测：`public_host` 仍是 `example.com` 系时，`jobs` / `api` 启动即告警。
  这不是小事——同源校验是 `Origin == https://{public_host}`，占位值会让**所有写操作**
  被 400 拒绝，而健康检查、选池、推送全绿，用户只会看到「网页按钮点了没反应」。

### 修复

- **池子告警从未推送**：`jobs/after_close.py` 用
  `getattr(dispatcher, "pool_alerts", None)` 调用它，而 `Dispatcher` **没有这个方法**，
  于是每次出池只记一条 `WARNING ... 告警未推送`，`pool_low` / `pool_overflow`
  一次都没到过手机。`getattr` 兜底让出池本身不失败，缺陷因此长期静默。
  现补上 `pool_alerts`（`pool_low` → P1，`pool_overflow` → P2）。

### 变更

- `SS_WEB_USER` 默认值改为 `admin`（原为个人用户名）。
- `scripts/deploy.py` 的服务器地址与目录改为读 `SS_DEPLOY_HOST` / `SS_DEPLOY_DIR`。
- demo 脚本的 westock 路径改为读 `WESTOCK_BIN` 环境变量。

<!-- public-since: 0.2.25 -->

## [0.2.24] - 2026-11-13

### 安全

- **部署脚本第一版会删库**：初版 `rm -rf {dir}/*` 会抹掉 `data/market.db`
  （1.2 GB 生产库）、`data/backups/`（7 份日备份）、生成的 `cf/config.yml`
  和 `config/*.env`——这些都不在 git 里。改用 `tar -x` 的**覆盖语义**
  （归档里有的更新、没有的原样保留），并加两条守卫：脚本自检 + 测试扫描
  可执行代码里是否出现删目录命令（已变异验证）。

## [0.2.23] - 2026-11-13

### 新增

- **`scripts/deploy.py`：部署只发已提交的内容**。原先用
  `git ls-files -z | rsync --files-from=-`，它只决定**发哪些文件**、不决定
  **发什么内容**——发的是工作区当前内容。于是「改了已跟踪文件、引用了未跟踪的新文件」
  会完整漏过：`api/main.py`（已跟踪）带着 `from api import logs` 上去了，
  `api/logs.py`（未跟踪）没上去，容器启动即 `ImportError`。
  现在改为 `git archive HEAD` 导出（内容来自提交对象），并在入口拒绝脏工作区。
- 日志轮转写进 `deploy/docker-compose.yml`（YAML 锚点共用六个服务）。原先只配在
  VM 的 `/etc/docker/daemon.json` 里，那是机器状态、不进版本控制，换机器就丢。
- `api/logs.py`：过滤 `/healthz` 的访问日志（实测它占 api 日志的 99.6%）。

### 变更

- **`openai` 改为延迟导入**：它自身一条 `import` 要 10 秒（上百个 pydantic 模型）。
  `agent.main()` 启动路径会构造 `LlmClient`，这成本全算进容器启动——
  实测到写第一个心跳 21.9 秒，进程级冒烟测试只等 20 秒。
  连同「`mcp/westock.py` 只为两个名字却牵连 `talib`/`pandas`」一起拆出
  `llm/schemas.py` 后，实测降到 **11.3 秒**。异常判定同步改为鸭子类型
  （看 `status_code` 属性），不再 `isinstance` openai 的异常类。

### 修复

- **进程级冒烟测试的超时太紧**：PROBE 只等 20 秒（干净环境实际 11 秒），
  CI 机器一忙就假红。它恰恰是**唯一**能抓住「容器秒退 / 心跳没起」的测试，
  不能靠运气通过——现给到 90 秒（子进程 180 秒）。

## [0.2.22] - 2026-11-13

### 修复

- **`members_job` 把网络请求放在写锁里**：`refresh_sector_members` 是「逐板块
  抓取 + 逐板块写库」交替进行，整段都在 `loops.lock` 内。抓几十个板块要几分钟，
  于是每秒的 tick 和 30 秒的心跳全程被堵——实测 2026-11-13 08:50:44
  `heartbeat_job ... skipped: maximum number of running instances reached (1)`。
  现拆成 `fetch_sector_members`（只发请求、不碰库）+ `write_sector_members`
  （纯本地写入），`members_job` 先抓完再加锁写。新增 AST 守卫断言
  「网络抓取调用必须出现在取锁之前」，已变异验证。

## [0.2.21] - 2026-11-12

### 修复

- **除权/停牌周刷新从未运行**：`startup_recovery` 里有「键缺失或距上次 ≥7 天就补跑」
  的分支，但 `main()` 一直没把 `refresh_actions` / `refresh_suspensions` 传进去
  （参数默认 `None`）。生产库证据：`corporate_actions` 27 行**全部**是
  `snapshot_implied`（快照反推），`suspension_calendar` **0 行**，
  `refresh_actions.last` 从未写入。
  根因更深一层：它需要的 `westock.batch_dividend()` / `westock.suspension()`
  在 `mcp/westock.py::WestockMcp` 上**并不存在**——测试用的是 `FakeWestockCli`，
  那个 CLI 客户端从来没实现。所以先补「能力探测 + 接线」：能力齐备才接上，
  缺失则每次启动打一条 warning 指明缺什么、会导致哪张表永远是空的。
  **补齐真实取数（CLI 或 MCP 工具）仍未做**，不在本次范围内。
- **`startup_recovery` 的 `refresh_actions` / 除权刷新段也在锁外**：
  上一版只给 EOD 补跑加了 `loops.lock`，这一段（含它写的 `refresh_actions.last`
  marker）仍是裸写。现一并进锁，并由新守卫测试覆盖（按调用点逐个查）。

## [0.2.20] - 2026-11-12

### 修复

- **`startup_recovery` 起循环后无锁补跑**：它在第 3 步就
  `loops.start()`，6 个快照线程随即经 `_write_cache` 写共享连接；而随后的 EOD 补跑、
  `job_state` marker 全在**没有锁**的情况下写同一个连接。原注释误以为此时无并发写入，漏了这些线程。现补跑整段在 `loops.lock` 内。
- **`git add` 成功、`commit` 失败会残留暂存**：只还原工作区会留下
  「index 是新版本、工作区是旧版本」的 `MM` 状态，重跑被 dirty 检查挡住，
  且下次 `git commit -a` 可能误提交。现失败路径同时 `git reset -- <本次文件>`。
- **两处锁外的直接 SQL 写入**：`eod_job` / `members_job` 写
  `job_state` marker 用的是裸 `conn.execute(...)`，既不在写锁内、名字也不在守卫白名单里，
  于是完全漏检。现两处 marker 都移进写锁。

### 变更

- **通用守卫再收紧**：除按函数名识别写库调用外，现在也能识别**直接写 SQL**
  （`conn.execute("INSERT/UPDATE/DELETE/REPLACE ...")`）。已变异验证：
  把 marker 移回锁外、或用「在空语句上取锁」的假锁绕过，守卫都会变红。

## [0.2.19] - 2026-11-12

### 安全

- **限流的容量淘汰可被用来解除封锁**：超上限时按 `window_start`
  丢最旧，完全不看 `blocked` 是否仍在生效。攻击路径：先用 IP A 打满 10 次拿到封锁
  （它的窗口起点自然最早），再用 `MAX_TRACKED` 个其它 IP 各失败一次把 dict 撑爆 →
  A 作为「最旧」被淘汰 → 封锁立即失效。现容量淘汰**只考虑未在封锁中的条目**；
  若极端情况下全都在封锁中，宁可短暂超出上限（并记 warning），也不解除任何一条封锁。

### 修复

- **启动补跑可能被 APScheduler 静默丢弃**：`run_date` 在
  `make_scheduler()` 里就定成 `now+60s`，而 `startup_recovery()` 在它之后执行、
  再到 `sched.start()`——中间可能补跑最多 5 天 EOD（每只票都发请求），耗时数十分钟。
  等调度器真正启动时 `run_date` 早已过期，超过 `misfire_grace_time`(300s) 就被丢弃，
  且不报任何错。现在 `make_scheduler` 额外暴露 `arm_startup_renew()`，
  由 `main()` 在 `startup_recovery` 之后调用、用 `modify_job(next_run_time=...)`
  重算到真正启动时刻的 +60s。
  （注意 `modify_job` 只改 `next_run_time`，不改 `trigger.run_date`——测试据此断言。）

## [0.2.18] - 2026-11-12

### 修复

- **四处「锁外发告警」**：`dispatcher.alert()` 经 `_push_log` 写 `push_log`
  （`BEGIN IMMEDIATE`），但 `members_job` / `archive_job` / `backup_job` /
  `self_check_job` 都在写锁外调用它。同进程每秒的 tick 共用同一个 `conn`，
  交错时对方的 `with conn` 会把中间状态提前提交；而 `BEGIN IMMEDIATE`
  撞上另一线程未结束的延迟事务会直接报「cannot start a transaction within a transaction」。
  现四处告警都在 `_write_lock(loops)` 内。
  `backup_db` 本身**不**进锁：它用 `conn.backup(dest)` 读源库写目标文件，不写源库，
  且要拷 1GB+，占着写锁会把 tick 饿死。
- **通用守卫改成按调用点判定**（上一版按「函数里出现过锁」判定，于是上面四处全部漏检）：
  只要某处调用了写库函数却不在写锁 `with` 体内就报错。锁的三种等价写法都认
  （`_write_lock(...)` / `tick.lock` / `loops.lock`）。已变异验证：
  把 `backup_job` 或 `close_digest_job` 的锁去掉，守卫立刻变红并指出具体行号。

## [0.2.17] - 2026-11-12

### 修复

- **通用守卫测试本身有盲区**：它只认 `with _write_lock(...)`，
  而 `close_digest_job` 写的是 `with tick.lock:`——两者是同一把锁
  （`main()` 构造 `SnapshotLoops` 时传 `lock=tick.lock`）。于是把那个 `with` 删掉，
  守卫测试**依然全绿**。另外白名单漏了 `close_digest`（它经 `dispatcher._push_log`
  写 `push_log`，`BEGIN IMMEDIATE`），导致漏锁根本不被识别。
  现在锁写法认三种（`_write_lock(...)` / `tick.lock` / `loops.lock`），
  白名单补上 `close_digest`；删掉 `close_digest_job` 的锁后守卫会变红（已变异验证）。

## [0.2.16] - 2026-11-12

### 新增

- **节假日表过期告警**：`builtin_holidays` 是硬编码的（目前只覆盖到 2027-10-07），
  过期后每个工作日都会被当成交易日。这本身不致命——EOD 会被上游快照的日期闸挡成
  `skipped`，而 `skipped` 立即 `break`、不会反复刷请求——但**没人会注意到表已过期**。
  现在启动时若今日超出末条日期，会记一条 warning 指明要改哪里。

## [0.2.15] - 2026-11-12

### 新增

- **当天 EOD 补跑的节假日/周末排除测试**：判据改成 `is_trading_day()` 后，
  节假日靠 `builtin_holidays` 挡住、周末靠 `weekday()` 挡住，此前没有测试覆盖。
  写这两条时发现 `env` fixture 的日历是用 `_trading_days_rev` 造的——它只跳周末、
  **不认节假日**，把 2026-10-01 也当成了交易日。测试必须先清空这份预置日历，
  否则「节假日不补跑」这条会假绿。
  （另注：`builtin_holidays` 目前只覆盖 2026–2027；超出范围的工作日会被当成交易日，
  EOD 会去跑但被上游的日期闸挡住，只白白消耗请求。这是已知限制，不是本轮的改动点。）

## [0.2.14] - 2026-11-12

### 修复

- **三个写库任务没有持写锁**：`cleanup_job`（写 6 张表 + `PRAGMA optimize`）、
  `westock_renew_job`（写 `app_secret`）、`westock_import_job`（写 `watch_stock`）
  都直接拿共享 `conn` 写库，而同进程的 tick（每秒）也用同一个连接——
  线程交错时对方的 `with conn` 会把中间状态提前提交，多条 DELETE 失去原子性。
  续期尤其危险：启动补跑是 `DateTrigger(+60s)`，可能落在盘中任何时刻，
  不能按「08:40 不在盘中」推断安全。

### 新增

- **通用守卫测试**（`test_every_db_writing_job_holds_the_shared_write_lock`）：
  从 AST 枚举 `make_scheduler` 里所有 `*_job`，按调用图判断是否写库，
  要求各自持锁（或调用的助手内部持同一把锁）。单点测试防不住「以后新加任务忘加锁」，
  这条能。本轮就是靠它一次性找出上述三个漏锁任务。

## [0.2.13] - 2026-11-12

### 文档

- **更正 0.2.4 的性能声明**：该版本曾声称选池查询从 3570ms 降到约 1000ms（3.5 倍），该结论不成立。该数字来自手动 `ANALYZE` 过的临时副本；生产库本来就有
  `sqlite_stat1`，`PRAGMA optimize` 是空操作，查询计划与耗时都没变（复测 3.3–4.2 秒）。
  真实瓶颈是「扫 816 万行」——`date` 不在 `WITHOUT ROWID` 主键 `(code, date)` 的首列上，
  按日期过滤必然全表扫。限制到近 265 个交易日只降到约 3.0 秒，不值得为此改 `n_bars` 语义。
  `PRAGMA optimize` 保留（无害且是官方推荐），但**它解决的不是这个问题**。
  结论：这不是缺陷——该查询每天收盘后跑一次，3–5 秒不影响任何交易路径。

## [0.2.12] - 2026-11-12

### 修复

- **当天 EOD 补跑在真实场景下根本不触发**：判据写成了 `today in cal.days`，
  但交易日历来自 `sh000300` 的 bar，而今天那根 bar 正是 EOD 写进去的——
  「EOD 从没跑过」时日历必然不含今天，条件恰好在最需要它时不成立（鸡生蛋）。
  改用 `is_trading_day()`（对超出日历末日的日期按工作日 + 内置节假日判定）。
  原测试自行往日历里插入了当天，因此没有暴露该缺陷。
- **`api/auth.py` 的失败计数可被外部撑爆内存**：`_fails` 无上限，而过期清理只发生在
  「该 key 再次被访问」时——伪造 `CF-Connecting-IP` 每次换一个 IP、各失败一次，
  条目就永驻（100 万个 ≈ 100MB），且每次只失败一次连封锁都不触发。
  现加 `MAX_TRACKED=10000` 上限 + 每 60 秒批量清扫，仍在窗口/封锁内的条目保留。
- **健康标记会被 EOD 的写锁饿死**：`/tmp/jobs_healthy` 的 touch 原在 `heartbeat_job`
  里，跟着写锁一起阻塞。EOD（`run_eod` + `after_close`）持锁约 4 分钟，而 compose
  健康检查是 `find -mmin -2`、每 60 秒一次、连 3 次失败判 unhealthy——正好被打穿。
  现把 touch 拆成独立任务 `health_touch`，不碰数据库、不碰写锁。
- **发布脚本 commit 成功后打 tag 失败会留下错位状态**：回滚无条件把文件还原成旧版，
  于是 HEAD 是新版本、工作区是旧版本，重跑又被 dirty 检查挡住。现在 commit 既成
  事实就不再动文件，并提示手工补 `git tag`。

## [0.2.11] - 2026-11-12

### 修复

- **`after_close` 在写锁之外写库**：`eod_job` 只把 `run_eod` 包进了
  `with _write_lock(loops)`，紧接着的 `after_close`（选池，写 `pool` 表）却跑在锁外。
  实测 2026-11-12 15:08:32 心跳写库撞上它，`busy_timeout`（5 秒）耗尽后抛
  `database is locked`。EOD 事务持锁约 3 分钟、`after_close` 约 46 秒，
  而判活阈值 `STALE_S` 只有 120 秒——连续几个心跳周期失败会让 watchdog
  误判 jobs 已死（P0 误报）。现整条写路径与 `run_eod` 共用同一把写锁。
- **心跳遇锁即抛异常**：锁冲突时现在安静跳过并记一条 warning。心跳 30 秒一次、
  判活阈值 120 秒，跳过一次安全；抛给 apscheduler 只会每分钟刷 ERROR，
  把真正的异常淹没。

## [0.2.10] - 2026-11-12

### 修复

- **当天 EOD 错过就永远不补**：启动补跑的候选只有「已收盘的历史交易日」
  （`d < today`），当天的缺口不在候选里。cron 的 `misfire_grace_time` 只有 5 分钟，
  进程只要恰好在 15:05–15:10 之间不在（部署、重启、崩溃），当天就没有 bar，
  次日 EOD 的 `prev_day` 会直接跨过这一天。现收盘后（`close_time + 5min`）若当天
  marker 缺失则补跑；**收盘前不补**（bar 未定盘，判据复用 `eod_job` 同一套
  `close_time + CLOSE_BUFFER_MIN`）。

## [0.2.9] - 2026-11-12

### 变更

- `make test-web` 的 node 镜像改为可覆盖（`WEB_NODE_IMAGE`）：默认 `node:20-bookworm-slim`
  与 `deploy/Dockerfile` 的 web 阶段一致，某台机器没这个 tag 时
  `make test-web WEB_NODE_IMAGE=node:22-slim` 也能跑。

## [0.2.8] - 2026-11-12

### 安全

- **网页口令没有防暴力破解**：实测外网连打 10 次错误口令全是 401、毫无延迟。
  口令是人起的一串字，被慢慢穷举只是时间问题；一旦猜中，`/api/chat` 会花 LLM
  预算、`/api/hook/*` 能改状态。现按客户端 IP 限流：5 分钟内失败 10 次即封锁
  15 分钟并返回 429（带 `Retry-After`），成功登录清零。
  **必须认 `CF-Connecting-IP`**：请求经 cloudflared 进来，`request.client.host`
  永远是隧道容器地址，用它当键等于全球一个桶——攻击者打 10 次就能把主人锁在门外。

### 文档

- **补 RUNBOOK §7.5「从备份恢复数据库」**：此前手册里根本没有这一步，
  而它恰恰是事故时最需要的操作。写明必须停 jobs/api/agent、必须删
  `market.db-wal`/`-shm`（只换主库会让 SQLite 把旧 WAL 重放到新库上，
  `integrity_check` 仍可能报 ok）、恢复前先验完整性、损坏的旧库要留证不覆盖。
  流程已用真实备份（`market-20261111.db`）实测走通：816 万 bar、5583 只票、
  5 个迁移齐全。

## [0.2.7] - 2026-11-12

### 文档

- **RUNBOOK 的 shadow 说明会误导人**：原文写「shadow 期不会花钱、不会推送」。
  前半句对（零 LLM 花费），后半句只对通道 B（agent 主动开口）成立——
  通道 A 的确定性信号推送、通道 C 的运维告警在 shadow 期**照常发送**。
  实测 shadow 第 2 天有 11 条 `accepted` 推送，按原文会被误判成 shadow 没生效。
  现在补了一张三通道对照表，并写明正确判据是「回合 `decision` 全为 shadow + 零花费」。

## [0.2.6] - 2026-11-12

### 新增

- **续期决定全留日志**：这个模块原先一行日志都没有，只有 `app_secret` 里的标记，
  于是「为什么三天没续期」只能靠反推 SQL——而最需要回答它的时刻恰好是
  token 快过期、人很急的时候。现在 skip 会说明原因（剩余多少小时 / 当天已续过 /
  在退避窗口内），成功与两次失败也各有日志。

## [0.2.5] - 2026-11-12

### 新增

- **故障演练闸的测试**（`tests/notify/test_drill.py`）：这个模块此前完全没有测试，
  而它守的是「演练时不能真的推消息出去」。现在覆盖：开关只认字面 `1`
  （`true`/`yes`/`0` 都不算，误判会把生产推送静默吞掉）、每次调用重读环境变量、
  跳过时留日志，以及**三个外发出口都必须把闸用在判定里**（用 AST 检查，
  grep 字符串会被 `if False and drill_skip(...)` 骗过去）。

## [0.2.4] - 2026-11-12

### 修复

- **选池查询全表扫描 816 万行**：`daily_bars` 是 `WITHOUT ROWID` 表、从没跑过
  优化器统计，`WHERE date <= ? GROUP BY code` 只能 SCAN 整表（实测约 3.5–5 秒）。
  现每日清理时跑 `PRAGMA optimize` 刷新统计（SQLite 官方推荐的轻量做法）。

  **更正（同日复测）**：「降到约 1000ms、3.5 倍」的结论不成立。那个数字来自
  手动 `ANALYZE` 过的临时副本；生产库本来就有 `sqlite_stat1`（`daily_bars` 一行），
  `PRAGMA optimize` 属于空操作，查询计划与耗时都没变（复测 3.3–4.2 秒）。
  真正的瓶颈是「扫 816 万行」，不是统计缺失——`date` 不在 `WITHOUT ROWID` 主键
  `(code, date)` 的首列上，按日期过滤必然全表扫。把窗口限制到近 265 个交易日
  只能降到约 3.0 秒，收益不值得为此改变 `n_bars` 的语义。
  （另测：给这张表加二级索引反而更慢，41 秒。）
  **结论：这不是缺陷**——查询每天收盘后跑一次，3–5 秒不影响任何交易路径。

## [0.2.3] - 2026-11-12

### 修复

- **westock token 在周末过期无人续期**：续期任务只挂 `mon-fri` 的 08:40/08:46，
  而 token 到期时刻由腾讯给，不认交易日历。实测本轮 token 于 2026-11-15（周日）
  13:49 到期：周六周日两天都不跑，直到周一 08:40 才动，那时已过期 18.8 小时，
  期间除权事件与停牌日历的调用全部失败。现改为**每天**运行。
- **续期错过就丢一整天**：cron 的 `misfire_grace_time` 只有 5 分钟，
  08:40 那一刻进程不在（部署/重启/崩溃）这次就整个丢了，下一个机会是次日。
  现增加**启动后 60 秒的幂等补跑**（剩 ≥24h 或当天已成功都直接跳过，不会重复打腾讯）。

## [0.2.2] - 2026-11-12

### 修复

- **发布脚本把 lockfile 里全部 235 个依赖版本改成项目版本号**（0.2.1 引入）：
  正则替换所有 `"version"` 字段，`westock-data-clawhub` 从 1.0.4 变成 0.2.1，
  `npm ci` 报 "package.json and package-lock.json are not in sync"，
  Docker 镜像构建整条失败。现只改本包自己的两处版本（顶层与 `packages[""]`），
  并验证序列化结果与原文逐字节保真（缩进风格不同则拒绝执行）。
  lockfile 本体已从 v0.2.0 的完好版本恢复。
- **JSON 重写破坏格式**：发布脚本用 `json.dumps` 重写整份清单，
  4 空格缩进会被改成 2 空格。现加逐字节保真校验。

## [0.2.1] - 2026-11-12

### 修复

- **发布脚本 `APPLY=0` 仍会真发布**：make 把任何非空字符串当真值。
  现在只有 `APPLY=1` 才触发真实发布。
- **发布脚本报错时留下半改的工作区**：版本文件先被改、`[Unreleased]` 校验后做，
  失败后既没发成版、重跑又被脏工作区挡住。现改为「先全部校验 → 查 git 前置 →
  统一写入」，任何一步失败都恢复原状。
- **目标 tag 已存在时先 commit 后失败**：出现「commit 是新版本、tag 指向旧提交」
  的错位。现写盘前先预检 tag。
- **空版本照发**：`### 新增` 下零条目也能发布打 tag。现要求至少一个实际条目。
- **`v0.1.0` 回滚落点不存在**：RUNBOOK 的回滚命令指向未打 tag 的版本。
  `v0.1.0` 现已追溯打在 `de56875`（2026-11-10 最后一个提交）上。
- **预发布版本无法晋级**：`0.3.0-rc1 → 0.3.0` 被拒（只比较前三段）。
  现按 SemVer precedence 比较。
- **lockfile 版本不跟随**：`package-lock.json` / `web/package-lock.json`
  留在旧版本，`npm install` 会改回来产生噪声 diff。现随发布一起更新并纳入守卫。
- **JSON 重写破坏格式**：发布脚本用 `json.dumps` 重写整份清单，
  4 空格缩进会被改成 2 空格。现只替换 version 字段的值。
- **跨零点发版日期写错**：发布日期取本机时区。现固定 Asia/Shanghai。

### 变更

- `make check-version` 不再依赖 Docker build（纯文本检查不应被 Docker Hub
  连通性绑架）；`make release` 只有 `APPLY=1` 才真执行。

## [0.2.0] - 2026-11-11
### 新增

- **版本号体系**：`core/version.py` 是唯一来源，`/healthz` 与 `/api/status`
  现在会返回 `version`——排障时能直接回答「线上跑的是哪一版」，不必靠镜像 tag 猜。
- **CHANGELOG 与发布流程**：本文件 + `make release VERSION=x.y.z`
  （校验 → 改四处版本号 → 收口 changelog → 提交 → 打 tag），
  版本漂移由 `tests/test_version.py` 守卫。

### 修复

- **停牌判据在收盘集合竞价误报**：14:57–15:00 上游不逐笔推送，时间戳静止被当成停牌。
  现按 `config/markets.yaml` 的 `session.close_time` 判定，14:57 起不再判停牌。
  同时修掉开盘那半个问题：非交易时段的静止会累积成计时基准，导致开盘/午后第一笔误判。
- **LLM 自检把 401 当成健康**：探测请求原本不带密钥，且判据是「状态码 < 500」，
  密钥过期时自检照样报绿——最该告警的情况恰好不告警。现带 `Authorization`、
  只认 2xx/3xx，且 URL 取环境变量优先（否则 `LLM_BASE_URL` 故障演练会让自检说谎）。
- **`templates._seq_id` 缺 `kind` 段**：与 `dispatcher._seq_id` 不同口径，
  同一只票同日的突破与跌破会撞成同一个 ntfy 序列号，第二条被当成「更新」吞掉。
- **重启后价格类冷却清零**：检测器只在重启时恢复指数/板块信号的冷却基准，
  价格类没恢复，重启后同一只票立刻重报一条。
- **开盘简报正文无总长度上限**：竞价条数封顶后，Top N 明细仍在预算之外追加，
  实测 Top 100 + 长股票名 = 5886 字节，超过 ntfy 消息上限会被整条拒收。

---

## [0.1.0] - 2026-11-10

影子期之前的基线版本。

> **公开仓库里没有 `v0.1.0` 这个 tag，也没有 `de56875` 这个提交。**
> 公开版的历史是重建过的（见文件末尾 `public-since` 说明），本节及更早的
> 小节属于私有期沿革，只作背景记录。**可用的回滚落点 = `git tag --list`
> 里最早的那个 `v0.2.x`**，RUNBOOK 的回滚章节按这个口径写。

### 新增

- **盘后选池**：按策略从全市场筛选观察池（主板/创业板/科创板三档）。
- **盘中事件检测**：箱体突破/破位、量能倍数、滞涨、涨停封板、停牌、除权，
  全部为确定性规则判据，可复现。
- **推送与抑制**：P0/P1/P2 分级；同票同类型 30 分钟去重；日熔断按序列号计数；
  P1 合并、板块合并、溢出摘要三种合并推送。
- **agent 影子期**：只记录不升级，零模型调用；`llm_budget` 预扣不退，
  真实用量只写 `token_audit`，从不回写预算。
- **网页口令**：HTTP Basic（`SS_WEB_PASS`），`/healthz` 与 `/auth` 免鉴权。
- **运维**：`preflight` 体检、08:55 自检、watchdog、日志按天滚动。
- **westock 扫码续期**：静默续期优先，失败才把扫码链接推到手机。

### 变更

- 网页访问采用应用层口令，不用 Cloudflare Access——后者会挡住手机 ntfy 客户端。

### 修复

- **开盘简报整天丢失**：448 条竞价异常全塞进正文（468 行 / 11134 字节），
  超过 ntfy 消息上限被 400 拒收，且该推送无重试。现竞价异常最多列 10 条。
- **同一只票刷屏**：价格信号只要条件消失一个 tick 就重新武装，
  实测某只票 8 分钟产生 19 条突破信号。现加 30 分钟冷却期。
- **批次内同票重复**：一条合并推送里同一只票出现 9 次，标题写「11 只同向异动」
  实际只有 4 只。现按 `(代码, 类型)` 去重。
- **集合竞价误判停牌**：判据「快照时间戳连续 60 秒不变」在 09:15–09:25 恒成立，
  实测开盘 10 分钟内误报 116 条停牌（这些票当天都有大量突破，明明在正常交易）。
  现只在连续竞价时段判定。
- **推送序列号用了池子日期**：池子是 T−1 数据，当天的信号被记成昨天的；
  池子某天没重建时连续两天还会撞同一个号。现用事件所属交易日。
- **缺口只在内存里交接**：EOD 补拉失败的票记在内存计数器里，进程重启就丢。
  现落 `gap_pending` 表，由 backfill 接力。
- **配额分支吞掉当天 K 线**：补拉超限时 `continue` 跳过了当天 bar 的写入。
- **LLM 自检每天误报**：读的是从不存在的 `config/llm.env`，导致每天 08:55
  误报 P1「自检失败: llm」，把真故障淹掉。
- **体检脚本写死变量名**：`preflight` 要求 `LLM_API_KEY`，生产用的是
  `DEEPSEEK_API_KEY`，自检永久失败。
- **westock 定时续期每天失败**：扫码缓存放在宿主机配置目录，容器内看不到。
- **日志不可用**：httpx 把每次请求的完整 URL（含几百个股票代码）按 INFO 打出来，
  另有 APScheduler 每秒一条，`docker logs` 基本无法阅读。
