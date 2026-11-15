# 更新日志

本文件记录本项目的所有重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## 约定

**版本号怎么涨**（本项目是单实例自用系统，按「使用者能感知到什么」判，不按代码量）：

| 段 | 什么时候涨 | 本项目的例子 |
|---|---|---|
| `MAJOR` | 需要人工迁移数据/配置，或旧数据不再兼容 | 改了 `event_log` 主键口径 |
| `MINOR` | 新增可感知能力（新判据、新推送类型、新页面） | 增加板块级合并推送 |
| `PATCH` | 修缺陷、改内部实现，对外行为不变 | 修掉信号刷屏 |

**记录纪律**：

1. 日常改动先堆在 `[Unreleased]`，分类只允许 Keep a Changelog 那六种：
   `新增 / 变更 / 弃用 / 移除 / 修复 / 安全`。
2. 每条写**使用者能看懂的现象**，不写「重构了 X 模块」这种内部动作；
   缺陷条要写清「原来错在哪、修完是什么样」。
3. 发布时把 `[Unreleased]` 整体下沉成 `## [x.y.z] - YYYY-MM-DD`，
   并把 `[Unreleased]` 重新留空。
4. `core/version.py` 的 `VERSION`、三处包清单、git tag `v{x.y.z}`、
   本文件的小节标题，**四者必须一致**——由 `tests/test_version.py` 守卫。

**发布动作**：`make release VERSION=x.y.z`（校验 → 改版本 → 收口 changelog → 提交 → 打 tag）。

---


## [Unreleased]

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
