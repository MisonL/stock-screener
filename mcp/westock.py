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

import logging

import json
from datetime import datetime
from pathlib import Path

from core.clock import SH
from core.http import RateLimiter
from core.market import classify_market
from llm.schemas import MCP_TOOL_SCHEMAS, envelope

log = logging.getLogger("mcp.westock")

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
    # 除权/停牌：**不是 agent 工具，是 jobs 的周刷新数据源**
    "dividend": "data_dividend",
    "suspension": "data_suspension",
}

#: 停牌接口的 market 参数与 `MARKETS` 不是一回事：`data_suspension` 只认 hs/hk/us
SUSPENSION_MARKET = "hs"


class WestockMcp:
    """6 个工具的封装；`http` 只要有 `.post(url, json=…, headers=…, timeout=…)` 即可。"""

    def __init__(self, env_path=ENV_PATH, http=None, clock=None, limiter=None,
                 sleep=None):
        self.env_path = Path(env_path)
        #: 退避用的 sleep，测试注入成空函数即可不真的等
        self.sleep = sleep
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

    # ---- 除权 / 停牌（jobs 的周刷新用，不进 agent 工具集） -----------------

    def batch_dividend(self, codes, years: int = 10, chunk: int = 50,
                       backoff_s: float = 5.0, retries: int = 3):
        """多只票的分红送转，返回 `{code: [CorpAction, ...]}`。

        **字段名是实测出来的**（`data_dividend` 的返回，见 `_action_of`）：
        `cashDiviRMB` 是「每 10 股派息（元）」，不是每股；`bonusShareRatio` 是
        每股送股比例、`tranAddShareRatio` 是每股转增比例——这两个已经是"每股"
        口径，不用再除 10。`exDiviDate` 是 `YYYYMMDD` 字符串。

        只保留 `procedure='方案实施'` 且 `exDiviDate` 合法的行：未实施的预案没有
        除权日，拿来算复权会把因子算错。

        **取数失败返回 `None`，不是 `{}`**：调用方要能区分「查到了，这些票确实
        没分红」和「压根没查到」。混为一谈会让"失败"被记成"已完成"，然后按
        ≥7 天的规则推到下周才重试（0.2.33 就栽在这上面）。不抛异常——周刷新失败
        不该把 jobs 主链路带崩。
        """
        from core.datasource.westock import CorpAction

        out: dict[str, list] = {}
        codes = list(codes)
        failed_chunks = 0
        for i in range(0, len(codes), chunk):
            part = codes[i:i + chunk]
            data = self._fetch_chunk(part, years, backoff_s=backoff_s, retries=retries)
            if data is None:
                # **单个 chunk 失败不再整体放弃**：5000 多只票要发 100 多个请求，
                # 全丢会让整个周刷新白跑，而且因为不盖时间戳会每次启动都重来一遍。
                # 记一笔继续下一个 chunk，最后按"有没有拿到东西"返回。
                failed_chunks += 1
                continue
            # **两种返回结构都出现过**（实测）：
            #   单只票 → `{code, end, plans:[…]}`
            #   多只票 → `{sh600036: {code, end, plans:[…]}, sz002415: {…}}`
            # 判据是"顶层有没有 plans"：有就是单只，没有就按 code 键遍历。
            if isinstance(data, dict) and "plans" in data:
                blocks = [data]
            elif isinstance(data, dict):
                blocks = list(data.values())
            elif isinstance(data, list):
                blocks = data
            else:
                blocks = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                code = _normalize_code(block.get("code"))
                if not code:
                    continue
                for plan in block.get("plans") or []:
                    action = _action_of(code, plan)
                    if action is not None:
                        out.setdefault(code, []).append(action)
        total_chunks = max(1, (len(codes) + chunk - 1) // chunk)
        if failed_chunks and failed_chunks == total_chunks:
            return None                      # 全失败：真的没取到数据，别盖"已完成"
        if failed_chunks:
            log.warning("分红取数 %d/%d 个批次失败（多为上游限频），已写入其余批次",
                        failed_chunks, total_chunks)
        return out

    def _fetch_chunk(self, part, years, backoff_s: float, retries: int):
        """取一个批次，撞上限频就退避重试；拿到数据返回按 code 分组的块。

        **限频的伪装必须看穿**：上游被限流时不会报错，而是返回
        `ok=True` + **空/残缺数据**（实测：连续调用时返回键数 50 → 17 → 0 → 1 → 0…，
        冷却一两分钟才恢复）。若不校验条数，这些"空返回"会被当成
        「这些票十年内都没分红」——数据静默缺失，比报错难发现得多。

        所以判据是**返回的票数明显少于请求数**（少于一半）就当限频，退避重试；
        重试仍不足才接受现有结果（B 股、新股本来就可能没有分红记录）。
        """
        want = len(part)
        best: dict | None = None
        for attempt in range(retries + 1):
            resp = self._rpc(MCP_TOOLS["dividend"],
                             {"codes": ",".join(part), "years": int(years)})
            if not resp.get("ok"):
                reason = resp.get("reason") or ""
                if "mcp_unavailable" in reason:
                    return None                  # token 没了，重试也没用
                data = None
            else:
                data = _unwrap(resp.get("data"))
            got = len(data) if isinstance(data, dict) else 0
            if got * 2 >= want:
                return data                      # 够数，直接用
            # 不足一半：优先保留拿到最多的那次（残缺也比没有强）
            if best is None or got > len(best):
                best = data if isinstance(data, dict) else best
            if attempt < retries and self.sleep:
                self.sleep(backoff_s * (attempt + 1))
        return best

    def suspension(self, start: str, end: str, market: str = SUSPENSION_MARKET):
        """停牌列表，返回 `[(code, start_date, end_date), ...]`。

        `end_date` 用接口给的 `resumptionDate`（复牌日）；**未定复牌日的返回
        `19000101` 这个哨兵值**，这里换成 `end`（今天）——把它当"还在停牌"，
        比记成一个 1900 年的日期安全得多：`resume_dates()` 按 `end_date == 上一
        交易日` 找复牌股，1900 年的记录永远不会误命中，但也永远清不掉。
        接口只按当前快照给"正在停牌"的票，没有区间查询参数，`start`/`end` 仅用于
        把哨兵值夹到今天。

        **取数失败返回 `None`**（理由同 `batch_dividend`）：空列表是正常结果
        （今天没有票停牌），不能拿来当失败信号。
        """
        resp = self._rpc(MCP_TOOLS["suspension"], {"market": market})
        if not resp.get("ok"):
            return None
        data = _unwrap(resp.get("data"))
        rows = data.get("stocks") if isinstance(data, dict) else None
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            code = _normalize_code(row.get("code"))
            begin = _ymd(row.get("suspendDate"))
            if not code or begin is None:
                continue
            finish = _ymd(row.get("resumptionDate"))
            if finish is None or finish <= begin:
                finish = _ymd(end) or begin      # 19000101 / 未定 → 当作"仍在停牌"
            out.append((code, begin, finish))
        return out


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


def _normalize_code(raw) -> str | None:
    """把 `sh600519` / `600519.SH` 统一成 `sh600519`；认不出就返回 None。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().lower()
    if len(text) >= 8 and text[6] == ".":
        num, suffix = text[:6], text[7:9]
        if suffix in ("sh", "sz", "bj"):
            return f"{suffix}{num}"
    if len(text) == 8 and text[:2] in ("sh", "sz", "bj"):
        return text
    return None


def _ymd(raw) -> int | None:
    """`"20260626"` / `20260626` / 带横线的日期 → `20260626`；非法返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    if text.startswith("1900"):             # 接口用 19000101 表示"未定/无"
        return None
    return int(text)


def _num(raw) -> float:
    """空串 / None / 非数字一律 0.0——接口用空串表示"本次没有这一项"。"""
    try:
        return float(str(raw).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _action_of(code: str, plan) -> "object | None":
    """一条 `plans[]` → `CorpAction`；未实施或无除权日返回 None。

    单位口径（**实测确认，别照直觉改**）：三个字段都是「每 **10 股**」的刻度，
    所以要除以 10 才得到每股口径。证据是同一行的 `dividendPlan` 文本：

        cashDiviRMB='10.03'   dividendPlan='10派10.030元'
        tranAddShareRatio='4.00'  dividendPlan='10派5.800元转4.000股'

    即 `cashDiviRMB` 就是「10 派 X 元」里的 X，`tranAddShareRatio` 就是
    「转 N 股」里的 N。不除 10 会让每股现金虚高 10 倍、复权因子整个算错。
    """
    from core.datasource.westock import CorpAction

    if not isinstance(plan, dict):
        return None
    if str(plan.get("procedure") or "").strip() != "方案实施":
        return None
    ex = _ymd(plan.get("exDiviDate"))
    if ex is None:
        return None
    return CorpAction(
        code=code,
        ex_date=ex,
        cash_per_share=_num(plan.get("cashDiviRMB")) / 10.0,
        bonus_ratio=_num(plan.get("bonusShareRatio")) / 10.0,
        tran_ratio=_num(plan.get("tranAddShareRatio")) / 10.0,
        source="westock_dividend",
    )


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


def _unwrap(data):
    """剥掉工具返回的 `{"ok": …, "data": …}` 外壳，给出真正的业务数据。

    **这是实测出来的形状**：`data_dividend` / `data_suspension` 的 `content[].text`
    是 `{"ok":true,"data":{…}}`，而 `_content_data()` 只是把 text 解析成 JSON，
    所以 `_rpc()["data"]` 里还是带 `ok`/`data` 的一层壳。不剥就会把 `True` 和
    字典当成两个"股票块"去遍历，结果一条都取不到——而且不报错。
    只剥一层，且仅当它确实有 `data` 键、没有业务字段时。
    """
    if isinstance(data, dict) and "data" in data and "ok" in data:
        return data["data"]
    return data


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
