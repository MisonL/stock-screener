"""agent 主进程：交易时段内的四类唤醒（计划 Task 11）。

**record 只由主循环调用**：`asyncio.wait_for` 取消后 `run_turn` 里走不到写日志，
超时回合会零日志、事件永远滞留 `escalated`。

四种唤醒必须都入队——事件 / 心跳 / 闹钟 / 开盘简报第二段。缺一种就永久静默
（尤其是 `open`：没有它，开盘简报永远只有第一段）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import closing
from datetime import datetime, timedelta

from agent.attention import AttentionLayer
from agent.heartbeat import Heartbeat
from agent.journal import record as journal_record
from agent.loop import (BUDGET_EXHAUSTED_WHY, BUDGET_TOO_LARGE_WHY, ROUNDS_EXHAUSTED_WHY,
                        Deps, Trigger, render_trigger, run_with_deadline)
from agent.schedule import (after_alarm, due_alarms, next_heartbeat, release_alarm, schedule_alarm,
                            set_fired_turn)
from core.clock import SH, SystemClock, TradingCalendar

SLEEP_OUT_OF_SESSION_S = 60
LOOP_EVERY_S = 2
# **回合预算：轮数是主界，deadline 是可选的业务上限（`0` = 不设）。**
#
# 默认不设业务上限是一次生产故障换来的：上游网关排队时段**每轮要 23–31 秒**，
# 任何固定的 deadline 迟早会被正常回合超过去，然后整天的回合全部报废
# （实盘：一天 21/21 个回合 timeout）。
#
# 但 `0` **不等于完全没有上限**：`agent/loop.py` 会给一个兜底的
# `SAFETY_DEADLINE_S`（10 分钟），只有"上游彻底不回包"才会撞上。
# 那个值刻意小于 watchdog 的判死阈值（15 分钟），让回合自己收尾记 `timeout`，
# 而不是被外部判定成"进程卡死"。
#
# 要收紧时长时把这里改成正数；一条铁律不变：**若设了单次调用上限，
# deadline 必须 ≥ 该上限 × 2**——一个回合至少要两次 LLM 调用
# （先决定查什么、拿到工具结果后再出决策）。
# 下面两组是**兜底默认值**（配置文件缺键时用）。真正生效的值从
# `config/agent.yaml` 的 `event` / `heartbeat_turn` 读（见 `_turn_budget`）——
# 否则"改成可配置参数"就是空话：改 YAML 不会改变运行时行为。
EVENT_ROUNDS_BUDGET, EVENT_DEADLINE_S = 3, 0
OTHER_ROUNDS_BUDGET, OTHER_DEADLINE_S = 6, 0
ESCALATED_RECYCLE_MIN = 30
CONFIRM_EXPIRE_H = 24
#: 作废扫描的最小间隔（秒）。每轮 `_once()`（2 秒）都扫一次没必要，
#: 但也不能像原来那样"一天只扫一次"——那样同日积压扫不到。
ALARM_SWEEP_EVERY_S = 60
#: 连续多少个回合因额度失败，才认定"今天额度真的用完了"并 latch。
#:
#: 为什么需要这个：`budget_shortfall()` 把"这次请求太大"和"今天没了"分开后，
#: 额度**接近**用完的那段尾巴（实测剩 9056 / 5.5M 而典型请求要 21888）
#: 会让每个心跳/事件回合各写一行 `rounds=0, latency_ms=0, decision=error`。
#: 每次 5 分钟一行不算事故，但它是**纯粹的噪声**，而且正好是这个项目最怕的
#: 那种记录形状（11-19 事故就是这种行）。
#:
#: 为什么不直接把 `MIN_TURN_EST_IN` 调大：那会把 Codex 实测的场景
#: （剩 41366、大请求 41367、但心跳只需 8913 完全跑得动）又锁成"今天没了"。
#: 判定必须看**连续性**，不能看单次估算——单次估算永远可能乐观或保守。
#:
#: 连续 3 次都因额度失败 = 这段尾巴里所有回合都跑不动了，可以停止尝试；
#: 中途只要有一次成功（哪怕是最小的心跳），计数清零，不 latch。
BUDGET_FAIL_STREAK_TO_LATCH = 3
#: 单轮 `_once()` 最多跑几个闹钟回合。宕机后积压十几条同时到期时，
#: 一口气串行跑完会连着烧掉上百轮 LLM；每轮限 2 条，积压照样被消化。
ALARM_MAX_PER_ONCE = 2
OPEN_TURN_AT = (9, 26)
#: 开盘回合每天最多试几次（防"日志写不出来 → 每 2 秒重跑一次"）
OPEN_TURN_MAX_ATTEMPTS = 3
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


def _turn_budget(cfg, section: str, rounds_default: int, deadline_default: float
                 ) -> tuple[int, float]:
    """从 `config/agent.yaml` 读一组回合预算：`(rounds, deadline_s)`。

    **必须真读配置**：这两段以前是死配置（`core/config.py` 只做校验），
    写死在 `agent/main.py` 的常量里——运维改 YAML 完全不会有任何效果，
    而故障排查时第一反应恰恰是去查 YAML。

    缺键/非法值一律回落到兜底默认，不让一个笔误把 agent 挡在启动之外。
    """
    try:
        block = getattr(getattr(cfg, "agent", None), section)
    except (AttributeError, TypeError):
        return rounds_default, deadline_default
    rounds = getattr(block, "rounds", None)
    deadline = getattr(block, "deadline_s", None)
    try:
        rounds = int(rounds) if rounds is not None else rounds_default
    except (TypeError, ValueError):
        rounds = rounds_default
    try:
        deadline = float(deadline) if deadline is not None else deadline_default
    except (TypeError, ValueError):
        deadline = deadline_default
    # 轮数必须为正：0 轮等于"永不开口"，让它回落到默认
    if rounds <= 0:
        rounds = rounds_default
    # deadline < 0 无意义（0 合法 = 不设业务上限），负数当笔误
    if deadline < 0:
        deadline = deadline_default
    return rounds, deadline


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
        # 回合预算从配置读一次（运行中改 YAML 不生效，与 shadow_mode 同一口径）
        self.event_rounds, self.event_deadline = _turn_budget(
            deps.cfg, "event", EVENT_ROUNDS_BUDGET, EVENT_DEADLINE_S)
        self.other_rounds, self.other_deadline = _turn_budget(
            deps.cfg, "heartbeat_turn", OTHER_ROUNDS_BUDGET, OTHER_DEADLINE_S)
        self.next_heartbeat_at = None
        self.budget_exhausted = False
        # 今日额度是否用完（跨日复位；与"记账写库坏了"是两回事，见 `_once()`）
        self._quota_exhausted = False
        # 连续因额度失败的回合数（见 BUDGET_FAIL_STREAK_TO_LATCH）
        self._budget_fail_streak = 0
        self._last_day = self.clock.today()
        # 上一次扫过期闹钟的时刻（见 `_once()` 里的闹钟循环）
        self._alarm_swept_at = None
        # 今天试过几次开盘回合（见 `_once()` 尾部的次数上限）
        self._open_attempts = 0

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
                release_alarm(self.deps.conn, trigger.alarm_id, now=self.clock.now())
            raise
        turn_id = journal_record(self.deps.conn, trigger, result, self.clock)
        if trigger.alarm_id is not None:
            set_fired_turn(self.deps.conn, trigger.alarm_id, turn_id)
            # **回合失败要把闹钟退回去**：`after_alarm` 在跑之前就认领成 `fired`，
            # 而 LLM 挂掉/预算耗尽/超时都会让这一轮白跑。不放回 `pending` 的话，
            # 模型说的"我 30 分钟后回来看"就静默作废（清理任务只处理 3 天前的
            # pending，不会补跑）。超时不算失败——它已经花掉预算跑过了。
            # **预算耗尽时不要退回**：当天不可能恢复了，退回只会变成
            # 「每 2 秒重跑一次失败回合」的死循环（生产实测一天 17885 行空转）。
            # 直接转 cancelled，把这次回访算作放弃，而不是无限重试。
            # **`timeout` 也要退回**（v0.2.90 修的 L2）。原来只有 `error`/`invalid`
            # 会退回，超时回合把闹钟永久留在 `fired` —— 模型说的"我 30 分钟后
            # 回来看"静默作废，与最初那个"失败即永久丢失"是同一个毛病
            # （`release_alarm` 的 docstring 里本来就写了"LLM 挂掉/预算耗尽/超时"，
            # 实现只覆盖了前两种）。现在它有 60 秒退避与 3 次上限，退回不会成环。
            # **"轮数用尽"不退回**（v0.2.93 修的 M-c）。它和"回合超时"是两种
            # 完全不同的失败：超时是外部慢，重试有意义；轮数用尽是**模型自己
            # 在 6 轮内没有收敛**，隔 60 秒再跑一遍必然同样用尽——评审实测
            # 3 次重试 ≈ 18 轮 LLM 调用全烧在同一条没有收敛的闹钟上，
            # 最后一次转 cancelled 且**无任何通知**，用户以为"30 分钟后回来看"
            # 实际静默丢了。
            # 直接 cancelled，把这次回访明确记为放弃。
            rounds_exhausted = getattr(result, "why", "") == ROUNDS_EXHAUSTED_WHY
            if rounds_exhausted:
                self._cancel_alarm(trigger.alarm_id, "轮数用尽（模型未收敛）")
                # **必须让用户知道这次回访没发生**（v0.2.96，评审建议）。
                # 闹钟是用户看得见的承诺（「我 30 分钟后回来看」），
                # `agent_turn` 里的 `decision=timeout, why=轮数用尽` 只有运维在看。
                # 不推的话，用户以为 30 分钟后会有结论，实际静默丢了——
                # 和「静默 pending 再烧 18 轮」是同一个毛病，只是这次省了钱。
                # 每条闹钟只推一次（取消是单向的，不会重复），走低优先级便签。
                self._notify_alarm_give_up(trigger)
            elif getattr(result, "why", "") == BUDGET_TOO_LARGE_WHY:
                # **"本次请求超出剩余额度"不能退回重试**（v0.2.97，生产实测回归）。
                #
                # v0.2.96 为了修「差 1 个 token 就锁死整天」，把它和"额度真用完"
                # 分开了——但退回重试是错的处理：额度**只会更少**，同一条闹钟
                # 隔 60 秒再跑必然还是太大。
                #
                # 实测代价（11-20 13:20–13:40，额度剩 9056 / 5.5M）：35 条闹钟
                # 各退回 3 次，写了 **105 行 `rounds=0, latency_ms=0, decision=error`**
                # 的空转记录——正是 11-19 事故的记录形状，只是有 3 次上限兜着，
                # 不会无限转。但 10 分钟 105 行照样把日志冲垮、把 churn 快线顶到 114。
                #
                # 正确处理：**这条闹钟今天跑不动了，直接取消并记原因**。
                # 不 latch 整天（心跳/事件可能更小、还跑得动），也不重试。
                self._cancel_alarm(trigger.alarm_id, "额度不足（请求超出剩余额度）")
            elif result.decision.kind in ("error", "invalid", "timeout"):
                exhausted = getattr(result, "why", "") == BUDGET_EXHAUSTED_WHY
                if exhausted:
                    self._cancel_alarm(trigger.alarm_id, "预算耗尽")
                else:
                    release_alarm(self.deps.conn, trigger.alarm_id, now=self.clock.now())
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
        why = getattr(result, "why", None)
        if why == BUDGET_EXHAUSTED_WHY:
            # 连"最小请求"都放不下 —— 真的没了，立即 latch
            self._quota_exhausted = True
            self.budget_exhausted = True
            self._budget_fail_streak = BUDGET_FAIL_STREAK_TO_LATCH
        elif why == BUDGET_TOO_LARGE_WHY:
            # "这次太大"——可能是额度尾巴，也可能只是这个回合上下文大。
            # **看连续性，不看单次**：连续 N 次都失败才认为今天跑不动了。
            self._budget_fail_streak += 1
            if self._budget_fail_streak >= BUDGET_FAIL_STREAK_TO_LATCH:
                self._quota_exhausted = True
                self.budget_exhausted = True
        else:
            # 任何一次成功（哪怕最小的心跳）都证明额度还在
            self._budget_fail_streak = 0
        # **这里刻意不再镜像 `budget_exhausted`**（第四轮评审发现第 6 次假绿的
        # 副产品）：`_once()` 在**闹钟与开盘回合之前**无条件重算一次
        # `budget_exhausted = _quota_exhausted or _budget_unavailable()`，
        # 所以这里再写一遍是**冗余的、且测不到的**——评审实测：把这段删掉，
        # 全部测试照样绿，因为它永远被 `_once()` 那一行覆盖。
        #
        # 与其留一段无覆盖的代码冒充"修复"，不如删掉，让 `_once()` 那行成为
        # **唯一**的判定点。上面 `_quota_exhausted = True` 的 latch 仍然必要：
        # 它跨回合保存"今天额度用完了"这个不可自愈的事实。
        return turn_id

    def _cancel_alarm(self, alarm_id: int, reason: str) -> None:
        """把已认领的闹钟转 `cancelled`，并记下原因（见 008 迁移）。"""
        self.deps.conn.execute(
            "UPDATE agent_schedule SET status = 'cancelled', fired_turn = NULL,"
            " cancelled_reason = ? WHERE id = ? AND status = 'fired'",
            (reason, alarm_id))
        self.deps.conn.commit()

    def _notify_alarm_give_up(self, trigger) -> None:
        """告诉用户"这次回访放弃了"（轮数用尽）。

        **只在真有推送通道时做**，且任何异常都不能影响调度——
        一条通知失败不值得让整个回合链路崩掉。
        """
        pusher = getattr(self.deps, "pusher", None)
        if pusher is None:
            return
        try:
            target = getattr(trigger, "target", None) or ""
            note = getattr(trigger, "note", "") or ""
            body = (f"放弃了 {target or '这次'} 的回访：模型 6 轮内没有得出结论。"
                    f"原定关注：{note or '（无备注）'}")
            from agent.decision import TurnDecision
            decision = TurnDecision(kind="speak", speak=True, body=body, level="P2")
            pusher.agent_note(decision, None, None, target)
        except Exception:                                  # noqa: BLE001
            logging.getLogger("agent").warning(
                "回访放弃通知发送失败", exc_info=True)

    def _budget_unavailable(self) -> bool:
        """预算是否不可用：**只看数据库标志**，每轮重读。

        `llm/budget.py::_try_deduct` 成功扣减时会删掉这个键，于是这里下一轮
        就自动复位——不需要谁记得去清 `self.budget_exhausted`。
        读不到时 fail-closed 返回 True（宁可少跑一轮回访，不要反复撞锁）。
        """
        try:
            row = self.deps.conn.execute(
                "SELECT value FROM job_state WHERE name = 'budget.write_failed:agent'"
            ).fetchone()
            return bool(row and row[0])
        except Exception:                                  # noqa: BLE001
            return True

    def heartbeat_housekeeping(self):
        conn = self.deps.conn
        now = self.clock.now()
        deadline = (now - timedelta(minutes=ESCALATED_RECYCLE_MIN)).isoformat()
        conn.execute("UPDATE event_log SET agent_status = 'pending_retry'"
                     " WHERE agent_status = 'escalated' AND escalated_at < ?", (deadline,))
        # **跨日沉底事件必须显式收尾**（v0.2.94）。
        # `poll()` 只查 `trade_date = 今天`，所以前一天剩下的 `new` 事件
        # **永远不会再被取出**——它们既不 `done` 也不 `suppressed`，
        # 就这样停在"待处理"上。`jobs/cleanup.py` 要 30 天后才删，
        # 期间任何按 `agent_status` 做的统计（比如"今天有多少事件没处理"）
        # 都会读到一批永远不会动的僵尸行。
        #
        # 生产实测：shadow 观测期（11-10~11-17）每天都能积下 1~63 条，
        # 因为 `poll()` 的 shadow 分支**只写 shadow 回合、不改事件状态**。
        # 11-18 关掉 shadow 之后残留归零，但先前那 142 条已经沉底。
        #
        # 标成 `expired` 而不是 `done`：它们**确实没被处理**，
        # 记成 done 会让"处理率"这类口径说谎。
        conn.execute("UPDATE event_log SET agent_status = 'expired'"
                     " WHERE agent_status = 'new' AND trade_date < ?",
                     (self.clock.today(),))
        cutoff = (now - timedelta(hours=CONFIRM_EXPIRE_H)).isoformat()
        conn.execute("UPDATE agent_turn SET pending_status = 'expired'"
                     " WHERE pending_status = 'pending' AND ts < ?", (cutoff,))
        # **这个 commit 是必须的**：抽 `sweep_stale_alarms()` 时顺手把它一起搬走了，
        # 于是上面两条 UPDATE 全部失效——escalated 事件永不回收、确认卡永不过期，
        # 而且没有任何报错（事务在连接关闭时被隐式回滚）。
        # 抽方法时"把 commit 跟着语句一起搬"是很容易踩的坑，加测试钉住。
        conn.commit()

    def sweep_stale_alarms(self, now: datetime) -> int:
        """作废**跨交易日**的 pending 闹钟，返回作废条数。

        闹钟的语义是"过 N 分钟回来看看这个价位"，价位语境当天有效。隔天再跑一遍
        昨天没跑成的回访，看到的是新一天的行情、回答的是昨天的问题——而且会
        **一次性全部到期**：agent 停摆半天后积累的十几条会在同一秒被
        `due_alarms()` 全部取出，连跑十几个回合烧光预算，再推一串过时判断给用户。
        清理任务（`jobs/cleanup.py`）只处理 3 天前的，救不了"昨天积压、今天开盘"
        这个最常见的情形。

        **必须独立于心跳**：放进 `heartbeat_housekeeping()` 里试过，但那个方法只在
        心跳到期时跑——进程在 09:15 启动时 `next_heartbeat` 给的下一跳是 09:17，
        于是最初 2 分钟里作废没发生、闹钟循环却每 2 秒都在跑，昨天的闹钟照样开跑。
        （实测复现：`next_heartbeat_at` 初始为 None → 设成 09:17 → 09:15 这一轮
        `now < next_heartbeat_at`，housekeeping 不执行，而 alarm 循环执行。）
        """
        # 判据是**日期**，不是"过期多久"：闹钟的语境是"哪一天的行情"。
        #   · `due_at` 属于更早的交易日 → 作废（隔天回答的是昨天的问题）
        #   · `due_at` 是今天（哪怕已经迟了一两个小时） → 保留
        #
        # **"迟太久就作废"的写法是错的**（v0.2.90 修）：它会把
        # 「昨天排的、今天 09:20 的回访」在进程 10:30 才拉起时一起杀掉——
        # 那条闹钟的日期是今天、语境完全有效，只是进程晚启动了 70 分钟。
        # 同样的误杀也发生在盘中短暂重启后：09:30 的正常回访在 10:40 恢复时被作废。
        # 那两类恰恰是**最该跑**的，不能和"昨天的陈货"一视同仁。
        #
        # 顺带说明：原来用 `max(当天零点, now - 1小时)` 做阈值时，盘中 `now - 1h`
        # 恒晚于零点，零点那个分支是**死代码**——写着两条判据，实际只有一条在跑。
        today_iso = now.date().isoformat()
        cur = self.deps.conn.execute(
            "UPDATE agent_schedule SET status = 'cancelled'"
            " WHERE status = 'pending' AND substr(due_at, 1, 10) < ?", (today_iso,))
        self.deps.conn.commit()
        return cur.rowcount

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
            self._quota_exhausted = False
            self._budget_fail_streak = 0
            self._open_attempts = 0
        if self.next_heartbeat_at is None:
            self.next_heartbeat_at = next_heartbeat(now)

        # **`poll()` 必须包住**（v0.2.95 修的 M2）：它排在心跳推进与闹钟循环**之前**，
        # 而且内部 `if not events: return []` 会提前返回。它一旦抛异常
        # （写锁超时/磁盘满，正是它要写 `suppressed`/`escalated` 的时候），
        # `_once()` 整体抛出、被 `run()` 吞掉 → 心跳、闹钟、开盘回合**全部停摆**。
        # 这与 v0.2.93 修的 housekeeping 是同一类错误，只是位置更早、后果更重。
        # 事件处理失败只该影响事件，不该影响调度。
        # **预算耗尽时连 poll 都不做**（v0.2.99，生产实测）。
        #
        # `poll()` 会把事件从 `new` 标成 `escalated`，然后下面每组跑一个回合。
        # 预算耗尽时那些回合必然在预检里返回 `rounds=0, latency_ms=0` 的 error——
        # 白写一行。实测 11-20 14:20:42 一轮 poll 返回 8 组，**同一秒写了 8 行**
        # 空转；这些事件又从 `escalated` 被回收到 `pending_retry`、再失败转
        # `gave_up`，一条事件最多折腾两轮。
        #
        # 更关键的是**状态污染**：跳过 `_run` 但已经标了 `escalated`，
        # 事件就卡在一个"已升级但没人跑"的中间态。不如不 poll——
        # 事件保持 `new`，跨日由 `heartbeat_housekeeping()` 标 `expired`，
        # 语义干净，也不会留下半截状态。
        if self.budget_exhausted:
            polled = []
        else:
            try:
                polled = self.attention.poll()
            except Exception:                              # noqa: BLE001
                logging.getLogger("agent").warning(
                    "attention.poll 失败（本轮跳过事件）", exc_info=True)
                polled = []
        for group in polled:
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
                                          rounds=self.event_rounds,
                                          deadline=self.event_deadline))

        if now >= self.next_heartbeat_at:
            due_at = self.next_heartbeat_at
            # **下一跳最先推进，必须在任何可能抛异常的事之前**（v0.2.90/0.2.93）。
            #
            # `_run` 的顺序是「跑回合 → 记日志 → 推送/确认卡/排闹钟」，任何一步
            # 抛异常都会被 `run()` 吞掉（只发个 alert 继续循环）。原来
            # `self.next_heartbeat_at = nxt` 写在 `await self._run(...)` **之后**，
            # 于是只要日志写成功之后炸一次，下一跳就永远不推进——
            # `now >= next_heartbeat_at` 恒成立，**每 2 秒跑一次心跳回合**。
            # 这与 11-19 那 17998 行是完全相同的形状，只是触发器不同。
            #
            # v0.2.93 又把 `heartbeat_housekeeping()` 挪到它后面：那个方法要写两张表
            # 并 commit，正是"磁盘满/写锁超时"会炸的地方；它一炸，`_once()` 整体抛出，
            # 心跳/闹钟/开盘回合**全部停摆**（虽然不写垃圾行，但比写了更糟）。
            # 家务是"尽力而为"的维护，不该有能力让调度停摆。
            nxt = next_heartbeat(now)
            self.next_heartbeat_at = nxt
            # 家务与「下一跳」与影子无关：不做的话影子期 escalated 永不回收、
            # 心跳停摆，两周观测拿到的就是一堆过期事件。
            # **它自己炸了也不能影响调度**：只记日志。
            try:
                self.heartbeat_housekeeping()
            except Exception:                              # noqa: BLE001
                logging.getLogger("agent").warning(
                    "heartbeat_housekeeping 失败（已忽略）", exc_info=True)
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
                                              rounds=self.other_rounds,
                                              deadline=self.other_deadline))
            # **回合跑完再按墙钟补正一次**（v0.2.93 修的 M-b）。
            # 上面那个 `nxt` 是用 `_once()` 开头的 `now` 算的，而回合可能跑几分钟
            # （上游慢时单轮 23–31 秒、6 轮回合 3 分钟是常态）。回合结束时 `nxt`
            # 往往已经过期，于是下一轮 `_once()` 立刻又是一次心跳——同一段墙钟里
            # 心跳回合数比原来多 37%~62%，全是白花的 token，还吃掉
            # `AGENT_TURN_CHURN_PER_HOUR` 的余量。
            # `next_heartbeat` 是纯函数、不抛异常，补这一次不会重新引入 S2。
            self.next_heartbeat_at = max(self.next_heartbeat_at,
                                         next_heartbeat(self.clock.now()))
            nxt = self.next_heartbeat_at
            if (self.shadow_mode and nxt.isoformat() != self._shadow_beat_at
                    and nxt <= self.clock.now()):
                # **不能排出一个已经到期的下一跳**：盘后（>15:05）next_heartbeat 给的是
                # 次日 09:15，而 `run()` 在 15:06 之后就不再调 `_once()`，那天的结束心跳
                # 永远不到期。影子期这里补一次收尾心跳（同样只记账、不跑模型）。
                self._shadow_turn("heartbeat", f"due_at={nxt.isoformat()}")

        # **预算耗尽就跳过闹钟**：闹钟回合必然在 `run_with_deadline` 的预检里
        # 立刻返回 error（rounds=0、0 毫秒），既不调模型也不产生任何价值，
        # 只会在 `agent_turn` 里留下成千上万行空转记录。
        # 跳过而不是 cancel：闹钟本身是有效的，只是今天没法跑。
        #
        # **注意这里不能 `return`**：下面还有开盘回合的处理。用 early return
        # 会顺手把无关逻辑一起跳过——以后往函数尾部加东西就会踩到。
        # **扫一遍过期闹钟**（最多每 `ALARM_SWEEP_EVERY_S` 一次）。
        # 放在这里而不是 `heartbeat_housekeeping()`：后者只在心跳到期时跑，
        # 进程刚启动的头一两分钟不会执行，而闹钟循环每 2 秒都在跑——
        # 于是昨天的闹钟会先开跑（真实路径复现过）。
        if (self._alarm_swept_at is None
                or (now - self._alarm_swept_at).total_seconds() >= ALARM_SWEEP_EVERY_S):
            self.sweep_stale_alarms(now)
            self._alarm_swept_at = now
        # **每轮都重读一次预算标志**（v0.2.95 修的 S1 后半）。
        # 只在 `_run()` 里做镜像是**死锁**：预算不可用时闹钟循环被跳过、
        # 开盘回合当天已经跑过、心跳还没到期 —— 于是这一轮根本不会 `_run`，
        # 标志永远没机会复位。评审实测就是这个形状。
        # 放在这里（闹钟循环之前）才保证"每轮必然执行一次"。
        # **两个来源必须分开记，不能合成一个变量**（v0.2.95）。
        #  · `_quota_exhausted`：今天的**额度**用完了 —— 当天不可能恢复，
        #    只能跨日复位（`_once()` 开头那处）。
        #  · `_budget_unavailable()`：**记账写库坏了** —— 依赖 30 秒探测自愈，
        #    所以每轮都要重读，不能缓存。
        # 混成一个变量会二选一地出错：缓存它，"记账坏了"就永不复位（S1 的 latch）；
        # 每轮重读它，"额度用完"就会被清掉 —— **而额度用完正是 11-19 事故的成因**，
        # 清掉之后闹钟恢复空转，一天又是上万行。
        self.budget_exhausted = self._quota_exhausted or self._budget_unavailable()
        # **单轮最多跑 `ALARM_MAX_PER_ONCE` 个闹钟回合**（v0.2.93 修的 M-d）。
        # 按日期作废把"同日迟到"的闹钟都保住了（对：语境有效），但也失去了原来
        # "同日过期 1 小时"顺手提供的泄压：宕机到 13:00 恢复时，上午积压的
        # 十几条会**同一轮全部到期**，`due_alarms()` 的 LIMIT 10 会一口取出 10 条，
        # 单条闹钟回合上限 6 轮、墙钟上限 600 秒——串行 10 条最坏 100 分钟连续回合。
        # 这正是 `sweep_stale_alarms` docstring 里列为"要防的事故"那种形状。
        # 每轮只跑 2 条：积压仍会被消化（每 2 秒一轮），但不会一口气烧光预算。
        for alarm in ([] if self.budget_exhausted
                      else due_alarms(self.deps.conn, now)[:ALARM_MAX_PER_ONCE]):
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
            await self._run(self._trigger("alarm", rounds=self.other_rounds,
                                          deadline=self.other_deadline,
                                          alarm_id=alarm["id"],
                                          target=alarm.get("target"),
                                          note=alarm.get("note") or ""))

        # **开盘回合也要有次数上限**（v0.2.90 的 S2 同源问题）：
        # `_open_turn_done()` 判断的是"当天有没有 open 行"，而回合若在
        # `journal_record` **之前**就抛异常，那一行永远写不出来 ——
        # `now >= OPEN_TURN_AT` 恒成立，于是每 2 秒重跑一次开盘回合。
        # 与心跳不同，这里没有"下一跳"可以推进，只能用当日尝试次数兜住。
        if ((now.hour, now.minute) >= OPEN_TURN_AT
                and not self._open_turn_done(now.date().isoformat())
                and self._open_attempts < OPEN_TURN_MAX_ATTEMPTS):
            self._open_attempts += 1
            if self.shadow_mode:
                self._shadow_turn("open", f"trade_date={now.date().isoformat()}")
            else:
                await self._run(self._trigger("open", rounds=self.other_rounds,
                                              deadline=self.other_deadline))

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
              detail_provider=lambda: {
                  # **只报"额度用完"，不报"记账写库坏了"**（第四轮评审的轻微项）。
                  # watchdog 用 `budget_exhausted` 去**静音** `agent_stalled` 与
                  # `llm_down`——因为额度用完是"预期停止"。但记账读不出来时
                  # 同样会 fail-closed 置 True，那会被误当成"额度用完"，
                  # 把「卡死」的告警一起静音。分开报：额度用完才静音，
                  # 记账坏了单独可观测（运维能查，但不影响告警）。
                  "budget_exhausted": boss._quota_exhausted,
                  "budget_write_failed": boss._budget_unavailable()
                  if boss._quota_exhausted is False else False,
              }).start()
    # **`run()` 是协程**：不 `asyncio.run` 的话这里只是建了个协程对象，
    # 进程立刻退出，容器反复重启，而所有单测都绿。
    asyncio.run(boss.run())


if __name__ == "__main__":
    main()
