# A 股跌深横盘突破盯盘 agent

一套自托管的 A 股盯盘系统：每天盘后从全市场筛出「**跌深 → 横盘缩量 → 放量突破**」形态的候选池，盘中实时检测突破并通过手机推送提醒，同时有一个 LLM agent 在事件发生时做上下文判断。

不是荐股软件，不是自动交易。它做的是**把符合特定形态的票筛出来并告诉你为什么**，决策仍然是你自己的。

> ### ⚠️ 部署前必读
>
> 1. **必须设网页口令**：不设 `SS_WEB_PASS` 时**所有接口都不鉴权**。本项目的默认
>    部署方式（Cloudflare Tunnel + 域名）是**公网可达**的——不设口令等于把状态、
>    观察池、聊天接口全部对外开放，而 `/api/chat` 每调一次都会**花你自己的 LLM 预算**。
>    详见「快速开始」里的 `web.env`。
> 2. **数据源是非官方接口**：A 股行情接口没有官方授权，随时可能变更或封禁，
>    使用前请自行判断合规性。见文末「免责」。
> 3. **westock 相关脚本需要你自己的账号**：扫码登录态只代表你本人，请勿用于
>    批量/多账号抓取，本项目不提供账号池。

---

## 它做什么

- **盘后选池**：全市场约 5500 只票，按 A/B 两组因子打分排序，取 Top N 进观察池，每只带分项因子值
- **盘中检测**：09:15–15:05 每秒 tick，检测已入池股票的放量突破，写事件、发推送
- **集合竞价**：09:25:30 抓一次竞价数据，识别高开/低开异动
- **LLM 盯盘**（可选）：事件触发时由模型结合上下文判断是否值得打扰你，产出可读的理由
- **网页**：观察池、信号列表、事件流、agent 工作日志、聊天
- **推送**：自建 ntfy，手机 App 订阅，支持去重、合并、每日上限、P0/P1/P2 分级

## 它不做什么

| 不做 | 原因 |
|---|---|
| 自动下单 | 不接任何交易接口 |
| 盘后检测突破 | 突破是分时行为，盘后看收盘价已经错过买点 |
| 荐股 / 预测涨跌 | 只做形态筛选和事实呈现 |
| 回测收益 | 没有历史成交回放引擎，不要拿它当策略验证工具 |

---

## 快速开始

### 前置

- Docker + Docker Compose
- 一个 A 股行情数据源（见下文「数据源」）
- 可选：一个域名（用于手机远程访问与推送）

### 1. 建目录与配置文件

compose 会在**解析阶段**校验 `env_file` 是否存在，缺文件连 `up` 都起不来；
容器以 `${UID:-1000}:${GID:-1000}` 运行，bind mount 的目录必须同属主可写。

```bash
git clone <仓库地址> && cd stock-screener

# 数据、缓存、隧道配置目录（compose 的 bind mount 目标）
mkdir -p data/logs ntfy/cache ntfy/auth cf

# 环境变量文件与 westock 登录态：内容可以先留空，但**文件必须存在**
# （这两个 .json 会被 westock 扫码脚本填入登录态；空文件即可启动，
#   只是 westock 续期会要求重新扫码）
mkdir -p ~/.config/stock-screener/state
touch ~/.config/stock-screener/llm.env \
      ~/.config/stock-screener/notify.env \
      ~/.config/stock-screener/web.env \
      ~/.config/stock-screener/state/westock.env \
      ~/.config/stock-screener/wb_client.json \
      ~/.config/stock-screener/wb_session.json
chmod 600 ~/.config/stock-screener/*.env \
          ~/.config/stock-screener/*.json \
          ~/.config/stock-screener/state/*.env
```

**这几个文件少建任何一个，docker 都会在宿主机上建一个同名空目录顶替**
（单文件 bind mount 的行为），之后容器里读写它直接 `EISDIR`；而且那个目录
通常归 root，想补建得先 `sudo rm -rf`。报错只出现在容器日志里，不好定位。

`UID`/`GID` 在 bash 里是**只读变量**，不能 `export UID=$(id -u)`（会报
`UID: readonly variable`）。它们本来就已存在，直接传给 make 即可：

```bash
make test UID=$(id -u) GID=$(id -g)      # 或在 compose 调用时内联
```

### 1.2 配置

```bash
cp deploy/cf/config.yml.example cf/config.yml             # 域名与隧道（仅公网部署需要）
# config/notify.yaml 已带 example.com 占位，改成你的域名即可
```

需要填的几处：

| 文件 | 改什么 |
|---|---|
| `config/notify.yaml` | `public_host` 换成你的域名（或本地调试时留 `example.com`） |
| `deploy/ntfy/server.yml` | `base-url` 与上面同域 |
| `cf/config.yml` | 隧道 UUID + 你的域名 |
| `~/.config/stock-screener/llm.env` | `LLM_BASE_URL` / `LLM_API_KEY`（不配则 agent 不工作，其余功能正常） |
| `~/.config/stock-screener/web.env` | `SS_WEB_USER` / `SS_WEB_PASS`（不配则网页不鉴权，**公网部署必须配**） |

**只在本机调试**可以跳过域名与隧道。注意 `api` 端口**不发布到宿主机**（唯一入口是隧道），
所以要在容器内访问：

```bash
docker compose -f deploy/docker-compose.yml --project-directory . exec api \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5000/healthz').read().decode())"
```

或自己写一个 override 文件加 `ports: ["5000:5000"]`。

### 2. 首次灌历史数据

