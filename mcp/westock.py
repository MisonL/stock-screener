"""westock-mcp Connector 的封装（计划 Task 6，spec §5.4.5 / D13 / D18 / D22）。

**它封装的是「腾讯自选股 MCP」，不是行情源**：D18 禁止直连 `qt.gtimg.cn` /
`hq.sinajs.cn` / `proxy.finance.qq.com` 的行情端点；本模块只打
`WESTOCK_MCP_URL`（`stockbuddy.qq.com/…/mcp`）的 `tools/call`，与 westock CLI 同属
「腾讯自选股服务端」，走各自进程内的 `RateLimiter`（≥300ms）。

三条设计约束：

1. **每次调用重读 env 文件**（`/state/westock.env`，D13 的可写挂载点）——续期脚本
   改写它之后不必重启进程。
2. **schema 只有一份**：`MCP_TOOL_SCHEMAS` 在 `llm/tools.py`（Task 3）声明，本模块
   `schema()` 原样返回，不另造副本（两处定义必然漂移）。
3. **写方法只能从确认卡回调调**（`ToolRegistry` 侧对 4 个写工具永远返回
   `needs_confirm`）；这里的 `add_watch` / `remove_watch` / `set_alert` / `cancel_alert`
   是「执行层」，不含任何权限判断——权限在调用点。

降级纪律：token 缺失或上游 401/403 → 整个封装返回 `ok: false,
reason: "mcp_unavailable"`，**绝不抛异常**，主链路（选股、推送）不受影响。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from core.clock import SH
from core.http import RateLimiter
from core.market import classify_market
from llm.schemas import MCP_TOOL_SCHEMAS, envelope

MIN_INTERVAL_S = 0.3                     # D18：只打 westock，≥300ms；与 jobs 的行情限速器独立
TIMEOUT_S = 15
ENV_PATH = Path("/state/westock.env")
DEFAULT_MCP_URL = "https://stockbuddy.qq.com/cgi/cgi-bin/openai/mcp/mcp"
MARKETS = ("A", "HK", "US")              # MCP 的 market 只认这三个缩写，传中文无效
SYS_PREFIX = "SYS:"

# 封装方法 → 底层 MCP 工具（spec §5.4.5 的表，逐行对应；改这里就是改协议）
MCP_TOOLS = {
    "read_watchlist": "portfolio_watchlist",
    "read_alerts": "portfolio_tips_query",
    "add_watch": "portfolio_watchlist_add",
    "remove_watch": "portfolio_watchlist_remove",
    "set_alert": "portfolio_tips_set",     # 全量覆盖语义，必须 read → merge → set
    "cancel_alert": "portfolio_tips_set",
}


class WestockMcp:
    """6 个工具的封装；`http` 只要有 `.post(url, json=…, headers=…, timeout=…)` 即可。"""

    def __init__(self, env_path=ENV_PATH, http=None, clock=None, limiter=None):
        self.env_path = Path(env_path)
        self._http = http
        self.clock = clock
        self.limiter = limiter or RateLimiter(MIN_INTERVAL_S)
        self._seq = 0

    # ---- 基础设施 ---------------------------------------------------------

    @property
    def http(self):
        """惰性建 `httpx.Client`：`trust_env=False` 必须给（容器里清过代理变量，
        但宿主机跑测试时环境里的 HTTPS_PROXY 会把自选股请求绕到境外代理上）。"""
        if self._http is None:
            import httpx

            self._http = httpx.Client(trust_env=False, timeout=TIMEOUT_S)
        return self._http

    def _as_of(self) -> str:
        now = self.clock.now() if self.clock is not None else datetime.now(SH)
        return now.isoformat()

    def _read_env(self) -> dict[str, str]:
        """每次调用重读 env 文件（续期后免重启）。"""
        try:
            text = self.env_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        out: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
        return out

    def schema(self) -> list[dict]:
        """原样返回 Task 3 的 6 个 schema（单一定义源）。"""
        return MCP_TOOL_SCHEMAS

    # ---- JSON-RPC ---------------------------------------------------------

    def _rpc(self, tool: str, args: dict) -> dict:
        """一次 `tools/call`；永远返回信封，不抛。"""
        as_of = self._as_of()
        env = self._read_env()
        token = env.get("WESTOCK_ACCESS_TOKEN") or ""
        if not token:
            return envelope(ok=False, reason="mcp_unavailable", as_of=as_of)
        url = env.get("WESTOCK_MCP_URL") or DEFAULT_MCP_URL
        self._seq += 1
        body = {"jsonrpc": "2.0", "id": self._seq, "method": "tools/call",
                "params": {"name": tool, "arguments": args}}
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        self.limiter.acquire()
        try:
            resp = self.http.post(url, json=body, headers=headers, timeout=TIMEOUT_S)
        except Exception as exc:                                  # noqa: BLE001
            return envelope(ok=False, reason=f"mcp_error:{type(exc).__name__}", as_of=as_of)
        status = getattr(resp, "status_code", 0)
        if status in (401, 403):
            # token 过期/被吊销：整个封装降级，主链路照常
            return envelope(ok=False, reason="mcp_unavailable", as_of=as_of)
        if status != 200:
            return envelope(ok=False, reason=f"mcp_error:http_{status}", as_of=as_of)
        payload = _payload(resp)
        if not isinstance(payload, dict):
            return envelope(ok=False, reason="mcp_error:bad_payload", as_of=as_of)
        if payload.get("error"):
            err = payload["error"]
            code = err.get("code") if isinstance(err, dict) else None
            return envelope(ok=False, reason=f"mcp_error:rpc_{code}", as_of=as_of)
        result = payload.get("result")
        if not isinstance(result, dict):
            return envelope(ok=False, reason="mcp_error:no_result", as_of=as_of)
        if result.get("isError"):
            return envelope(ok=False, reason="mcp_error:tool", as_of=as_of)
        return envelope(data=_content_data(result), as_of=as_of)

    # ---- 只读（agent 可自动调） -------------------------------------------

    def read_watchlist(self, group=None, market=None) -> dict:
        """**分组参数的键是 `group`，不是 `group_id`**（传 `group_id` 会被静默忽略并返回全部）。"""
        args: dict = {}
        if group not in (None, ""):
            args["group"] = group
        if market in MARKETS:
            args["market"] = market          # 传中文无效，与其静默返回全部，不如本地就不发
        return self._rpc(MCP_TOOLS["read_watchlist"], args)

    def read_alerts(self) -> dict:
        return self._rpc(MCP_TOOLS["read_alerts"], {})

    # ---- 写（只能从确认卡回调调） -----------------------------------------

    def add_watch(self, code: str, note: str | None = None) -> dict:
        args = {"code": code}
        if note:
            args["note"] = note
        return self._rpc(MCP_TOOLS["add_watch"], args)

    def remove_watch(self, code: str) -> dict:
        return self._rpc(MCP_TOOLS["remove_watch"], {"code": code})

    def set_alert(self, code: str, kind: str, threshold: float) -> dict:
        """`portfolio_tips_set` 是**全量覆盖**：先 read 回填，只增删 `SYS:` 前缀项。

        读失败时直接返回失败信封——拿空列表去覆盖会把用户手设的提醒全部抹掉。
        """
        current = self.read_alerts()
        if not current.get("ok"):
            return current
        rows = _rows(current.get("data"))
        kept = [row for row in rows
                if not (_is_sys(row) and _alert_code(row) == code and _alert_kind(row) == kind)]
        merged = kept + [_sys_alert(code, kind, threshold)]
        return self._rpc(MCP_TOOLS["set_alert"], {"tips": merged})

    def cancel_alert(self, code: str, kind: str) -> dict:
        current = self.read_alerts()
        if not current.get("ok"):
            return current
        rows = _rows(current.get("data"))
        kept = [row for row in rows
                if not (_is_sys(row) and _alert_code(row) == code and _alert_kind(row) == kind)]
        return self._rpc(MCP_TOOLS["cancel_alert"], {"tips": kept})


# ---------------------------------------------------------------- 单向导入


def import_watchlist(conn, mcp) -> int:
    """把手机自选股**单向**导入 `watch_stock(source='westock')`，返回新增条数。

    只增不改：`INSERT OR IGNORE` 让已存在的行（尤其 `source='manual'` 的手设行）
    原样不动；本地有、手机没有的行**不删**（不回写，用户删自选股不该连带删掉我们
    自己的观察名录）。所以两次运行的结果完全相同。
    """
    try:
        env = mcp.read_watchlist()
    except Exception:                                             # noqa: BLE001
        return 0
    if not isinstance(env, dict) or not env.get("ok"):
        return 0
    now_iso = (mcp.clock.now() if getattr(mcp, "clock", None) is not None
               else datetime.now(SH)).isoformat()
    added = 0
    rows = _rows(env.get("data"))
    with conn:
        for row in rows:
            code = _code_of(row)
            if not code:
                continue          # 裸六位代码没有市场前缀，classify_market 明确「不要猜」
            cur = conn.execute(
                "INSERT OR IGNORE INTO watch_stock(code, note, pct_threshold, source, added_at)"
                " VALUES (?, NULL, NULL, 'westock', ?)", (code, now_iso))
            if cur.rowcount and cur.rowcount > 0:
                added += 1
    return added


# ---------------------------------------------------------------- 小工具


def _payload(resp) -> dict | None:
    """MCP 是无状态 streamable HTTP：既可能回 JSON，也可能回 `text/event-stream`。"""
    ctype = ""
    headers = getattr(resp, "headers", None) or {}
    try:
        ctype = str(headers.get("content-type") or headers.get("Content-Type") or "")
    except Exception:                                             # noqa: BLE001
        ctype = ""
    text = getattr(resp, "text", "") or ""
    if "text/event-stream" in ctype or text.lstrip().startswith("data:"):
        frames = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        for frame in reversed(frames):
            try:
                return json.loads(frame)
            except ValueError:
                continue
        return None
    try:
        return resp.json()
    except Exception:                                             # noqa: BLE001
        try:
            return json.loads(text)
        except ValueError:
            return None


def _content_data(result: dict):
    """把 `result.content[].text` 里的 JSON 取出来；单条文本不是 JSON 就原样给文本。"""
    content = result.get("content")
    if isinstance(content, list):
        texts = [c.get("text") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except (TypeError, ValueError):
                return texts[0]
        if texts:
            return texts
    if "structuredContent" in result:
        return result["structuredContent"]
    return result


def _rows(data) -> list[dict]:
    """工具返回的形状归一：list / {data|rows|list|stocks: [...]} / 单对象。"""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("data", "rows", "list", "stocks", "items", "tips"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [data]
    return []


def _code_of(row: dict) -> str | None:
    """取一个**带市场前缀**的代码；`600519` 这种裸代码不算（`classify_market` 返回 None）。"""
    raw = row.get("code") or row.get("symbol") or row.get("stock_code")
    if not isinstance(raw, str):
        return None
    code = raw.strip().lower()
    return code if code and classify_market(code) is not None else None


def _is_sys(row: dict) -> bool:
    """`SYS:` 前缀项 = 我们自己设的提醒。字段名上游没有保证，所以整体看。"""
    return SYS_PREFIX in json.dumps(row, ensure_ascii=False)


def _alert_code(row: dict) -> str | None:
    raw = row.get("code") or row.get("symbol")
    return str(raw).strip().lower() if raw else None


def _alert_kind(row: dict) -> str | None:
    for key in ("kind", "type", "alert_type"):
        if row.get(key):
            return str(row[key]).strip().lower()
    text = str(row.get("text") or row.get("note") or "")
    if SYS_PREFIX in text:
        rest = text.split(SYS_PREFIX, 1)[1].strip()
        return rest.split()[0].lower() if rest else None
    return None


def _sys_alert(code: str, kind: str, threshold: float) -> dict:
    return {"code": code, "kind": kind, "threshold": threshold,
            "text": f"{SYS_PREFIX}{kind} {threshold}"}
