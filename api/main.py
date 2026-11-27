"""FastAPI 入口：`uvicorn api.main:app`。

模块级 `app` 必须显式装配（`create_app(...)`），否则 uvicorn 直接 `AttributeError`。
连接不在 import 期建：`make_conn_factory()` 每次按需建连接并迁移（Task 2 的约定）。
"""
import os
from pathlib import Path

from api import logs as _api_logs
from api.routes import create_app
from core.clock import SystemClock
from core.config import load_config
from core.db import make_conn_factory
from notify.dispatcher import Dispatcher
from notify.ntfy import NtfyClient

# 模块级安装：uvicorn 的 access logger 在进程启动时就存在，这里是最早的挂载点。
# 把 `/healthz` 的探活记录过滤掉——实测它占了 api 日志的 99.6%，会把有用信息淹掉。
_api_logs.install()


def _warn_if_placeholder_config(cfg) -> None:   # pragma: no cover - 进程入口
    """与 jobs 侧同一条提示：占位域名会让同源校验拒绝所有写操作。

    **必须在装配 `app` 之前调用**：`api/routes.py::_same_origin` 的判据是
    `Origin == https://{notify.public_host}`，`public_host` 还是 `stock.example.com`
    时所有写操作都会 400，而健康检查、选股、推送全绿——用户只看到「按钮点了没反应」。
    """
    import logging

    from core.config import is_placeholder_host

    host = getattr(getattr(cfg, "notify", None), "public_host", None)
    if is_placeholder_host(host):
        logging.getLogger("api").warning(
            "public_host 仍是占位值 %s：同源校验会拒绝所有写操作"
            "（网页按钮点了没反应）。改 config/notify.yaml 与 deploy/ntfy/server.yml", host)
    # `web.env` 只挂给 api，所以口令检查只能放在这里（jobs 侧查是恒真误报）
    if not (os.environ.get("SS_WEB_PASS") or "").strip():
        msg = ("SS_WEB_PASS 未配置：网页不鉴权。公网部署务必在 "
               "~/.config/stock-screener/web.env 里设置")
        # **公网部署必须 fail-closed，不能只 warning 后继续**。
        #
        # `api/auth.py` 的中间件在口令缺失时 `return await call_next(request)`
        # ——**全站放行**。而表现是"一切正常、页面能用"，日志里一条 warning
        # 没有人会看。`deploy/docker-compose.yml` 的 `web.env` 还写着
        # `required: false`，文件不存在也能启动。
        #
        # 届时这条公网 URL 上：`/api/status`、`/api/push-log`、`/api/agent-log`
        # 全量数据可读，`POST /api/chat` 能烧 LLM 预算，写 `watch` 能改状态。
        # 路径写错、`chmod` 不对、迁移机器漏建文件，任何一处都会静默变成这样。
        #
        # 触发条件：`public_host` 已配成真域名（说明这是对外部署），
        # 而不是仍为 RFC 2606 占位域 / 空（本地开发）。
        if not is_placeholder_host(host):
            raise SystemExit(
                "SS_WEB_PASS 未配置且 public_host 已设为真域名 "
                f"({host})：拒绝启动。这也是唯一一道「人在门外」的关口，"
                "不能 fail-open。请创建 ~/.config/stock-screener/web.env 并写 "
                "SS_WEB_PASS=<口令>。")
        logging.getLogger("api").warning("%s（当前 public_host 仍是占位值，视为本地开发，继续启动）", msg)

_cfg = load_config(Path("config"))
_warn_if_placeholder_config(_cfg)
_clock = SystemClock()
# **`migrate_each=False`**：`core/db.py` 的注释明确要求 api 这么用。每次 HTTP 请求
# 都跑 `migrate()` 等于每个请求一次 `BEGIN IMMEDIATE`，会跟 jobs 的写事务互撞
# （busy_timeout 一过就是 500 database is locked），而未鉴权的公网请求也会触发它。
# 迁移在下面启动时做一次就够。
_factory = make_conn_factory(migrate_each=False)
# 启动时**迁移一次**（`migrate_each=False` 之后就没有别的地方做了）
from core.db import migrate  # noqa: E402  —— 紧跟 factory 便于阅读

_conn = _factory()
migrate(_conn)
_dispatcher = Dispatcher(
    _conn,
    NtfyClient(_cfg.notify.ntfy_url, _cfg.notify.topic, os.environ.get("NTFY_TOKEN")),
    _cfg, _clock,
)
# **LLM 故障转移必须有人看得见**（v0.2.152）：客户端只发信号，落账在这里做。
# 不注册的话 `last_failover` 只活在内存和日志里——11-27 那次就是"换了供应商没人知道"。
from llm.client import set_failover_sink  # noqa: E402
from llm.failover import record as _record_failover  # noqa: E402

set_failover_sink(lambda info: _record_failover(
    _conn, info, _clock.today(), _dispatcher))

app = create_app(_cfg, _factory, _clock, dispatcher=_dispatcher)
