# RUNBOOK（第 1–6 节：公网接入）

> **所有 `docker compose` 命令都必须带 `-f deploy/docker-compose.yml --project-directory .` 这两个参数**——
> 仓库根没有 compose 文件；不带会报 `no configuration file provided`，或把 `./data ./config ./cf`
> 按 `deploy/` 解析成 `deploy/data` 等错误路径。下文统一简写为 `DC`：
>
> ```bash
> DC="docker compose -f deploy/docker-compose.yml --project-directory ."
> ```

域名：下文 `<域名>` 换成你自己的域名（例 `example.com`）。三处必须一起改，别只改一处：

| 位置 | 键 |
|---|---|
| `config/notify.yaml` | `public_host`、`ntfy_url`、`topic` |
| `deploy/ntfy/server.yml` | `base-url` |
| `cf/config.yml` | 两个 `hostname` |

**验证顺序**：第 1–5 节是配置，第 6 节才 build/启动。第 2–4 节的公网检查**留到第 6 节统一做**——
在隧道跑起来之前访问公网域名只会得到 Cloudflare 1033/530，那不是配置错误。

---

## 1. 同步仓库与 env 文件

在 VM（`<你的服务器>`）上：

```bash
mkdir -p ~/stock-screener && cd ~/stock-screener      # 仓库根
mkdir -p ~/.config/stock-screener && chmod 700 ~/.config/stock-screener
mkdir -p ntfy/cache ntfy/auth data/logs cf
```

### 三份 env 文件

**compose 解析项目时就会检查 `env_file` 是否存在**——哪怕只 `up -d ntfy`。
所以第 1 节先把三个文件建出来（值可后填），否则第 4 节第一条命令就会以
`env file .../llm.env not found` 失败，且报错指向 api 而不是 ntfy。

```bash
mkdir -p ~/.config/stock-screener/state
touch ~/.config/stock-screener/{llm,notify,web}.env \
      ~/.config/stock-screener/state/westock.env \
      ~/.config/stock-screener/wb_client.json \
      ~/.config/stock-screener/wb_session.json
chmod 600 ~/.config/stock-screener/*.env \
          ~/.config/stock-screener/*.json \
          ~/.config/stock-screener/state/*.env
```

**`wb_client.json` / `wb_session.json` 必须存在**：compose 单文件 bind mount
在这两个路径上，文件不存在时 docker 会在**宿主机**建一个同名空目录顶替，
容器内读它直接 `EISDIR`。这个目录通常归 root，补建前要先 `sudo rm -rf`。

**`westock.env` 必须放在 `state/` 子目录**：compose 只把 `state/` 挂到容器内
`/state`，而 `jobs/main.py` 从 `/state/westock.env` 读取。放在
`~/.config/stock-screener/` 根目录容器是读不到的。

| 文件 | 键 | 何时填 |
|---|---|---|
| `llm.env` | `DEEPSEEK_API_KEY` | 现在填 |
| `notify.env` | `NTFY_TOKEN` | **只有这一个**；第 4 节生成后填 |
| `state/westock.env` | `WESTOCK_MCP_URL`、`WESTOCK_ACCESS_TOKEN` | 现在填 |

`public_host` / `ntfy_url` / `topic` 是**非密钥配置，唯一来源是 `config/notify.yaml`**，不要往 env 里再复制一份。

验证：

```bash
ls -l ~/.config/stock-screener/*.env     # 期望：三个文件都是 -rw-------（600）
ls -d ntfy/cache ntfy/auth data/logs cf  # 期望：四个目录都存在
```

---

## 2. cloudflared 授权与 DNS

**这一步在你自己的电脑上做，不是在 VM 上**（VM 按项目约束只跑 Docker，没有 cloudflared 二进制）。

```bash
# 你自己的电脑
cloudflared tunnel login                     # 浏览器里选域名并授权
cloudflared tunnel create stock-screener     # 记下 TUNNEL-UUID
cloudflared tunnel route dns stock-screener stock.<域名>
cloudflared tunnel route dns stock-screener ntfy.<域名>
```

把凭据 JSON 拷到 VM 的 `~/stock-screener/cf/<TUNNEL-UUID>.json`，然后：

```bash
cd ~/stock-screener
cp deploy/cf/config.yml.example cf/config.yml
# 编辑 cf/config.yml：替换 <TUNNEL-UUID> 与两个 hostname；
# 最后一条 `- service: http_status:404` 是兜底，不要删。
```

