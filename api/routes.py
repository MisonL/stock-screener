"""只读接口：行情、K 线、池子、板块、自选、信号与状态（§7.5）。

三条硬约束：

1. **`conn_factory()` 是唯一进库方式**，每个请求用完即关——不要在模块级持有连接，
   否则 `uvicorn --workers N` 下多个进程抢同一个 `sqlite3.Connection` 会崩。
2. **`/api/status` 只从库里推导**（`push_log` / `service_heartbeat` / `upstream_calls`）：
   jobs 与 api 是两个进程，内存里的标志永远对不上。
3. **`/api/watch` 的写操作要过同源校验**（`Origin` 必须等于 `notify.public_host`）：
   公网的 Cloudflare Access 兜住「谁能进来」，同源校验兜住「别的站点替用户发请求」，
   两层互不替代。
"""
from __future__ import annotations

import json

import os
import secrets
from contextlib import asynccontextmanager, closing
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from core.clock import SH, TradingCalendar
from core.market import classify_market
from core.snapcache import SnapshotCache
from core.version import VERSION
from jobs.snapshot import SCOPES, calls_by_scope
from notify.hmac_sign import ensure_secret, is_expired, verify


def _json_or_none(raw):
    """库里存的是 JSON 文本；坏数据返回 None 而不是抛给前端。"""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def create_app(cfg, conn_factory, clock, dispatcher=None, mcp=None) -> FastAPI:
    """`dispatcher` 只给 watchdog 用（告警由 api 直接发，不经过 jobs/agent，§7.6.4）。

    `mcp` 是 `mcp.westock.WestockMcp`（确认卡回调执行写操作时用）；None 时按
    `/state/westock.env` 惰性建一个——api 进程不共享 agent 的 `Deps`（Task 14）。
    """

    @asynccontextmanager
    async def lifespan(_app):
        import threading

        from agent.heartbeat import Heartbeat

        started: list = []
        # api 心跳：没有它，/api/status 的 services.api 永远是 null，前端状态栏一条红杠
        beat = Heartbeat(conn_factory, service="api", every_s=30, clock=clock)
        beat.beat()
        beat.start()
        started.append(beat)

        # watchdog：每 30 秒（不是 60：60+180 的最坏时延 239 秒会卡在 3 分钟验收线上）。
        # 只在**第一次** lifespan 起线程——重复进入（测试、reload）会叠出两条看门狗，
        # 每条各自读 push_log 去重，双发窗口就出来了。
        if dispatcher is not None and getattr(_app.state, "watchdog_started", False) is False:
            from api.watchdog import CHECK_EVERY_S, check

            _app.state.watchdog_started = True

            def loop():
                stop = threading.Event()
                while not stop.wait(CHECK_EVERY_S):
                    try:
                        check(conn_factory(), clock, dispatcher, cfg=cfg)
                    except Exception:          # noqa: BLE001 —— 看门狗自己不能死
                        pass

            threading.Thread(target=loop, name="watchdog", daemon=True).start()
        try:
            yield
        finally:
            beat.stop()

    app = FastAPI(title="stock-screener", lifespan=lifespan)
    # **访问口令**：`SS_WEB_PASS` 配了才生效；/healthz 与 /auth 免鉴权
    # （healthcheck 不带凭据；/auth 是登录入口本身）。
    from api.auth import middleware as _auth_middleware

    app.middleware("http")(_auth_middleware)
    # 工作日志路由（Task 13）：不挂的话 `GET /api/agent-log` 永不注册
    from api.agent_log import register as register_agent_log
    from chat.session import ChatSession
    from api.chat import register as register_chat

    register_agent_log(app, conn_factory, clock, cfg)
    # **单例**：ChatSession 在 api 进程启动时构造一次，路由不每次新建（D18）
    chat_session = ChatSession(conn_factory(), cfg, clock)
    register_chat(app, chat_session)
    snap = SnapshotCache(conn_factory)

    # ---- 依赖助手 --------------------------------------------------------

    def q(sql: str, args: tuple = ()) -> list:
        # 不 close：factory 可能返回进程级共享连接（测试与 api 心跳都用同一个）。
        # 生产侧由 `make_conn_factory(migrate_each=False)` 每次新建，句柄随请求作用域回收。
        return conn_factory().execute(sql, args).fetchall()

    def one(sql: str, args: tuple = ()):
        rows = q(sql, args)
        return rows[0] if rows else None

    def latest_pool_date() -> int | None:
        row = one("SELECT max(trade_date) FROM pool")
        return row[0] if row else None

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):     # pragma: no cover - 兜底
        return JSONResponse({"detail": str(exc)}, status_code=500)

    # ---- 行情 ------------------------------------------------------------

    @app.get("/api/quotes")
    def quotes(market: str | None = None):
        data = snap.get("market")
        rows = data["rows"]
        if market:
            # 快照写入侧不保证带 `market`，按代码前缀推断（与 core.market 同一份口径，
            # 别在 api 里再写一套前缀表）。推断不出的行在筛选时直接排除。
            rows = [r for r in rows if _row_market(r) == market]
        return {"as_of": data["as_of"], "rows": rows}

    @app.get("/api/quote/{code}")
    def quote(code: str):
        for scope in ("pool", "market"):
            row = snap.row_for(scope, code)
            if row is not None:
                return row
        raise HTTPException(status_code=404, detail=f"{code} 不在任何快照里")

    # ---- K 线 ------------------------------------------------------------

    @app.get("/api/kline/{code}")
    def kline(code: str, days: int = Query(120, ge=1, le=1700),
              adjust: str = Query("raw", pattern="^(raw|adj)$")):
        """默认 `raw`：图上要按用户买卖的真实价看，复权价只在算因子时用（§7.5.2）。"""
        if adjust == "adj":
            rows = q("SELECT date, open, high, low, close, volume FROM daily_bars_adj"
                     " WHERE code = ? ORDER BY date DESC LIMIT ?", (code, days))
        else:
            rows = q("SELECT date, open, high, low, close, volume FROM daily_bars"
                     " WHERE code = ? ORDER BY date DESC LIMIT ?", (code, days))
        rows = list(reversed(rows))
        return {"code": code, "adjust": adjust,
                "bars": [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
                          "close": r[4], "volume": r[5]} for r in rows]}

    # ---- 池子 ------------------------------------------------------------

    @app.get("/api/pool")
    def pool(market: str | None = None, date: int | None = None, limit: int = Query(500, ge=1, le=2000)):
        day = date or latest_pool_date()
        if day is None:
            return {"trade_date": None, "rows": []}
        sql = ("SELECT trade_date, market, code, rank, score, box_days, box_high_raw, box_low_raw,"
               " close_raw, box_vol_mean, pos_in_box, factors FROM pool WHERE trade_date = ?")
        # 前端 boxOverlay 读的是 *_raw 键：只回别名会让 K 线 Math.max(NaN) 整图消失
        args: list = [day]
        if market:
            sql += " AND market = ?"
            args.append(market)
        rows = q(sql + " ORDER BY rank LIMIT ?", tuple(args) + (limit,))
        snap_pool = snap.get("pool")
        prev_close_of = {r.get("code"): r.get("prev_close")
                         for r in snap_pool.get("rows", []) if r.get("code")}
        return {"trade_date": day, "rows": [
            {"trade_date": r[0], "market": r[1], "code": r[2], "rank": r[3], "score": r[4],
             "box_days": r[5], "box_high": r[6], "box_low": r[7], "close": r[8],
             "box_high_raw": r[6], "box_low_raw": r[7], "close_raw": r[8],
             "prev_close": prev_close_of.get(r[2]),
             "box_vol_mean": r[9], "pos_in_box": r[10],
             # factors 在库里是 JSON 文本；原样丢给前端会让 Pool.tsx 的 factorsOf 拿到字符串
             "factors": _json_or_none(r[11])} for r in rows]}

    # ---- 板块 ------------------------------------------------------------

    @app.get("/api/sector")
    def sector():
        data = snap.get("sector")
        rows = []
        for r in data["rows"]:
            rows.append({"code": r.get("code"), "name": r.get("name"), "chg": r.get("chg"),
                         "price": r.get("price"), "prev_close": r.get("prev_close"),
                         "members": r.get("members"), "limit_up_count": r.get("limit_up_count"),
                         "leader": r.get("leader")})
        rows.sort(key=lambda x: (x["chg"] is None, -(x["chg"] or 0)))
        return {"as_of": data["as_of"], "rows": rows}

    @app.get("/api/sector/{code}/members")
    def sector_members(code: str):
        rows = q("SELECT s.code, m.name FROM sector_member s"
                 " LEFT JOIN stock_meta m ON m.code = s.code WHERE s.sector = ?"
                 " ORDER BY s.code", (code,))
        return {"sector_code": code, "rows": [{"code": r[0], "name": r[1]} for r in rows]}

    # ---- 设置（只读快照）--------------------------------------------------

    @app.get("/api/settings")
    def settings():
        return {"strategy": _yaml(Path("config/strategy.yaml")),
                "markets": _yaml(Path("config/markets.yaml"))}

    # ---- 自选 ------------------------------------------------------------

    @app.get("/api/watch")
    def watch_list():
        rows = q("SELECT code, added_at, note, pct_threshold FROM watch_stock ORDER BY added_at")
        return {"rows": [{"code": r[0], "added_at": r[1], "note": r[2],
                          "pct_threshold": r[3]} for r in rows]}

    def _same_origin(request: Request) -> None:
        # 只认 https 精确 Origin：裸 host 与 http:// 都是「同源校验被绕过」，
        # 拿 http:// 或 host 放行等于把 CSRF 防护交给网络层。
        origin = request.headers.get("origin")
        host = str(cfg.notify.public_host).strip()
        if not origin or origin != f"https://{host}":
            raise HTTPException(status_code=400, detail="同源校验失败")

    @app.post("/api/watch")
    async def watch_add(request: Request):
        _same_origin(request)
        body = await request.json()
        code = (body or {}).get("code")
        if not code or not isinstance(code, str) or len(code) < 6:
            raise HTTPException(status_code=422, detail="code 不合法")
        note = (body or {}).get("note")
        pct = (body or {}).get("pct_threshold")
        if note is not None and not isinstance(note, str):
            raise HTTPException(status_code=422, detail="note 必须是字符串")
        # `True` 也是 int 的子类：不单独拒掉，bool 会被存成 1.0 当阈值用
        if pct is not None and (isinstance(pct, bool) or not isinstance(pct, (int, float))
                                or pct != pct or pct in (float("inf"), float("-inf"))):
            raise HTTPException(status_code=422, detail="pct_threshold 必须是有限数字")
        conn = conn_factory()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO watch_stock(code, added_at, note, pct_threshold, source)"
                " VALUES (?, ?, ?, ?, 'manual')",
                (code, clock.now().isoformat(), note, pct))
        return {"ok": True, "code": code}

    @app.delete("/api/watch/{code}")
    def watch_remove(code: str, request: Request):
        _same_origin(request)
        conn = conn_factory()
        with conn:
            n = conn.execute("DELETE FROM watch_stock WHERE code = ?", (code,)).rowcount
        return {"ok": True, "removed": n}

    # ---- 信号 ------------------------------------------------------------

    @app.get("/api/signals")
    def signals(market: str | None = None, date: int | None = None, kind: str | None = None):
        sql = ("SELECT id, ts, trade_date, market, code, kind, level, seq, read_at FROM signals"
               " WHERE 1=1")
        args: list = []
        if market:
            sql += " AND market = ?"
            args.append(market)
        if date:
            sql += " AND trade_date = ?"
            args.append(date)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        rows = q(sql + " ORDER BY ts DESC, id DESC", tuple(args))
        return {"rows": [{"id": r[0], "ts": r[1], "trade_date": r[2], "market": r[3], "code": r[4],
                          "kind": r[5], "level": r[6], "seq": r[7], "read_at": r[8]} for r in rows]}

    # ---- 回调 ------------------------------------------------------------

    @app.post("/api/hook/signals/{signal_id}/read")
    def hook_read(signal_id: int, t: str = Query("")):
        """验签失败一律 403 **且不改库**；成功只写 `read_at`。"""
        conn = conn_factory()
        row = conn.execute("SELECT trade_date FROM signals WHERE id = ?", (signal_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="signal 不存在")
        secret = ensure_secret(conn)
        from notify.hmac_sign import day_end_ts

        exp = day_end_ts(int(row[0]))
        # 过期判断必须用注入的 `clock`：`is_expired` 默认读系统时间，测试与回放都会判错
        if not verify(secret, "read", signal_id, exp, t) or is_expired(exp, clock.now().timestamp()):
            raise HTTPException(status_code=403, detail="签名或有效期不通过")
        with conn:
            conn.execute("UPDATE signals SET read_at = COALESCE(read_at, ?) WHERE id = ?",
                         (clock.now().isoformat(), signal_id))
        return {"ok": True}

    # ---- 确认卡回调（D21 / 批2 Task 6）-----------------------------------
    #
    # 4 个 westock 写工具**只能**从这里落地：agent / chat 回合里模型调用它们只会拿到
    # `needs_confirm`，真正的执行发生在用户点「确认执行」之后。顺序是定死的：
    # ① 验签 + 过期（失败 403，**不改库**）② 条件更新 pending → confirmed
    # （rowcount==0 → 403，重复点击/已过期都不重复执行）③ 解析 pending_action
    # ④ 调 WestockMcp 的写方法 ⑤ 成功后才写审计行。
    # ④ 失败必须把 pending_status 回滚成 pending 并回 500 + 一条告警——否则用户点了
    # 一次失败之后再点永远是 403，这张卡彻底废掉。

    WRITE_TOOLS = ("mcp_add_watch", "mcp_remove_watch", "mcp_set_alert", "mcp_cancel_alert")

    def _westock():
        obj = getattr(app.state, "mcp", None)
        if obj is None:
            from mcp.westock import WestockMcp

            obj = WestockMcp()
            app.state.mcp = obj
        return obj

    app.state.mcp = mcp

    def _rollback_pending(conn, turn_id: int, tool: str, reason: str) -> None:
        with conn:
            conn.execute("UPDATE agent_turn SET pending_status='pending'"
                         " WHERE id=? AND pending_status='confirmed'", (turn_id,))
        if dispatcher is not None:
            dispatcher.alert("confirm", f"确认执行失败（{tool}）：{reason}", "P1")

    @app.post("/api/hook/agent/{turn_id}/confirm")
    def hook_agent_confirm(turn_id: int, t: str = Query(""), exp: int = Query(0)):
        conn = conn_factory()
        # ① 验签 + 过期。**先查确认卡是否真的发过**：没有 `confirm-{turn_id}` 的
        # push_log 行就说明这不是我们推出的卡，签名再对也不执行。
        card = conn.execute(
            "SELECT 1 FROM push_log WHERE seq_id = ? AND kind = 'confirm_card' LIMIT 1",
            (f"confirm-{turn_id}",)).fetchone()
        if card is None:
            raise HTTPException(status_code=404, detail="确认卡不存在")
        secret = ensure_secret(conn)
        if not verify(secret, "confirm", turn_id, exp, t) or is_expired(exp, clock.now().timestamp()):
            raise HTTPException(status_code=403, detail="签名或有效期不通过")
        # ② 条件更新：rowcount==0 → 已确认/已过期，直接 403
        with conn:
            changed = conn.execute(
                "UPDATE agent_turn SET pending_status='confirmed'"
                " WHERE id=? AND pending_status='pending'", (turn_id,)).rowcount
        if changed != 1:
            raise HTTPException(status_code=403, detail="该确认已处理或已过期")
        # ③ 解析 pending_action（唯一格式：{"tool": ..., "args": {...}}）
        row = conn.execute("SELECT pending_action FROM agent_turn WHERE id=?",
                           (turn_id,)).fetchone()
        tool = ""
        try:
            action = json.loads(row[0] or "")
            tool = str(action["tool"])
            args = action.get("args") or {}
            if tool not in WRITE_TOOLS or not isinstance(args, dict):
                raise ValueError("不是可执行的写操作")
        except (TypeError, ValueError, KeyError) as exc:
            _rollback_pending(conn, turn_id, tool or "?", f"pending_action 不可解析：{exc}")
            raise HTTPException(status_code=500, detail="pending_action 不可解析") from exc
        # ④ 执行
        try:
            env = getattr(_westock(), tool[len("mcp_"):])(**args)
        except Exception as exc:                                  # noqa: BLE001
            _rollback_pending(conn, turn_id, tool, type(exc).__name__)
            raise HTTPException(status_code=500, detail="写操作执行异常") from exc
        if not (isinstance(env, dict) and env.get("ok")):
            reason = (env or {}).get("reason") if isinstance(env, dict) else None
            _rollback_pending(conn, turn_id, tool, str(reason))
            raise HTTPException(status_code=500, detail="写操作失败")
        # ⑤ 审计行：**不是推送**（kind='confirm_result'，日熔断与 notify_ok 都排除它）
        with conn:
            conn.execute(
                "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body,"
                " event_id, signal_id, trade_date, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (clock.now().isoformat(), args.get("code"), "confirm_result", "P2",
                 f"confirm-result-{turn_id}", 1, f"{tool} 已执行", None, None,
                 clock.today(), "accepted"))
        return {"ok": True, "tool": tool, "args": args}

    # ---- 认证页（D25）-----------------------------------------------------

    app.state.auth_tokens: dict[str, float] = {}
    app.state.auth_sessions: dict[str, float] = {}
    app.state.auth_pending: dict[str, dict] = {}
    # 一次性 token（经隧道的登录链接）消费后换成的短时 session：
    # 轮询接口只认它，TTL ≤ 5 分钟；一次性 token 本身在 /auth 时就被 pop 掉。
    _SESSION_TTL_S = 300

    def _issue_session() -> str:
        session = secrets.token_urlsafe(16)
        now = time.time()
        # 顺手清掉过期 session，避免进程常驻时无限增长
        for tok, exp in list(app.state.auth_sessions.items()):
            if exp <= now:
                app.state.auth_sessions.pop(tok, None)
        app.state.auth_sessions[session] = now + _SESSION_TTL_S
        return session

    def _valid_session(token: str | None) -> bool:
        if not token:
            return False
        exp = app.state.auth_sessions.get(token)
        return exp is not None and exp > time.time()

    @app.get("/auth", response_class=HTMLResponse)
    def auth_page(request: Request, token: str | None = None):
        """`/auth` 要么来自 127.0.0.1（SSH 端口转发），要么带一次性 token（经隧道进来）。"""
        client_ip = request.client.host if request.client else ""
        if client_ip in ("127.0.0.1", "::1"):
            return HTMLResponse(_auth_html(_issue_session()))
        # 用后即焚（pop）：一次性链接被截图/日志泄露后不能再用第二次
        if token:
            exp = app.state.auth_tokens.pop(token, 0)
            if exp > time.time():
                return HTMLResponse(_auth_html(_issue_session()))
        raise HTTPException(status_code=403, detail="需要本机访问或一次性的 /auth 链接")

    @app.post("/api/auth/start")
    def auth_start(request: Request):
        client_ip = request.client.host if request.client else ""
        if client_ip not in ("127.0.0.1", "::1"):
            raise HTTPException(status_code=403, detail="只允许本机发起登录")
        token = secrets.token_urlsafe(16)
        app.state.auth_tokens = {token: time.time() + 300}     # TTL 5 分钟，旧码立即作废
        app.state.auth_pending = {"token": token, "state": "waiting"}
        return {"token": token, "url": f"https://{cfg.notify.public_host}/auth?token={token}"}

    @app.get("/api/auth/status")
    def auth_status(token: str | None = None):
        # status 也属于 /api/auth/*：没有有效 session 一律 403，别再用 200+waiting 糊弄。
        if not _valid_session(token):
            raise HTTPException(status_code=403, detail="需要有效的登录 session")
        state = (app.state.auth_pending or {}).get("state", "idle")
        return {"state": state, "ok": state == "confirmed"}

    # ---- 心跳 ------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        # 带出版本：排障第一个问题就是「线上跑的是哪一版」，不该只能翻镜像 tag。
        # `/healthz` 免鉴权（docker healthcheck 不带凭据），版本号本身不是秘密。
        return {"ok": True, "version": VERSION}

    @app.get("/api/status")
    def status():
        services: dict[str, dict] = {}
        for name in ("api", "jobs", "agent"):
            row = one("SELECT last_beat_at FROM service_heartbeat WHERE service = ?", (name,))
            if row is None:
                services[name] = {"last_beat_at": None, "stale_s": None}
            else:
                try:
                    delta = (clock.now() - datetime.fromisoformat(row[0])).total_seconds()
                except (TypeError, ValueError):
                    delta = None
                services[name] = {"last_beat_at": row[0],
                                  "stale_s": int(delta) if delta is not None else None}

        counts = {t: one(f"SELECT count(*) FROM {t}")[0] for t in ("event_log", "signals", "push_log")}

        day = latest_pool_date()
        pools: dict[str, int] = {}
        if day is not None:
            pools = {r[0]: r[1] for r in q("SELECT market, count(*) FROM pool WHERE trade_date = ?"
                                           " GROUP BY market", (day,))}

        detail_row = one("SELECT detail FROM service_heartbeat WHERE service = 'jobs'")
        upstream = {s: 0 for s in SCOPES}
        if detail_row is not None and detail_row[0]:
            try:
                import json

                raw = (json.loads(detail_row[0]) or {}).get("calls_by_scope") or {}
                # 只认七个已知 scope：库里的额外键不该通过接口透出（契约漂移）
                upstream.update({k: int(v) for k, v in raw.items() if k in upstream})
            except ValueError:
                pass

        agent_row = one("SELECT detail FROM service_heartbeat WHERE service = 'agent'")
        budget_exhausted = False
        if agent_row is not None and agent_row[0]:
            try:
                import json

                budget_exhausted = bool((json.loads(agent_row[0]) or {}).get("budget_exhausted"))
            except ValueError:
                budget_exhausted = False

        # 生产者（Dispatcher._channel_down）把原因写在 `error` 列，`body` 是 NULL：
        # 只取 body 会让 notify_last_error 永远是 null
        last_down = one("SELECT ts, COALESCE(body, error) FROM push_log"
                        " WHERE kind = 'push_channel_down' AND status = 'failed'"
                        " ORDER BY ts DESC LIMIT 1")
        last_ok = one("SELECT max(ts) FROM push_log WHERE kind NOT IN ('push_channel_down','confirm_result')"
                      " AND status = 'accepted'")
        notify_ok = True
        notify_last_error = None
        if last_down is not None:
            notify_last_error = last_down[1]
            notify_ok = not (last_ok is None or str(last_ok[0] or "") < str(last_down[0]))

        # 首页「agent 最后活动 N 分钟前」：**口径与 watchdog 规则 3 同一份**——
        # 都是 `agent_turn` 最后一行的时间，不要一处读心跳、一处读回合
        last_turn = one("SELECT max(ts) FROM agent_turn")
        agent_last_turn_at = last_turn[0] if last_turn else None
        # 从没产生过回合时用启动宽限起点算龄，与 watchdog 规则 3 同一口径；
        # 直接给个 15.0 会让首页在「一次回合都没有」时假装正常
        agent_turn_stale_min = None
        try:
            ts = datetime.fromisoformat(agent_last_turn_at) if agent_last_turn_at else None
        except (TypeError, ValueError):
            ts = None
        if ts is not None:
            if ts.tzinfo is None:
                from core.clock import SH as _SH

                ts = ts.replace(tzinfo=_SH)
            agent_turn_stale_min = round(
                (clock.now() - ts.astimezone(clock.now().tzinfo)).total_seconds() / 60, 1)
        else:
            first = one("SELECT value FROM job_state WHERE name = 'watchdog.first_check'")
            if first and first[0]:
                try:
                    started = datetime.fromisoformat(str(first[0]))
                    if started.tzinfo is None:
                        from core.clock import SH as _SH

                        started = started.replace(tzinfo=_SH)
                    agent_turn_stale_min = round(
                        (clock.now() - started.astimezone(clock.now().tzinfo))
                        .total_seconds() / 60, 1)
                except (TypeError, ValueError):
                    agent_turn_stale_min = None

        # **agent 是否该在活动**：agent 只在交易时段唤醒（09:15–15:05），
        # 收盘后没有新回合是预期行为。前端只按分钟数标红会让首页每天
        # 收盘后一路红到天亮（实机验证：152 分钟前标红）。
        # **必须与 agent 的时段规则一致**（`agent/main.py::_in_session`）：
        # 它排除 11:30–13:00 午休。接口不算午休的话，12:00 会返回 in_session=true，
        # 前端就按「交易时段内、超 15 分钟没回合」标红——正常午休被误报成停止。
        now = clock.now()
        hm = now.strftime("%H:%M")
        in_session = ("09:15" <= hm <= "15:05" and not ("11:30" <= hm < "13:00")
                      and TradingCalendar(conn_factory()).is_trading_day(
                          int(now.strftime("%Y%m%d"))))

        flag = one("SELECT value FROM job_state WHERE name = 'maintenance.flag'")
        return {
            # 版本号：给排障脚本与人工核对用（前端目前不读它），
            # 与 `/healthz` 同一个来源 `core/version.py`
            "version": VERSION,
            "services": services,
            "in_session": in_session,
            "counts": counts,
            "pools": pools,
            "upstream_1h": upstream,
            "notify_ok": notify_ok,
            "notify_last_error": notify_last_error,
            "disk_used_pct": _disk_used_pct("data"),
            "maintenance": bool(flag) and str(flag[0]) == "1",
            "budget_exhausted": budget_exhausted,
            # 前端首页状态栏：`agent_last_turn_at` 为 ISO 串，`agent_turn_stale_min`
            # 是「距今多少分钟」（与 watchdog 规则 3 的 15 分钟阈值同一口径）
            "agent_last_turn_at": agent_last_turn_at,
            "agent_turn_stale_min": agent_turn_stale_min,
        }

    # ---- 静态资源与 SPA 回落 ---------------------------------------------
    # 未知 GET 一律回落 index.html（前端自己路由），但**四类服务端路径不能回落**：
    # /api/*（前端会拿到 200 + HTML 而不是 404/JSON）、/healthz、/auth（二维码页会被覆盖成空白）、
    # /api/auth/*。少了这个排除，D25 的登录页与验收脚本都会拿到一堆 HTML。
    _dist = Path("web/dist")
    _server_prefixes = ("/api", "/healthz", "/auth")      # 前缀不带尾斜杠，统一按路径段比

    @app.get("/{full_path:path}")
    def spa(full_path: str, request: Request):
        if request.method != "GET":
            raise HTTPException(status_code=405)
        path = "/" + full_path
        # 按**路径段**匹配：`startswith("/auth")` 会把前端的 /auth-log、/authors/1 也吞掉
        if any(path == p or path.startswith(p + "/") for p in _server_prefixes):
            raise HTTPException(status_code=404, detail="不是前端路由")
        asset = (_dist / full_path).resolve()
        if full_path and _dist.exists() and asset.is_file() and str(asset).startswith(str(_dist.resolve())):
            return FileResponse(asset)
        index = _dist / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="前端产物不存在")


    return app


