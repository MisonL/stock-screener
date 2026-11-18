"""通道 B 与两段式第二段的推送出口（计划 Task 12，spec §7.2 / §7.2.2 / D2 / D4 / D21）。

**第二段是同一 `X-Sequence-ID` 的更新，不是新通知**：用户手机上看到的是原消息被
「追加」而不是多出一条。所以第二段必须：
1. 用**同一个** `seq_id` 发；
2. 正文以**第一段原文一字不改**开头（`\n\n——\n` + 解读）——只发解读会丢掉原文里的
   涨停价/可买量，那正是用户最先看的那行（§7.2.2）；
3. 在 `push_log` 里与第一段**同 `seq_id` 的第二行**（`ts` 不同，验收口径就是两行 ts 差）。

第一段的定位：事件类按 `push_log.event_id`（D2，索引 `idx_push_event`），开盘简报没有
`event_id`，按 `seq_id = open-{trade_date}` 定位。
"""
from __future__ import annotations

import json
import logging
import os

from core.clock import SH
from notify.policy import (NON_PUSH_KINDS, agent_notes_today, channel_b_today,
                           pushed_today, recent_independent_push)
from notify.templates import Message, render_confirm_card

AGENT_NOTE_KIND = "agent_note"       # channel_b_today / recent_independent_push 都按它识别
CONFIRM_KIND = "confirm_card"
OPEN_SEQ = "open-{trade_date}"
DRILL_ENV = "SS_DRILL"
log = logging.getLogger(__name__)
ACTION_LABEL = {
    "mcp_add_watch": "加入自选",
    "mcp_remove_watch": "移出自选",
    "mcp_set_alert": "设置提醒",
    "mcp_cancel_alert": "取消提醒",
}


def _name_of(code6: str | None) -> str:
    """标题里的股票名来自 `push_log.code`（就是 6 位代码）。

    查不到名字也不丢方向词：`title_for` 里的「放量突破 / 跌破箱体下沿」是**方向**决定的，
    名字只是前缀。宁可用代码，也不能让标题与正文方向相矛盾。
    """
    return str(code6 or "")


def code6_of(code: str | None) -> str:
    """带前缀 code → 6 位。`sh600519` 拼进 `seq_id` 会与模板的 `sig-600519-…` 不一致。"""
    text = str(code or "")
    return text[2:] if text[:2] in ("sh", "sz", "bj") else text


def action_desc(pending_action) -> str:
    """把 `{"tool", "args"}` 变成一句人话。未知工具**不抛**——确认卡照样要发出去。"""
    if isinstance(pending_action, str):
        try:
            pending_action = json.loads(pending_action)
        except (TypeError, ValueError):
            return pending_action
    if not isinstance(pending_action, dict):
        return str(pending_action)
    tool = str(pending_action.get("tool") or "")
    args = pending_action.get("args") or {}
    label = ACTION_LABEL.get(tool)
    if label is None:
        return f"{tool} {json.dumps(args, ensure_ascii=False)}"
    target = args.get("code") or args.get("symbol") or json.dumps(args, ensure_ascii=False)
    return f"{label} {target}"


def drill_enabled() -> bool:
    """故障演练（RUNBOOK §11）：`SS_DRILL=1` 时推送只写日志。

    在**发送出口**（`_send`）与**四个推送方法入口**两处拦：出口拦住了所有真正
    触达 ntfy 的路径，入口保证连 `push_log` 行都不写——演练数据不能进真实推送统计。
    """
    return (os.environ.get(DRILL_ENV) or "").strip() == "1"


