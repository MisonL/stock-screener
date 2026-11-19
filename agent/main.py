"""agent 主进程：交易时段内的四类唤醒（计划 Task 11）。

**record 只由主循环调用**：`asyncio.wait_for` 取消后 `run_turn` 里走不到写日志，
超时回合会零日志、事件永远滞留 `escalated`。

四种唤醒必须都入队——事件 / 心跳 / 闹钟 / 开盘简报第二段。缺一种就永久静默
（尤其是 `open`：没有它，开盘简报永远只有第一段）。
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import closing
from datetime import datetime, timedelta

from agent.attention import AttentionLayer
from agent.heartbeat import Heartbeat
from agent.journal import record as journal_record
from agent.loop import Deps, Trigger, render_trigger, run_with_deadline
from agent.schedule import (after_alarm, due_alarms, next_heartbeat, release_alarm, schedule_alarm,
                            set_fired_turn)
from core.clock import SH, SystemClock, TradingCalendar

SLEEP_OUT_OF_SESSION_S = 60
LOOP_EVERY_S = 2
# **回合级 deadline，不是单次调用超时**（后者见 `config/llm.yaml` 的
# `timeouts.agent_call_s`）。一条铁律：**deadline ≥ 单次调用超时 × 2**。
# 一个回合至少要两次 LLM 调用（先决定查什么、拿到工具结果后再出决策），
# deadline 只够一次调用时，每轮都死在"还没等回来就到点"上。
# 原来的 25/60 是按单次 20 秒配的（25 < 20×2，本来就不够），
# 中转站排队超过 20 秒后整个交易日 100% 回合超时。
#   · 事件回合：2 轮 × 40s = 80s，给 90s
#   · 心跳/闹钟回合：3 轮 × 40s = 120s，**恰好等于密集期心跳间隔**，
#     再长会让心跳互相堆积
EVENT_ROUNDS_BUDGET, EVENT_DEADLINE_S = 3, 90
OTHER_ROUNDS_BUDGET, OTHER_DEADLINE_S = 6, 120
ESCALATED_RECYCLE_MIN = 30
CONFIRM_EXPIRE_H = 24
OPEN_TURN_AT = (9, 26)
SHADOW_WHY = "只记录不升级"
HEARTBEAT_SERVICE_DEFAULT = "agent"


def _setup_logging() -> None:
    """把 INFO 日志打到 stdout。

    **不加这一步，生产容器 `docker logs` 是空的**：全仓只有 `getLogger()`，
    没有 `basicConfig`，根 logger 无 handler 且默认级别 WARNING，
    INFO（抓取限速、EOD 结果、回合决策）全部被丢弃，故障时只能翻数据库。
    """
    import logging
    import sys

    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    # **APScheduler 每秒一条 INFO 会把有用日志淹掉**（实测 1 秒 2 行）：
    # 调到 WARNING，只在真出错时出声。
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    # **httpx 的 INFO 会把每个请求的完整 URL 打出来**（含几百个股票代码），
    # 实测一份 20 分钟的日志几乎全是这种行，`docker logs` 没法用。
    # 只在真出错（4xx/5xx 的 warning/error 由业务层记）时出声。
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def shadow_enabled(cfg) -> bool:
    """本次进程是否处于影子模式（spec §10：上线头两周「只记录不升级」）。

    **缺省按 True**：配置里漏了 `agent.attention.shadow_mode` 时安全方向是「不发」——
    宁可少说两句，也不能在观测期让手机响起来。与 `attention.py` 的判定同一口径。
    """
    attention = getattr(getattr(cfg, "agent", None), "attention", None)
    return getattr(attention, "shadow_mode", True) is not False


def heartbeat_service() -> str:
    """心跳服务名：`HEARTBEAT_SERVICE` 覆盖，缺省 `agent`（RUNBOOK §11 演练用）。"""
    return (os.environ.get("HEARTBEAT_SERVICE") or "").strip() or HEARTBEAT_SERVICE_DEFAULT


def next_session_start(now: datetime) -> datetime:
    base = now.astimezone(SH)
    target = base.replace(hour=9, minute=15, second=0, microsecond=0)
    return target if target > base else target + timedelta(days=1)


def in_session(now: datetime) -> bool:
    """盘中时段。**午休（11:30–13:00）不算**：11:30–13:00 与 15:05 后都休眠到下一个
    时段起点，否则午休期间每 2 秒白跑一次主循环（每轮都要查库、判闹钟、判开盘回合）。"""
    hm = now.astimezone(SH).strftime("%H:%M")
    if "11:30" <= hm < "13:00":
        return False
    return "09:15" <= hm <= "15:05"


def should_wake(conn, now: datetime) -> bool:
    """今天该不该醒。用 `is_trading_day`，不是 `contains`——当日 bar 15:05 才 append。"""
    if not in_session(now):
        return False
    try:
        return TradingCalendar(conn).is_trading_day(int(now.astimezone(SH).strftime("%Y%m%d")))
    except (LookupError, ValueError):
        return False


def _first_target(trigger):
    """事件回合的标的：拿组内第一个带 target 的事件。"""
    for ev in getattr(trigger, "events", None) or []:
        if getattr(ev, "target", None):
            return ev.target
    return None


def _default_market(conn, cfg) -> str:
    row = conn.execute(
        "SELECT market FROM event_log ORDER BY id DESC LIMIT 1").fetchone()
    if row and row[0]:
        return row[0]
    enabled = cfg.enabled_markets()
    return enabled[0] if enabled else "cn_main"


class AgentMain:
    def __init__(self, deps, clock=None):
        self.deps = deps
        self.clock = clock or SystemClock()
        self.attention = AttentionLayer(deps.conn, deps.cfg, self.clock)
        # 进程启动时集中读一次；运行中改 YAML 不会偷偷切换「是否允许花钱/推送」。
        self.shadow_mode = shadow_enabled(deps.cfg)
        # 影子期心跳的留痕水位（= 已经审计过的那个到期时刻）：容器重启后
        # `next_heartbeat_at` 是 None，重新排出的「同一跳」若再记一遍就会重复；
        # 水位取自库里的行，进程重启也幂等。
        row = deps.conn.execute(
            "SELECT context_digest FROM agent_turn WHERE wake_kind = 'heartbeat'"
            " AND decision = 'shadow' ORDER BY id DESC LIMIT 1").fetchone()
        digest = (row[0] or "") if row is not None else ""
        self._shadow_beat_at = (digest.split("due_at=", 1)[1].strip()
                                if "due_at=" in digest else None)
        self.next_heartbeat_at = None
        self.budget_exhausted = False
        self._last_day = self.clock.today()

    def _trigger(self, wake_kind, *, group=None, events=None, rounds, deadline, alarm_id=None,
                 target=None, note=""):
        market = None
        if group is not None and getattr(group, "market", None):
            market = group.market
        else:
            row = self.deps.conn.execute(
                "SELECT market FROM event_log WHERE trade_date = ? ORDER BY id DESC LIMIT 1",
                (self.clock.today(),)).fetchone()
            market = row[0] if row and row[0] else _default_market(self.deps.conn, self.deps.cfg)
        channel = getattr(group, "channel", None) if group is not None else None
        if target is None:
            ev = next((e for e in (events or []) if getattr(e, "target", None)), None)
            target = getattr(ev, "target", None) if ev is not None else None
        trig = Trigger(wake_kind=wake_kind, group=group,
                       events=list(events or []), channel=channel, market=market,
                       rounds_budget=rounds, deadline_s=deadline, alarm_id=alarm_id)
        # 闹钟唤醒要带回标的与备注，否则「排了闹钟」的回访没有上下文
        trig.target = target
        trig.note = note
        return trig

    async def _run(self, trigger):
        try:
            result = await run_with_deadline(trigger, self.deps)
        except BaseException:
            # **抛异常时也要把闹钟退回去**。`run_with_deadline` 只把
            # `asyncio.TimeoutError` 转成结果，其它异常（LLM 客户端炸了、
            # 磁盘写满、协程被取消）会直接往上抛——那条路径上 `journal_record`
            # 与 `release_alarm` 都不会执行，而闹钟在 `after_alarm` 里已经被
            # 认领成 `fired` 了，于是这次回访永久丢失，连一行日志都没有。
            if trigger.alarm_id is not None:
                release_alarm(self.deps.conn, trigger.alarm_id)
            raise
        turn_id = journal_record(self.deps.conn, trigger, result, self.clock)
        if trigger.alarm_id is not None:
            set_fired_turn(self.deps.conn, trigger.alarm_id, turn_id)
            # **回合失败要把闹钟退回去**：`after_alarm` 在跑之前就认领成 `fired`，
            # 而 LLM 挂掉/预算耗尽/超时都会让这一轮白跑。不放回 `pending` 的话，
            # 模型说的"我 30 分钟后回来看"就静默作废（清理任务只处理 3 天前的
            # pending，不会补跑）。超时不算失败——它已经花掉预算跑过了。
            if result.decision.kind in ("error", "invalid"):
                release_alarm(self.deps.conn, trigger.alarm_id)
        if result.decision.pending_action and self.deps.pusher is not None:
            # **把理由带进卡里**：沉默回合（`speak=false`）不推正文，确认卡是用户
            # 唯一看到的东西。只给一行"加入自选 sh600519"的话，用户不知道该不该点。
            # `why_silent` 是沉默回合的必填理由（见 `agent/decision.py`），
            # `body`/`grounded.text` 覆盖说话回合。
            reason = (result.decision.why_silent or result.decision.body
                      or getattr(result.grounded, "text", "") or "")
            self.deps.pusher.confirm_card(turn_id, result.decision.pending_action,
                                          result.grounded, reason=reason)
        if result.decision.schedule_next:
            due = self.clock.now() + timedelta(minutes=result.decision.schedule_next)
            # **标的必须传**：target 为 NULL 的闹钟醒来后无从知道要回访哪只票
            schedule_alarm(self.deps.conn, turn_id, due.isoformat(),
                           trigger.market or "cn_main",
                           getattr(trigger, "target", None) or _first_target(trigger),
                           result.decision.schedule_note or "",
                           now=self.clock.now())
        if getattr(result, "why", None) == "agent 日预算已用完":
            self.budget_exhausted = True
        # 预算写库失败也要进「预算不可用」：否则下一唤醒继续请求、继续花
        try:
            row = self.deps.conn.execute(
                "SELECT value FROM job_state WHERE name = 'budget.write_failed:agent'"
            ).fetchone()
            if row and row[0]:
                self.budget_exhausted = True
        except Exception:                                  # noqa: BLE001
            pass
        return turn_id

    def heartbeat_housekeeping(self):
        conn = self.deps.conn
        now = self.clock.now()
        deadline = (now - timedelta(minutes=ESCALATED_RECYCLE_MIN)).isoformat()
        conn.execute("UPDATE event_log SET agent_status = 'pending_retry'"
                     " WHERE agent_status = 'escalated' AND escalated_at < ?", (deadline,))
        cutoff = (now - timedelta(hours=CONFIRM_EXPIRE_H)).isoformat()
        conn.execute("UPDATE agent_turn SET pending_status = 'expired'"
                     " WHERE pending_status = 'pending' AND ts < ?", (cutoff,))
        conn.commit()

    def close_sweep(self):
        conn = self.deps.conn
        now = self.clock.now()
        today = self.clock.today()
        rows = conn.execute(
            "SELECT id FROM event_log WHERE agent_status = 'pending_retry'"
            " AND trade_date <= ?", (today,)).fetchall()
        for (eid,) in rows:
            conn.execute("UPDATE event_log SET agent_status = 'gave_up' WHERE id = ?", (eid,))
            with conn:
                conn.execute(
                    "INSERT INTO agent_turn(ts, wake_kind, context_digest, rounds, decision,"
                    " why, fallback, market) VALUES (?,?,?,?,?,?,?,?)",
                    (now.isoformat(), "close", f"收盘清扫 事件={eid}", 0, "gave_up",
                     "收盘未接手", 0, "cn_main"))
        conn.commit()

    def _open_turn_done(self, today_iso):
        return self.deps.conn.execute(
            "SELECT 1 FROM agent_turn WHERE wake_kind = 'open' AND ts >= ?"
            " ORDER BY id DESC LIMIT 1", (today_iso,)).fetchone() is not None

    def _shadow_knocked(self, source: str) -> bool:
        """heartbeat 的 `source` 是否就是**同一个到期时刻**（进程内直接比水位）。"""
        due_at = source.split("due_at=", 1)[-1].strip()
        return due_at == self._shadow_beat_at

    def _shadow_turn(self, wake_kind: str, source: str, *, market=None) -> int:
        """被拦下的非事件唤醒：只记日志，不进入 LLM/推送链路。

        `trigger_id` 只对应 event_log.id，非事件唤醒一律 NULL；来源（含 schedule.id）
        放 digest。幂等按 kind + source：heartbeat 的 source 含**该次到期时刻**，
        所以下一个真实心跳仍有自己的审计行，同一个到期时刻（含容器重启后重算出的
        同一跳）不会重复记账；open 的 source 含交易日。
        """
        conn = self.deps.conn
        digest = f"shadow {wake_kind} {source}"
        row = conn.execute(
            "SELECT id FROM agent_turn WHERE wake_kind = ? AND decision = 'shadow'"
            " AND context_digest = ? LIMIT 1", (wake_kind, digest)).fetchone()
        if row is not None:
            return row[0]
        with conn:
            cur = conn.execute(
                "INSERT INTO agent_turn(ts, wake_kind, trigger_id, context_digest, rounds,"
                " decision, why, fallback, market) VALUES (?,?,NULL,?,0,'shadow',?,0,?)",
                (self.clock.now().isoformat(), wake_kind, digest, SHADOW_WHY,
                 market or _default_market(conn, self.deps.cfg)))
        turn_id = cur.lastrowid
        if wake_kind == "heartbeat":
            self._shadow_beat_at = source.split("due_at=", 1)[-1].strip()
        return turn_id

    def _pending_retry(self):
        rows = self.deps.conn.execute(
            "SELECT id, ts, trade_date, market, kind, scope, target, payload, salience,"
            " escalated, turn_id, agent_status, escalated_at, dedupe_key FROM event_log"
            " WHERE trade_date = ? AND agent_status = 'pending_retry' ORDER BY id",
            (self.clock.today(),)).fetchall()
        from agent.attention import EventRow

        return [EventRow(id=r[0], ts=r[1], trade_date=r[2], market=r[3], kind=r[4],
                         scope=r[5], target=r[6], payload={}, salience=float(r[8]),
                         escalated=int(r[9] or 0), turn_id=r[10], agent_status=r[11],
                         escalated_at=r[12], dedupe_key=r[13] or "") for r in rows]

    def _close_done(self):
        row = self.deps.conn.execute(
            "SELECT 1 FROM job_state WHERE name = ?",
            (f"agent_close.{self.clock.today()}",)).fetchone()
        if row is None:
            self.deps.conn.execute(
                "INSERT OR REPLACE INTO job_state(name, value) VALUES (?, '1')",
                (f"agent_close.{self.clock.today()}",))
            self.deps.conn.commit()
            return False
        return True

    async def _once(self):
        now = self.clock.now()
        if self.clock.today() != self._last_day:
            self._last_day = self.clock.today()
            self.budget_exhausted = False
        if self.next_heartbeat_at is None:
            self.next_heartbeat_at = next_heartbeat(now)

        for group in self.attention.poll():
            events = sorted(group.events, key=lambda e: e.id)
            group.events = events
            if self.shadow_mode:
                # **与 alarm/open 同级的硬闸**：不能只靠 `poll()` 在 shadow 下返回
                # 空组来实现影子——那个内部实现一旦改动，或者协程被取消前状态已提交，
                # 这里就会真的 `await self._run(...)` 调模型花钱。
                self._shadow_turn(
                    "event", f"组={group.key} 事件={len(events)}",
                    market=getattr(group, "market", None))
                continue
            await self._run(self._trigger("event", group=group, events=events,
                                          rounds=EVENT_ROUNDS_BUDGET,
                                          deadline=EVENT_DEADLINE_S))

        if now >= self.next_heartbeat_at:
            due_at = self.next_heartbeat_at
            # 家务与「下一跳」与影子无关：不做的话影子期 escalated 永不回收、
            # 心跳停摆，两周观测拿到的就是一堆过期事件。
            self.heartbeat_housekeeping()
            if self.shadow_mode:
                # 影子期不排队 LLM 回合（pending_retry 的事件已被 poll() 拦在 shadow 行里）
                source = f"due_at={due_at.isoformat()}"
                if not self._shadow_knocked(source):
                    self._shadow_turn("heartbeat", source)
            else:
                retry = self._pending_retry()
                # 这些事件的第一次失败已经发生过（状态就是 pending_retry），
                # 本次接手再失败必须转 gave_up——见 journal.record 的注释
                for ev in retry:
                    ev.agent_status = "pending_retry"
                await self._run(self._trigger("heartbeat", events=retry,
                                              rounds=OTHER_ROUNDS_BUDGET,
                                              deadline=OTHER_DEADLINE_S))
            nxt = next_heartbeat(self.clock.now())
            if (self.shadow_mode and nxt.isoformat() != self._shadow_beat_at
                    and nxt <= self.clock.now()):
                # **不能排出一个已经到期的下一跳**：盘后（>15:05）next_heartbeat 给的是
                # 次日 09:15，而 `run()` 在 15:06 之后就不再调 `_once()`，那天的结束心跳
                # 永远不到期。影子期这里补一次收尾心跳（同样只记账、不跑模型）。
                self._shadow_turn("heartbeat", f"due_at={nxt.isoformat()}")
            self.next_heartbeat_at = nxt

        for alarm in due_alarms(self.deps.conn, now):
            # **先认领再跑**：不认领的话，60 秒的闹钟回合期间 2 秒后的下一轮会把同一个
            # 闹钟再取出一次，形成重复回合
            if not after_alarm(self.deps.conn, alarm["id"]):
                continue
            if self.shadow_mode:
                # 影子期也要**认领**（上面已认领）并补 fired_turn：否则这条闹钟每 2 秒
                # 到期一次、永远重排；fired_turn 指向 shadow 行，事后能对上号。
                turn_id = self._shadow_turn(
                    "alarm", f"alarm={alarm['id']} due_at={alarm.get('due_at')}",
                    market=alarm.get("market"))
                set_fired_turn(self.deps.conn, alarm["id"], turn_id)
                continue
            await self._run(self._trigger("alarm", rounds=OTHER_ROUNDS_BUDGET,
                                          deadline=OTHER_DEADLINE_S,
                                          alarm_id=alarm["id"],
                                          target=alarm.get("target"),
                                          note=alarm.get("note") or ""))

        if ((now.hour, now.minute) >= OPEN_TURN_AT
                and not self._open_turn_done(now.date().isoformat())):
            if self.shadow_mode:
                self._shadow_turn("open", f"trade_date={now.date().isoformat()}")
            else:
                await self._run(self._trigger("open", rounds=OTHER_ROUNDS_BUDGET,
                                              deadline=OTHER_DEADLINE_S))

    async def run(self):
        while True:
            now = self.clock.now()
            with closing(self.deps.conn_factory()) as conn_tmp:
                awake = should_wake(conn_tmp, now)
            if not awake:
                if now.hour >= 15 and not self._close_done():
                    self.close_sweep()
                time.sleep(SLEEP_OUT_OF_SESSION_S)
                continue
            try:
                await self._once()
            except Exception as exc:                # noqa: BLE001 —— 主循环不能死
                if getattr(self.deps, "alert", None) is not None:
                    self.deps.alert("agent_main_crash", f"agent 主循环异常：{exc}", "P1")
            await asyncio.sleep(LOOP_EVERY_S)


def main(conn_factory=None, clock=None, every_s: int = 30) -> None:
    """agent 进程入口（批2 替换空壳）。"""
    import os as _os

    _setup_logging()

    from core.db import make_conn_factory as _mcf
    from core.config import load_config as _lc
    from llm.budget import Budget as _B
    from llm.client import LlmClient as _LC
    from llm.tools import ToolRegistry as _TR
    from notify.dispatcher import Dispatcher
    from notify.ntfy import NtfyClient

    conn_factory = conn_factory or _mcf()
    clock = clock or SystemClock()
    conn = conn_factory()
    cfg = _lc()

    from mcp.westock import WestockMcp

    westock = WestockMcp()                       # env 缺失时降级，工具照常返回 mcp_unavailable
    ntfy = NtfyClient(cfg.notify.ntfy_url, cfg.notify.topic, _os.environ["NTFY_TOKEN"])

    from agent import memory
    from agent.push import AgentPusher

    pusher = AgentPusher(conn, ntfy, cfg, clock)
    deps = Deps(conn=conn, cfg=cfg, clock=clock, llm=_LC(cfg),
                budget=_B(conn, "agent", cfg.llm.budget.agent),
                tools=_TR(conn, cfg, clock, mcp=westock),
                render=lambda t: render_trigger(t, conn, clock, cfg),
                pusher=pusher, memory=memory, westock=westock)
    deps.conn_factory = conn_factory
    deps.dispatcher = Dispatcher(conn, ntfy, cfg, clock)
    deps.alert = deps.dispatcher.alert

    boss = AgentMain(deps, clock)
    # **心跳必须有**：compose 跑的是 `python -m agent.main`。没有心跳线程，
    # watchdog 会在 120 秒后判 agent_down；而且首页「最后活动」与预算抑制都读它。
    Heartbeat(conn_factory, service=heartbeat_service(), every_s=every_s, clock=clock,
              detail_provider=lambda: {"budget_exhausted": boss.budget_exhausted}).start()
    # **`run()` 是协程**：不 `asyncio.run` 的话这里只是建了个协程对象，
    # 进程立刻退出，容器反复重启，而所有单测都绿。
    asyncio.run(boss.run())


if __name__ == "__main__":
    main()