验证（在**你自己的电脑**上）：

```bash
cloudflared tunnel list       # 期望：stock-screener 一行为 RUNNING（该列叫 CONNECTIONS，值为节点数）
```

VM 侧要到第 6 节 `up` 之后再验：`$DC ps cloudflared` 显示 running，`$DC logs cloudflared` 无反复重连。

---

## 3. 网页访问保护

**当前采用应用内置口令（HTTP Basic），不依赖 Cloudflare Access。**

理由：Access 需要建 API Token 或手工点面板，且**会拦住手机 ntfy App**
（App 走 `https://ntfy.<域名>`，无法完成浏览器登录）。内置口令零外部依赖、
手机浏览器原生记住，够一个人用。

- 口令来自 VM 上 `~/.config/stock-screener/web.env`（`SS_WEB_USER` / `SS_WEB_PASS`，mode 600，不进 Git）
- `deploy/docker-compose.yml` 只把它挂给 **api** 服务（agent/jobs 不对外）
- **`/healthz` 与 `/auth` 免鉴权**：healthcheck 不带凭据，挡了会被判 unhealthy 并无限重启
- 未配 `SS_WEB_PASS` 时保持开放（本机开发 / 纯内网），不会把自己锁在门外
- **推送链接免密**：通知里的每个链接都带签名票据 `?t=..&exp=..`，点开即进，
  不再弹原生登录框；首次进入后拿 `HttpOnly` 会话 Cookie，之后刷新也不再问。
  裸敲域名（无票据无 Cookie）仍然要口令。
  票据有效期 30 天（`SS_SESSION_DAYS` 可改，**同时作用于 Cookie 与推送链接**）。
  **本地 http 调试时 Secure Cookie 不会持久化**：浏览器与 httpx 都不回传，
  每次请求仍要 Basic。这是 `Secure` 属性的应有行为，别为了本地方便去掉它。**要让所有在飞链接立刻失效**：
  删掉 `sqlite3 data/market.db "DELETE FROM app_secret WHERE name='hmac_key'"`
  再重启 **api / jobs / agent 三个服务**（`docker compose restart api jobs agent`）。

  **只重启 api 是不够的**：`Dispatcher` 在构造时就把密钥缓存进实例，而 jobs 与
  agent 各自长期持有一个 `Dispatcher`（`jobs/main.py`、`agent/main.py`）。只重启 api
  的话，api 用新密钥验签，而 jobs/agent 仍用**旧密钥**渲染新通知——那些通知点开
  一律验签失败，直到签名方重新加载密钥。
  访问日志里的 `t`/`exp` 会自动打码成 `***`，不会把凭据写进 `docker logs`。
- 口令比较用 `hmac.compare_digest`（常量时间），避免前缀时序泄露