class AgentPusher:
    def __init__(self, conn, ntfy, cfg, clock):
        self.conn = conn
        self.ntfy = ntfy
        self.cfg = cfg
        self.clock = clock
        # 每次调用时读（而不是构造时读一次）：演练用 `docker compose run -e` 注入，
        # 测试里 monkeypatch 也直接生效；代价只是一次 os.environ 查询。
        self.drill = drill_enabled

    def _drill_skip(self, method: str, **fields) -> bool:
        """演练中：记一行日志并返回 True（调用方据此返回 None，不写 `push_log`）。

        用**每个公开方法入口判断**而不是构造函数注入 `drill=True`：入口分支能在
        写 `push_log` 之前就返回，既不发送也不留演练行；构造时注入只能拦住「发」，
        拦不住「先写一行 pending 再不发」的统计污染。
        """
        if not self.drill():
            return False
        log.info("SS_DRILL=1 演练：%s 只记日志、不推送、不写 push_log %s", method, fields)
        return True

    # ---- 基础设施 --------------------------------------------------------

    def _first_segment(self, *, event_id=None, seq_id=None):
        sql = ("SELECT id, ts, code, kind, level, seq_id, segment, body, event_id, status,"
               " trade_date FROM push_log WHERE segment = 1 AND status = 'accepted'"
               " AND body IS NOT NULL")
        params: list = []
        if event_id is not None:
            sql += " AND event_id = ?"
            params.append(event_id)
        elif seq_id is not None:
            sql += " AND seq_id = ?"
            params.append(seq_id)
        else:
            return None
        row = self.conn.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
        if row is None:
            # **不猜**：第一段没落库（failed/pending）时硬发第二段，用户会看到一条
            # 没有原文的「解读」，而原文可能根本没到
            return None
        return {"id": row[0], "ts": row[1], "code": row[2], "kind": row[3], "level": row[4],
                "seq_id": row[5], "segment": row[6], "body": row[7], "event_id": row[8],
                "status": row[9], "trade_date": row[10]}

    def _insert(self, *, code, kind, level, seq_id, segment, body, event_id, signal_id,
                trade_date, status="pending"):
        cur = self.conn.execute(
            "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, event_id,"
            " signal_id, trade_date, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.clock.now().isoformat(), code, kind, level, seq_id, segment, body,
             event_id, signal_id, trade_date, status))
        self.conn.commit()
        return cur.lastrowid

    _fail_streak = 0

    def _channel_down(self) -> None:
        """连续 3 次发送失败 → 写一条 `push_channel_down` 留痕，api 据此报 `notify_ok=false`。

        与 `notify/dispatcher.py::_channel_down` 同一口径：**只写本地留痕行，不发推送**
        （发不出去的时候再"发一条说发不出去"没有意义）。`seq_id` 用时间戳，
        每条故障独立成行，便于在网页上看到"故障持续了多久"。
        """
        now = self.clock.now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status, error)"
                " VALUES (?, NULL, 'push_channel_down', 'P2', ?, 1, ?, 'failed', ?)",
                (now.isoformat(), f"pcd-agent-{int(now.timestamp())}", self.clock.today(),
                 f"agent 连续 {self._fail_streak} 次发布失败"),
            )
        self._fail_streak = 0

    def _send(self, row_id: int, msg: Message) -> str | None:
        """发出去并把 `ntfy_id`/status 回写。失败返回 None（清掉这条腾出 seq）。"""
        if self._drill_skip("_send", row_id=row_id, seq_id=msg.seq_id):
            # 兜底：即使某个调用方绕过了方法入口，也绝不触达 ntfy
            return None
        try:
            ntfy_id = self.ntfy.publish(msg)
        except Exception as exc:                       # noqa: BLE001
            with self.conn:
                self.conn.execute("UPDATE push_log SET status = 'failed', error = ? WHERE id = ?",
                                  (str(exc)[:200], row_id))
            self._fail_streak += 1
            # **agent 侧失败也要能被 `notify_ok` 看见**：`api/routes.py` 只认
            # `kind='push_channel_down'` 这一种行，而这个行原来只有
            # `Dispatcher._channel_down()` 会写。agent 走的 `AgentPusher` 是另一条
            # 发送路径（通道 B 便签、事件第二段、确认卡），它失败时只把行标 failed——
            # 结果：agent 整天发不出去，首页仍显示"推送正常"，用户只能靠手机没响自己猜。
            if self._fail_streak >= 3:
                self._channel_down()
            return None
        self._fail_streak = 0
        with self.conn:
            self.conn.execute("UPDATE push_log SET status = 'accepted', ntfy_id = ? WHERE id = ?",
                              (ntfy_id, row_id))
        return str(msg.seq_id or "")

    # ---- 事件第二段 ------------------------------------------------------

    def second_segment(self, event_id, decision, grounded) -> str | None:
        if self._drill_skip("second_segment", event_id=event_id):
            return None
        first = self._first_segment(event_id=event_id)
        if first is None:
            return None
        return self._append(first, decision, grounded)

    def open_second_segment(self, trade_date, decision, grounded) -> str | None:
        if self._drill_skip("open_second_segment", trade_date=trade_date):
            return None
        first = self._first_segment(seq_id=OPEN_SEQ.format(trade_date=trade_date))
        if first is None:
            return None
        return self._append(first, decision, grounded)

    def _append(self, first: dict, decision, grounded) -> str | None:
        text = getattr(grounded, "text", None) or getattr(decision, "body", "") or ""
        body = f"{first['body']}\n\n——\n{text}"
        msg = Message(title=_title(decision, _name_of(first["code"])), body=body,
                      priority=_priority(decision), seq_id=first["seq_id"])
        row_id = self._insert(code=first["code"], kind=first["kind"], level=first["level"],
                              seq_id=first["seq_id"], segment=2, body=body,
                              event_id=first["event_id"], signal_id=None,
                              trade_date=first["trade_date"])
        # signal_id 与第一段保持一致（第二段行不重新关联信号）
        self.conn.execute("UPDATE push_log SET signal_id = (SELECT signal_id FROM push_log"
                          " WHERE id = ?) WHERE id = ?", (first["id"], row_id))
        self.conn.commit()
        return self._send(row_id, msg)

    # ---- 通道 B ----------------------------------------------------------

    def agent_note(self, decision, grounded, code: str | None = None,
                   target: str | None = None) -> str | None:
        if self._drill_skip("agent_note", code=code, target=target):
            return None
        conn = self.conn
        today = self.clock.today()
        cap = int(self.cfg.agent.channel_b_daily_cap)

        # **额度检查、去重判断与 seq 认领必须在同一个写事务里。**
        # 分两步做时，jobs 的通道 A 与 agent 的通道 B 会各自读到 59/60 然后都放行，
        # 实际发到 61——SQLite 的写串行化救不了"事务外已经做出的额度决定"。
        # `BEGIN IMMEDIATE` 先拿写锁再读，读到的就是别人已提交的最新值。
        conn.execute("BEGIN IMMEDIATE")
        try:
            if channel_b_today(conn, today) >= cap:
                # **超限时只写 agent_turn，不写 push_log**：写 suppressed 行会污染 seq 计数
                conn.rollback()
                self._audit_cap("通道 B 日上限")
                return None
            total_cap = int(self.cfg.notify.daily_cap)
            used = pushed_today(conn, channel=None, exclude_segments=(2,),
                                exclude_kinds=NON_PUSH_KINDS, day=today)
            if used >= total_cap:
                conn.rollback()
                self._audit_cap("推送总日上限")
                return None

            # 同 code 同 kind 的独立推送刚发过 → 改成对原条追加第二段（§7.2 去重表）
            if code:
                # **不传 kind**：§7.2 的去重是「同一 code 最近有没有推过」，跨 kind 才算
                # 「刚说过这只票」。kind 过滤是批1 dispatcher 自己那条规则（避免
                # 「先突破后跌破」被压掉），两者不是一回事。
                prev = recent_independent_push(conn, code6_of(code),
                                               minutes=int(self.cfg.notify.dedupe_min),
                                               now=self.clock.now())
                if prev is not None:
                    # **只对真实事件第一段追加**：event_id 为 NULL 的（开盘简报、通道 B
                    # 自己、alert）second_segment(None) 永不命中，会把这条提醒无声吞掉
                    if (prev.event_id is not None and prev.segment == 1
                            and prev.status == "accepted"):
                        conn.rollback()          # 额度不占：这条并入原条
                        return self.second_segment(prev.event_id, decision, grounded)

            text = getattr(grounded, "text", None) or getattr(decision, "body", "") or ""
            seq_id = self._note_seq(code, target, today)
            row_id = self._insert(code=code6_of(code) if code else None, kind=AGENT_NOTE_KIND,
                                  level="P2", seq_id=seq_id, segment=1, body=text,
                                  event_id=None, signal_id=None, trade_date=today)
            conn.commit()                    # 先落 `pending` 占住额度
        except BaseException:
            conn.rollback()
            raise

        # **真正的发送放在事务外**：网络调用不该占着写锁
        msg = Message(title=_title(decision, _name_of(code6_of(code) if code else None)),
                      body=text, priority=_priority(decision), seq_id=seq_id)
        return self._send(row_id, msg)

    def _note_seq(self, code, target, today) -> str:
        n = agent_notes_today(self.conn, today) + 1
        if code:
            return f"note-{code6_of(code)}-{today}-{n}"
        if target and str(target).startswith("pt"):
            return f"note-sector-{target}-{today}-{n}"
        if target and str(target)[:2] in ("sh", "sz"):
            return f"note-index-{target}-{today}-{n}"
        # `code is None` 时**禁止拼空串**：空 code 会让 seq 失去唯一性
        market = self.cfg.enabled_markets()
        return f"note-market-{market[0] if market else 'cn_main'}-{today}-{n}"

    def _audit_cap(self, why: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision, why,"
                " fallback, market) VALUES (?,?,?,?,?,?,?,?)",
                (self.clock.now().isoformat(), "heartbeat", f"通道B被压 {why}", 1, "speak",
                 f"超{why}", 0, "cn_main"))

    # ---- 确认卡 ----------------------------------------------------------

    def confirm_card(self, turn_id: int, pending_action, grounded=None) -> str | None:
        from notify.hmac_sign import ensure_secret

        if self._drill_skip("confirm_card", turn_id=turn_id):
            # 也不能把回合标成 pending：演练里没人会点这张卡，标了就是永久待办
            return None
        host = getattr(self.cfg.notify, "public_host", None) or "localhost"
        ttl_h = getattr(getattr(self.cfg.agent, "pending_action_ttl_h", None), "__int__", None)
        ttl = int(getattr(self.cfg.agent, "pending_action_ttl_h", 24) or 24)
        exp_ts = int(self.clock.now().timestamp()) + ttl * 3600
        desc = action_desc(pending_action)
        secret = ensure_secret(self.conn)
        msg = render_confirm_card(turn_id, desc, host, secret, self.cfg, exp_ts)
        payload = pending_action if isinstance(pending_action, str) else json.dumps(
            pending_action, ensure_ascii=False)
        row_id = self._insert(code=None, kind=CONFIRM_KIND, level="P2",
                              seq_id=f"confirm-{turn_id}", segment=1, body=desc,
                              event_id=None, signal_id=None, trade_date=self.clock.today())
        self.conn.execute("UPDATE agent_turn SET pending_status = 'pending' WHERE id = ?"
                          " AND pending_status IS NULL", (turn_id,))
        self.conn.execute("UPDATE agent_turn SET pending_action = COALESCE(pending_action, ?)"
                          " WHERE id = ?", (payload, turn_id))
        self.conn.commit()
        return self._send(row_id, msg)


# ---------------------------------------------------------------- 小工具


def _title(decision, name: str = "") -> str:
    """标题跟着**结构化方向**走，不跟着模型自己写的 `title` 走。

    模型完全可能正文写「放量突破」而方向是 `breakdown`——原样采用它的标题，
    手机上就是自相矛盾的两句。`title_for` 是这条规则的唯一实现。
    """
    from agent.decision import title_for

    title = str(getattr(decision, "title", None) or "")
    direction = getattr(decision, "direction", "none") or "none"
    if name:
        return title_for(decision, name) or title or "盯盘提醒"
    # 没有股票名（心跳/市场级）时只有方向词能复用
    if direction == "breakout":
        return title or "放量突破"
    if direction == "breakdown":
        return title or "跌破箱体下沿"
    return title or ("🚨 盯盘提醒" if getattr(decision, "level", None) == "P0" else "盯盘提醒")


def _priority(decision) -> int:
    return {"P0": 5, "P1": 4}.get(getattr(decision, "level", None), 3)