选池需要至少 265 个交易日的日线。`backfill` 复用生产镜像但不带 `build`，
所以**必须先构建镜像**，否则会去拉一个不存在的同名镜像：

```bash
docker compose -f deploy/docker-compose.yml --project-directory . build api
docker compose -f deploy/docker-compose.yml --project-directory . \
  --profile tools run --rm backfill python -m jobs.backfill --years 3
```

回填是**限速**的（每请求间隔 300ms），3 年全市场大约需要数小时。可以先 `--limit 500` 试跑。

### 3. 启动

```bash
# 公网部署（含隧道）
docker compose -f deploy/docker-compose.yml --project-directory . up -d api jobs agent ntfy cloudflared
curl -s https://stock.example.com/healthz     # {"ok":true,"version":"x.y.z"}

# 仅本机（不要隧道）
docker compose -f deploy/docker-compose.yml --project-directory . up -d api jobs agent ntfy
```

`jobs`、`agent`、`api` 都**不发布端口**。

---

## 数据源

日线与实时行情来自公开接口（腾讯 / 新浪）。这两者是**非官方接口**，随时可能变更或限速，因此：

- 所有请求经统一限速器，且只有 `jobs` 一个进程碰上游
- 每个 scope 的调用都记进 `upstream_calls`，可查用量
- 主源失败自动降级到备源，两个都失败发 P0 告警

**自选股**数据依赖 westock MCP（个人账号登录态，工具包 `westock-data-clawhub`）。
凭据放在 `~/.config/stock-screener/state/westock.env`（见 RUNBOOK §1）。没有账号时
自选股为空，其余功能不受影响。

> **除权除息与停牌日历需要 westock 账号**：两者来自 MCP 的 `data_dividend` /
> `data_suspension`，没有账号时对应刷新回调为 `None`，启动时会打印一条 WARNING，
> 此时 `corporate_actions` 只会有快照反推的 `snapshot_implied` 行、
> `suspension_calendar` 保持空表——**这两张表此时不要用来做判断**。
> 有账号时刷新在启动补跑（距上次 ≥7 天触发），不是周期任务。

### 其他已知限制

- **交易日历内置到 2027 年**：`core/clock.py` 的 `builtin_holidays` 覆盖 2026/2027。
  超出范围后会退化成「工作日即交易日」，元旦/春节会被当成交易日。跨年前要补表。
- **上游接口随时可能变**：解析逻辑没有版本协商，对方改字段就会直接失败（会发 P0）。
- **单实例设计**：共享同一个 SQLite 与同一份上游限速预算，不支持多副本部署。

---

## 部署

单机 Docker。生产建议：

```
你的服务器 ── cloudflared ── Cloudflare ── 你的域名
```

`api` 端口**不发布到宿主机**，唯一入口是隧道。这样服务器在公网也没有裸露端口。

```bash
# 同步代码并重建（只发已提交的内容，工作区必须干净）
SS_DEPLOY_HOST=your-server SS_DEPLOY_DIR=~/stock-screener python3 scripts/deploy.py

# 发布新版本
make release VERSION=0.3.0         # 干跑，先看要改什么
make release VERSION=0.3.0 APPLY=1 # 真执行
```

运维手册见 [`deploy/RUNBOOK.md`](deploy/RUNBOOK.md)——里面有备份恢复、演练、故障处置的具体命令。

---

## 目录

| 路径 | 内容 |
|---|---|
| [`docs/strategy.md`](docs/strategy.md) | 策略与因子的完整定义 |
| [`docs/architecture.md`](docs/architecture.md) | 服务拓扑、数据流、关键设计决策 |
| [`deploy/RUNBOOK.md`](deploy/RUNBOOK.md) | 运维手册 |
| `config/` | 策略参数、推送配置、交易日历兜底 |
| `core/` | 时钟、DB、配置、HTTP、复权、因子与选池 |
| `jobs/` | 调度器与盘后批处理 |
| `agent/` | LLM 回合 |
| `web/` | React 前端 |

---

## 开发

```bash
make test            # 后端全量测试（含 TA-Lib，与生产同环境）
make test-web        # 前端测试
make check-version   # 版本号与 tag/CHANGELOG 一致性
```

测试必须跑在镜像里（生产用 3.12 + TA-Lib），不要在宿主机直接 `pytest`。

### 几条这个项目里被反复验证过的规矩

1. **测试通过 ≠ 改对了**。这条项目里出过多次「有变异验证、全绿、但修的是错的」。任何修复都要问：*如果我的判断本身就是错的，哪条测试会红？*
2. **判据不能读自己写的表**。用今天的 bar 判断「今天该不该跑 EOD」，会陷入鸡生蛋。
3. **网络 I/O 不能放锁内**。锁是用来串行化本地写库的，不是用来保护外部请求的。
4. **「跑一次就失效」的任务要回答：进程那一刻不在会怎样？**
5. **生成文件的修改要 parse → 改字段 → 序列化 → 字节校验**，不要正则替换整个文件。

---

## 免责

本项目仅供个人学习与技术研究。A 股数据接口无官方授权，使用者需自行判断合规性。任何基于本项目产生的投资决策与后果由使用者自负。

**关于 westock / 腾讯自选股**：相关脚本（`scripts/wb_*.mjs`）依赖**你本人**的微信
扫码登录态，仅用于自动化你自己的账号。请勿用于批量注册、多账号轮换、账号池或
对外提供数据服务。本仓库的 MIT 许可只覆盖仓库内代码，不覆盖任何第三方服务。