def _row_market(row: dict) -> str | None:
    """快照行的市场：行里自带就用，否则按代码前缀推断（`/api/quotes?market=` 用）。"""
    market = row.get("market")
    if market:
        return market
    code = row.get("code")
    return classify_market(code) if isinstance(code, str) else None


def _disk_used_pct(path: str) -> float:
    """`data/` 所在文件系统的占用百分比（`os.statvfs`；容器里也拿得到）。"""
    try:
        st = os.statvfs(path)
    except OSError:
        return 0.0
    return 100.0 * (st.f_blocks - st.f_bavail) / st.f_blocks if st.f_blocks else 0.0


def _yaml(path: Path) -> dict:
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, Exception):                    # noqa: BLE001 —— 读不到就返回空，不让设置页 500
        return {}


_AUTH_HTML = """<!doctype html><meta charset="utf-8"><title>登录</title>
<h1>扫码登录</h1><p id="s">等待确认…</p>
<script>
// 页面在 /auth 时消费一次性链接，轮询用服务端下发的短时 session token
const TOKEN = "%TOKEN%";
async function poll(){const r = await fetch('/api/auth/status?token=' + encodeURIComponent(TOKEN));
 const j = await r.json(); document.getElementById('s').textContent = j.ok ? '已登录' : '等待确认…';
 if(!j.ok) setTimeout(poll, 1200);} poll();
</script>"""


def _auth_html(token: str = "") -> str:
    return _AUTH_HTML.replace("%TOKEN%", token)