验证（三条都要对）：

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://stock.<域名>/api/status          # 401
curl -s -o /dev/null -w "%{http_code}\n" -u user:pass https://stock.<域名>/api/status  # 200
curl -s -o /dev/null -w "%{http_code}\n" https://stock.<域名>/healthz             # 200
```

### 附：如果以后确实要上 Cloudflare Access

Zero Trust → Access → Applications 新建 **Self-hosted** 应用：

- 域名：`stock.<域名>`
- Policy：`Allow`，Include → Emails → 你的邮箱
- **Session Duration = 1 个月**（默认 24 小时）
- 认证方式选 **One-time PIN**
- 再加一条 **Bypass** 策略，路径 **`/api/hook/*`**
  （通知按钮是 ntfy App 直接 POST，不带浏览器会话；`/api/hook/*` 下每条 URL 都带 HMAC `?t=`，
  且只放「一条 URL 一个动作」的接口）

**ntfy 子域名不套 Access**：它有自己的 auth（deny-all + token）。

### `/auth` 的访问控制（Task 24 落地，规则先定死）

- **生产只允许「一次性随机 token + TTL ≤ 5 分钟」**：登录/扫码链接由 VM 内命令生成，用后即焚。
- VM 上通过 SSH 隧道做本机调试：`ssh -L 5000:127.0.0.1:5000 <vm>`，然后访问 `http://127.0.0.1:5000/auth`。
- **坑**：经 cloudflared 隧道进来的请求，`request.client.host` 是**隧道容器 IP**，不是 `127.0.0.1`——
  所以「127.0.0.1 白名单」在生产会静默失效，**绝不能依赖它**，生产必须走一次性 token。

验证：留到第 6 节（浏览器访问 `https://stock.<域名>/healthz`，**应当直接返回 JSON**）。

**注意**：当前默认方案是应用内置 Basic，`/healthz` 在免鉴权清单里，浏览器不会弹任何
登录框；出现 Access 邮箱验证只在你**额外启用**了第 3 节末尾那个可选方案时才成立。

---

## 4. ntfy 用户与 token

```bash
cd ~/stock-screener
$DC up -d ntfy
$DC exec ntfy ntfy user add --role=admin <你的用户名>
# deny-all 下默认 role 没有主题权限，带 token 也会 403 —— --role=admin 不能省
$DC exec ntfy ntfy token add --label stock-screener <你的用户名>    # 输出 tk_xxx
```

把 `tk_xxx` 填进 `~/.config/stock-screener/notify.env` 的 `NTFY_TOKEN=`，并**在本机 shell 里导出**，
后面的验证与 `soak.sh` 都读这个变量：

```bash
chmod 600 ~/.config/stock-screener/notify.env
export NTFY_TOKEN=tk_xxx          # 每条新 shell 都要重新 export（或写进 ~/.bashrc）
$DC restart ntfy
```

验证（容器内本地检查，不经过公网）：

```bash
$DC exec ntfy ntfy user list      # 期望：<你的用户名> 一行，role=admin
$DC ps ntfy                       # 期望：running
```

公网的 200/403/health 检查留到第 6 节。

---

## 5. 手机 App 与 MIUI

- 安装 **ntfy** 安卓 App（**≥ 1.25.1**）
- 订阅 `https://ntfy.<域名>` 的 `<topic>`，填 token
- MIUI 五项设置（缺一项都会在锁屏后收不到）：
  1. 忽略电池优化
  2. 允许自启动
  3. 后台无限制
  4. 省电策略 → 无限制
  5. 最近任务里加锁

验证：留到第 6 节的「Wi-Fi 与 4G 各收一条」。

---

## 6. 启动与验证

```bash
cd ~/stock-screener
$DC build            # 首次必须显式 build：prod 镜像与 web/dist 都在这一步生成
$DC up -d
$DC ps               # 期望：api healthy；ntfy / cloudflared running（本任务未给它们配 healthcheck）
```

### 公网入口

```bash
curl -s https://ntfy.<域名>/v1/health        # 期望：{"healthy":true}
```

```bash
# 不带 token 发布 → 403
curl -s -o /dev/null -w '%{http_code}\n' -d hi https://ntfy.<域名>/<topic>
# 带 token 发布 → 200（NTFY_TOKEN 必须已 export，见第 4 节）
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $NTFY_TOKEN" -d hi https://ntfy.<域名>/<topic>
```

```bash
# WebSocket 订阅 → 101（必须带升级头，否则 ntfy 回 400 "client not using the websocket protocol"）
curl -si -N --http1.1 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
  -H "Authorization: Bearer $NTFY_TOKEN" \
  "https://ntfy.<域名>/<topic>/ws" | head -1
# 期望：HTTP/1.1 101 Switching Protocols
```

`https://stock.<域名>/healthz`：**默认方案下直接 curl 就行**——它在免鉴权清单里，
不弹口令、也不要 Access。期望 200、正文 `{"ok":true,"version":"x.y.z"}`。
（**带 `version`**：这是确认「线上跑的是哪一版」的唯一权威出口。）

```bash
curl -s https://stock.<域名>/healthz    # {"ok":true,"version":"x.y.z"}
```

**只有在你额外启用了 Cloudflare Access 那个可选方案时**，才需要走浏览器登录或：

```bash
cloudflared access curl https://stock.<域名>/healthz    # Access 登录后输出同样内容
```

### 跨容器连通性

容器内 healthcheck 打的是 `127.0.0.1`，uvicorn 若没绑 `0.0.0.0` 它照样绿而公网全断。
从同一 compose 网络发起一次真实请求：

```bash
docker run --rm --network stock-screener_default python:3.12-slim \
  python -c "import urllib.request; print(urllib.request.urlopen('http://api:5000/healthz', timeout=3).status)"
# 期望：200
```

（`stock-screener_default` 是 compose 项目 `stock-screener` 的默认网络名。
cloudflared 镜像里没有 python，不要用 `exec cloudflared python`。）

### 手机

Wi-Fi 与 4G 下各发一条测试推送，手机都收到。

### 开始 3 天挂机

```bash
cd ~/stock-screener
NTFY_URL=https://ntfy.<域名> NTFY_TOKEN=$NTFY_TOKEN TOPIC=<topic> ./scripts/accept/soak.sh
```

**期望**：3 天后 App 里 **71/71** 条；这期间你随手记下的几次「手机响了」与消息时间相差 **< 1 分钟**。

---

## 7. 回填（D18：与 jobs 互斥）

> **硬性条款：执行 `backfill` 前必须 `docker compose stop jobs`，完成后 `start`。**
> backfill 与 jobs 共用同一上游限速出口（§6.1.2.2 的预算表按单进程算），
> 同时跑会把预算翻倍、触发上游风控，403 后反而更慢。

```bash
# **不要 `export UID GID`**：bash 里 UID 是只读变量，`export UID` 直接报
# `UID: readonly variable`。它们本来就已在环境里，compose 能读到；
# 要显式传就在命令行给：`make backfill UID=$(id -u) GID=$(id -g)`
cd ~/stock-screener

# 1) 停 jobs（不只是停调度——快照三循环也在 jobs 里）
docker compose -f deploy/docker-compose.yml --project-directory . stop jobs

# 2) 小批量先验证数据源与落库
docker compose -f deploy/docker-compose.yml --project-directory . --profile tools \
  run --rm backfill python -m jobs.backfill --limit 300
# 验证：docker compose -f deploy/docker-compose.yml --project-directory . exec jobs python - <<'PY'
#   import sqlite3; c=sqlite3.connect('/app/data/market.db')
#   print(c.execute("select count(*) from daily_bars").fetchone()[0], "bars")
# PY
# 期望：> 0 且 fetch_log 里 backfill 行 status=ok

# 3) 全量回填（约 40–60 分钟）
docker compose -f deploy/docker-compose.yml --project-directory . --profile tools \
  run --rm backfill python -m jobs.backfill

# 4) 恢复 jobs
docker compose -f deploy/docker-compose.yml --project-directory . start jobs
```

## 7.5 从备份恢复数据库

备份由 `cleanup` 之后的 `backup` 任务每天 23:30 写 `data/backups/market-YYYYMMDD.db`
（`sqlite3.backup` 在线快照 + `integrity_check`，保留 7 份）。

**恢复是唯一必须停服务的数据操作**——SQLite 文件被换成旧版时，若 jobs/api 还开着，
它们要么继续写旧句柄（改动丢失），要么在 WAL 上读到混搭状态。

```bash
cd ~/stock-screener

# 1) 先看清楚要恢复到哪一天（没有这一步就别动手）
ls -la data/backups/

# 2) 停全部写者：jobs/api/agent 都会写 market.db
docker compose -f deploy/docker-compose.yml --project-directory . stop jobs api agent

# 3) 把当前库挪开留证据（**不要直接覆盖**：覆盖后无法回答"坏成什么样"）
mv data/market.db data/market.db.broken-$(date +%Y%m%d%H%M)
rm -f data/market.db-wal data/market.db-shm      # 留着会让 SQLite 把旧 WAL 重放到新库上

# 4) 拷回备份
cp data/backups/market-YYYYMMDD.db data/market.db

# 5) 恢复前先验一次（不验就起服务，等于把事故变成两次）
docker run --rm -v "$PWD/data:/d" python:3.12-slim-bookworm \
  python -c "import sqlite3; c=sqlite3.connect('/d/market.db'); print(c.execute('PRAGMA integrity_check').fetchone()[0]); print('bars', c.execute('select count(*) from daily_bars').fetchone()[0])"
# 期望：ok 且 bars > 0

# 6) 起服务
docker compose -f deploy/docker-compose.yml --project-directory . start jobs api agent

# 7) 验证真的回来了
curl -s https://stock.example.com/healthz          # version 正确
docker logs --tail 30 stock-screener-jobs-1       # 无 traceback、心跳恢复
```

**三个坑**：

1. **必须删 `market.db-wal` 和 `-shm`**。只换主库不删 WAL，SQLite 会把旧 WAL 重放到
   新库上，等于把"恢复"变成了"恢复 + 未知增量"，且 `integrity_check` 仍可能报 ok。
2. **备份是"那一天 23:30 的库"**，不是"每一天的库"。恢复到 N 天前意味着丢掉 N 天
   的 bar/事件/信号——恢复后要跑一次 `eod` 补当天，必要时用 §7 的回填补缺口。
3. **别用 `docker compose down -v` 图省事**：`data/` 是 bind mount，`down -v` 删的是
   命名卷，对 bind mount 无影响，但会顺带停掉 ntfy/cloudflared，恢复面变大。

## 8. 上线（首次与日常）

```bash
cd ~/stock-screener
# 首次 up 前先建宿主机目录并改属主（user: ${UID} 下容器对 bind mount 没有写权限）：
mkdir -p ntfy/cache ntfy/auth data/logs && chown -R $UID:$GID ntfy data
chmod 700 ~/.config/stock-screener && chown -R $UID:$GID ~/.config/stock-screener

# 构建镜像（前端产物在这一步生成；不先 build，up 会找不到镜像）
docker compose -f deploy/docker-compose.yml --project-directory . build

# 起常驻服务（backfill 在 tools profile 里，不会跟 up 启动）
docker compose -f deploy/docker-compose.yml --project-directory . up -d

# 核对 user 解析值（UID 没导出时会静默变 1000）
docker compose -f deploy/docker-compose.yml --project-directory . config | grep 'user:'
# 验证：docker compose … ps → 5 个常驻服务 Up（jobs 可能 starting，等 start_period）
# 验证：curl -s http://127.0.0.1:5000/healthz 不通是正常的（无 ports），用下面这条：
docker compose -f deploy/docker-compose.yml --project-directory . exec api \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5000/healthz').status)"
# 期望：200

# down 之后宿主机零残留（数据都在 ./data ./ntfy，密钥都在 ~/.config）
docker compose -f deploy/docker-compose.yml --project-directory . down
```

## 9. 清理、备份、升级与回滚

### 9.1 自动备份（每日 23:30）

`jobs` 在容器内用 `sqlite3.Connection.backup()` 备份（`python:3.12-slim` **没有 sqlite3 CLI**；
WAL 模式下也不能用 `cp` 热拷——未 checkpoint 的数据在 `-wal` 里）。写完
`integrity_check == ok` 才原子改名，保留最近 7 份；失败发 P0 `backup` 告警。

```bash
ls data/backups/          # 期望：market-YYYYMMDD.db，≤ 7 份
```

> 备份落在同一块盘上挡不住磁盘故障，定期把 `data/backups/` 同步到另一台机。

### 9.2 升级前手工备份

```bash
docker compose -f deploy/docker-compose.yml --project-directory . exec jobs python - <<'PY'
import sqlite3
s = sqlite3.connect('/app/data/market.db')
d = sqlite3.connect('/app/data/market.db.pre-upgrade')
s.backup(d); d.close(); s.close()
print('backup ok')
PY
# 验证：ls data/*.pre-upgrade → 文件存在且 > 100MB
```

### 9.3 升级与回滚

**版本号是唯一落点**（不再用日期 tag 这种只能靠记忆对号的东西）。

升级前先确认「当前线上是哪一版」——`/healthz` 会直接吐出来：

```bash
curl -s https://stock.example.com/healthz          # {"ok":true,"version":"x.y.z"}
```

打新版本（`make release` 会做校验、改四处版本号、收口 changelog、提交并打 tag）：

```bash
make check-version                    # 先确认没有漂移（纯文本检查，不用 Docker）
make release VERSION=x.y.z            # 干跑，看它打算改什么
make release VERSION=x.y.z APPLY=1    # 真执行（**只有 1 算真**，0/false 都是干跑）
git push && git push --tags
```

部署新版本：

```bash
# **必须 build**：容器只挂 config/ 与 data/，不挂源码；只 up -d 会继续跑旧代码
docker compose -f deploy/docker-compose.yml --project-directory . build api
docker compose -f deploy/docker-compose.yml --project-directory . up -d --force-recreate api jobs agent

# 验证真的换过去了（镜像重建但没 recreate 时这里会露馅）
curl -s https://stock.example.com/healthz          # version 应变成新版本
```

回滚到上一个发布版本：

```bash
git tag --list --sort=-v:refname          # 先看有哪些真实落点（别凭记忆写版本号）
git checkout <上一个 tag>                  # 用上面列出来的真实 tag，别凭记忆写版本号
docker compose -f deploy/docker-compose.yml --project-directory . build api
docker compose -f deploy/docker-compose.yml --project-directory . up -d --force-recreate api jobs agent
curl -s https://stock.example.com/healthz  # version 应变成回滚到的版本
git checkout main
```

**回滚落点 = `git tag --list` 里最早的那个 `v0.2.x`**。本仓库是公开版重建过的
历史，`v0.1.0` / `de56875` 等影子期之前的对象**不在其中**（见 CHANGELOG 的
`[0.1.0]` 小节说明）。事故中先 `git tag --list --sort=-v:refname` 看真实落点，
再动手。

###9.3.1部署现在会自己验收、自己回滚

`scripts/deploy.py`从 v0.2.116起带部署后验收，v0.2.117修掉了
「0.8秒就放行」的假验收，v0.2.119补上 build/up失败也回滚。

**流程**：

1.构建前把 `stock-screener:rollback`指向**上一次验收通过的镜像**
 （记在目标机的 `.last_good_image`；没有这个文件时才退回用当前 `latest`）。
2. `compose build` + `up -d --force-recreate`。
3. **验收**（最多 `--verify-wait`秒，默认90）：
 -先无条件观察 `--settle`秒（默认20），期间任何容器
 `Restarting` / `unhealthy` →立即失败；
 - `api`/`jobs`/`agent`必须都在 running；
 -有 healthcheck的必须变成 `healthy`；
 - **容器跑的镜像 ID必须等于这次构建的 ID**；
 -最近4分钟启动日志里不能有 `Traceback`。
4.通过 →把镜像 ID写进 `.last_good_image`，退出0。
5.确认失败（`FAIL:`）→自动回滚并**再验收一次**，退出1。
6. **无法确认**（`UNKNOWN:`）→ **只报警、不自动回滚，退出码3**。
退出码3与1不同是有意的：这时**新代码已经上线**（`up -d`已返回0），
只是没验成。当成「部署失败」去手工回滚，会把健康的新版本切回旧版。
6. **无法确认**（`UNKNOWN:`，ssh抖动/脚本异常）→ **只报警，不自动回滚**。
自动回滚会把一次成功的部署强行切回旧版本，代价比报警大。

**为什么要 settle和 health**：`up -d`返回时容器状态就是 `Up0 seconds`。
旧实现「看到 running就通过」实测 **0.8秒**就返回 OK——部署后30秒
才崩的容器根本等不到。

**手工排障**：

```bash
python3 scripts/deploy.py --host <ip> --dir <path> --no-verify #跳过验收（仅排障）
python3 scripts/deploy.py --host <ip> --dir <path> --settle 0 --verify-wait 30
```

**回到某个历史版本**（跨多个版本、或 `rollback`标签已经不对时）：

```bash
git tag --list --sort=-v:refname #先看真实落点，别凭记忆写版本号
git checkout <上一个 tag>
docker compose -f deploy/docker-compose.yml --project-directory . build api
docker compose -f deploy/docker-compose.yml --project-directory . up -d --force-recreate api jobs agent
curl -s https://stock.example.com/healthz # version应变成回滚到的版本
git checkout main
```

⚠️ **`docker image prune -a`会删掉回滚镜像**。`stock-screener:rollback`
是带标签的镜像，`docker image prune -f`（不带 `-a`）只删 dangling，
不会动它；但 `-a` / `system prune -a`会把"没有被容器引用"的标签镜像
一并清掉，回滚能力随之消失。`scripts/host_disk_guard.sh`建议的是
不带 `-a`的那条，别自作主张加。

⚠️ **回滚只回代码，不回数据**。`data/market.db`是 bind mount，
`core/db.py`的 `migrate()`只前进没有 down。跨大版本回滚前，
先按 §9.2备一份库。

**三个坑**（都踩过）：

1. `docker compose build` 输出被 `tail -1` 吞掉时可能静默失败（拉 Docker Hub token
   超时的 TLS 错误），镜像没更新但 `up -d` 报 `Running`（不是 `Started`）——
   看到 `Running` 就说明**容器没重建**，代码没换。
2. **Docker Hub 直连不稳**（`TLS handshake timeout`）。虚拟机已配镜像加速
   （`/etc/docker/daemon.json` 的 `registry-mirrors`：`docker.m.daocloud.io`、
   `docker.1ms.run`）。即便如此，`dockerfile:1` 前端镜像仍可能取不到——
   此时先显式拉一次基座再构建：

   ```bash
   docker pull docker/dockerfile:1
   docker pull python:3.12-slim-bookworm
   docker pull node:20-bookworm-slim
   ```

   基座在本地有缓存后，`build` 不再需要联网。
3. 因此部署后一律用 `/healthz` 的 `version` 复验，别信命令的退出码。

### 9.4 清理

每日 16:00 由 jobs 自动清理（`push_log`/`event_log` 30 天、`pool` 90 天、
`upstream_calls` 2 小时、过期的 agent 计划改 `cancelled`），磁盘水位 >85% P1、>95% P0。
手工看水位：

```bash
df -h data
# 验证：/api/status 的 disk_used_pct 与 df 一致（±2%）
```

## 10. agent 上线（第二批）

**两阶段，不能跳**：先 `shadow` 观测 ≥ 3 个交易日标定阈值，再正式启用。没有那 3 天的
`salience` 分布就直接开 `shadow_mode: false`，等于拿未标定的阈值上线——上线首日要么一条不推，
要么把手机刷爆。

### 10.0 上线前自检（每次切阶段前都跑）

```bash
# 准备开始 shadow 观测
docker compose -f deploy/docker-compose.yml --project-directory . --profile tools \
    run --rm agent python scripts/accept/preflight.py --for-shadow

# 准备正式启用（shadow_mode 已改 false、容器已重建之后）
docker compose -f deploy/docker-compose.yml --project-directory . --profile tools \
    run --rm agent python scripts/accept/preflight.py
```

它查的是**切阶段才会暴露**的那几类问题：`budget.write_failed` 这个 fail-closed 标志
（一旦写入，所有回合都会被拒，必须手动清）、`shadow_mode` 与库里 `decision` 分布是否一致
（改了 yaml 没重建容器是最常见的坑）、预算表与 `agent_turn.tokens_*` 是否对得上（漏账会在这里现形）、
预留有没有残留未对账、agent 心跳在不在、三份 env 齐不齐、容器里有没有代理变量。

**不通过就别往下走**，脚本会直接给出该删的表项或该改的配置。

### 10.1 shadow 观测期

```bash
# config/agent.yaml 保持 attention.shadow_mode: true（默认值，先别改）
docker compose -f deploy/docker-compose.yml --project-directory . up -d --build agent api jobs

# 每日收盘后标定一次
docker compose -f deploy/docker-compose.yml --project-directory . --profile tools \
    run --rm agent python scripts/accept/batch2.py --salience-histogram --date YYYYMMDD
```

shadow 期 `poll()` 只写 `agent_turn(decision='shadow')`、不调用 LLM、不改 `agent_status`——
所以这段时间**不会有 LLM 花费**，agent 自己也**不会主动发消息**，可以放心挂着观察。

**但「不会推送」这句话只对通道 B（agent 主动开口）成立**，别误读成全站静默：

| 通道 | shadow 期行为 |
|---|---|
| A：确定性信号（突破/破位/滞涨…） | **照常推送**，这是产物本身，不是 agent 的决策 |
| B：agent 主动开口（心跳/计划/事件唤醒后的发言） | 被拦下，只记 `decision='shadow'` |
| C：运维告警（自检失败、磁盘、断流） | **照常推送**，与 shadow 无关 |

看几个数就能确认 shadow 真的生效：`push_log` 有 `accepted` 行（通道 A 照常）、
`llm_budget` **零行**、所有回合的 `tokens_in/out` 均为 **NULL**。
看到有推送不代表 shadow 没生效——判断依据是**回合表的 `decision` 全为 `shadow`、
且零 LLM 花费**。

**标定口径**：让通道 B 被 `suppressed` 的比例落在 **20–40%**。按直方图调
`config/strategy.yaml` 的 `attention.salience_min` 与 `attention.base`，**不改代码**。

### 10.2 正式启用

```bash
# 1. 确认已经标定过（见 10.1 的记录），再把 shadow_mode 改成 false
# 2. 重建三个服务
docker compose -f deploy/docker-compose.yml --project-directory . up -d --build agent api jobs
# 3. 确认没有代理变量（有代理会连不上中转站）
docker compose -f deploy/docker-compose.yml --project-directory . exec agent env | grep -i proxy
# 4. 首个交易日 09:15 起，库里应该有心跳行
docker compose -f deploy/docker-compose.yml --project-directory . exec api \
    python -c "import sqlite3;print(sqlite3.connect('/app/data/market.db').execute(\"select wake_kind,count(*) from agent_turn where substr(ts,1,10)=date('now','+8 hours') group by 1\").fetchall())"
```

**每日收盘后跑一次统计**（不达标非零退出，直接看输出）：

```bash
docker compose -f deploy/docker-compose.yml --project-directory . --profile tools \
    run --rm agent python scripts/accept/batch2.py --date YYYYMMDD
```

## 11. 故障演练（第二批）

**演练必须与生产数据面隔离**。`docker compose run agent` 默认复用同一 `./data`、同一
`service_heartbeat('agent')` 主键、同一 `llm_budget`——不隔离的话：演练会写生产 `agent_turn`、
每 30 秒覆盖真实心跳（「stop 3 分钟收 P0」那条演练会假绿）、而且烧真实额度。

```bash
cp data/market.db /tmp/drill.db
docker compose -f deploy/docker-compose.yml --project-directory . run --rm \
  -v /tmp/drill.db:/tmp/drill.db \
  -e SS_DB_PATH=/tmp/drill.db -e HEARTBEAT_SERVICE=agent-drill -e SS_DRILL=1 \
  -e LLM_BASE_URL=http://blackhole:1 agent
```

`SS_DRILL=1` 时 `AgentPusher` 全部方法只写日志不推送，心跳服务名用 `HEARTBEAT_SERVICE`。

逐条对照 spec §10 第二批的演练项，结果记进
``docs/architecture.md`（验收记录已并入）`。**演练与正式统计分在两个交易日做**，
否则统计里会混进演练回合（脚本的 `--exclude-drill` 只是双保险，防的是有人忘了传
`SS_DB_PATH` 直接 `docker compose run agent`）。

## 12. 磁盘维护

反复 `docker compose build` 会累积构建缓存（实测一天 20 次构建攒到 11.9 GB）。
`/` 曾从 56G 涨到 60G，清一次回到 53G：

```bash
docker builder prune -f          # 只清未被使用的缓存层，不动在用镜像
docker system df                 # 确认回收量（**看 Reclaimable 那一列，不止 Build Cache**）
df -h /                          # 目标：可用空间 > 30G

# 可回收里还有悬空镜像/未用卷时，再补这两条（都是安全的，不动活着的容器）
docker image prune -f
docker volume prune -f
```

2026-11-21 在 81.4 上实测：`docker builder prune -f` 回收 **15.69 GB**，
根分区从 85%（14G 可用）回到 70%（29G 可用）。

**不需要**动 `data/backups/`（7 份日备份 ≈8.5 GB，是恢复演练依赖的资产）。

### 12.1 测试容器不许长期留守（2026-11-21 事故）

> 本机 Docker Desktop 虚拟磁盘写满（294.65/294.68 GB），PostgreSQL 起不来：
> `could not write lock file "postmaster.pid": No space left on device`。
> 元凶是项目自己的长期测试容器 `ss-dev-run`（`sleep infinity`，**从不删除**）：
> `docker system df` 显示容器占用 **230.4 GB**、其中 230.2 GB 可回收，
> 而那个镜像只有 654 MB。`/tmp` 下统计到 33 万个变更路径。

两条硬规矩：

1. **不要在测试机上养一个常驻容器**。跑测试用 `make test`（内部是
   `docker run --rm`）或 `scripts/test_remote.sh`（远端 `--rm`）。
   `docker run --rm` 会收拾，`sleep infinity` 的守护容器不会。
2. **临时文件必须落在 tmpfs 或 `pytest` 的 `tmp_path` 里**。
   `Makefile` 的 `test` 已带 `--tmpfs /tmp:rw,size=2g`——临时文件进内存、
   随容器销毁、容量封顶。代码侧由 `tests/conftest.py` 的 autouse fixture
   兜底：任何 `tempfile.mkdtemp()` 都会被重定向到本用例的 `tmp_path`。

应用侧巡检（每 10 分钟）：`jobs` 的 `disk` 任务查宿主机根分区，
75% 预警 / 85% P1 / 95% P0。阈值在 `jobs/cleanup.py`。

宿主机侧护栏（容器**看不到** `/var/lib/docker` 的可写层明细，该目录在容器内为空）：

```bash
# 每 30 分钟，crontab 里加一行
*/30 * * * * NTFY_URL=<ntfy 地址> NTFY_TOPIC=<主题> \
  /home/qdm/stock-screener/scripts/host_disk_guard.sh >> /var/log/ss-disk-guard.log 2>&1
```

它检查三项：宿主机分区水位、**单个容器可写层是否异常增长**（230 GB 那种）、
Docker 可回收空间。**只报告，不自动删**——删什么属于运维决策。
