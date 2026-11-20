"""watchdog：api 每 30 秒检查 `jobs` / `agent` 的心跳（§7.6.4、D9）。

四条容易写错的边界，逐条钉死：

1. **维护模式早退是第一条**：回填 / 备份 / 演练期间不判，否则每次维护都发 P0。
2. **只在盘中窗口 + 交易日判**：只判时刻不判交易日，周末与长假每天两次 P0
   「盘中监控已停止」，手机被自己刷爆。
3. **周期 30 秒**（不是 60）：验收承诺「stop 后 3 分钟内收到 P0」，60 秒周期 + 180 秒阈值
   的最坏时延是 239 秒，卡在临界点上。
4. **同类告警 30 分钟内只发一次，且只认 `accepted`**：把 failed 也算作「已告警」，
   ntfy 故障期间后续重试会全部跳过，组合故障下手机一条都收不到。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Literal

from core.clock import SH, TradingCalendar

AlertKind = Literal["agent_down", "jobs_down", "agent_stalled", "llm_down",
                    "agent_churn"]

STALE_S = 120                  # 心跳超过这么久没更新 → 判死
CHECK_EVERY_S = 30             # 与 api/main.py 的调度周期一致
REPEAT_S = 1800                # 同类告警 30 分钟内不重复
AGENT_TURN_STALE_S = 15 * 60   # agent_turn 超过 15 分钟没有新行 → 调度卡死（规则 3）
#: 回合数暴增的判据：滚动一小时内超过这么多个回合就报（规则 5）。
#:
#: 这个值改过两次，两次都是**样本选错了**，记下来免得再犯：
#:
#: 第一版写 50。依据是 11-10~11-18 六个正常交易日（逐小时峰值 27~48、
#: 滚动一小时最大 50）。**50 就是正常日最忙那一小时本身**，等于拿踩线当阈值。
#:
#: 第二版写 200。看着有 4 倍余量——但只量了正常日。11-20 是暴跌日，
#: 下跌家数 5400+，模型每 2~6 分钟就排一条回访闹钟，45 分钟建了 96 条，
#: 实测滚动一小时 **169 个回合**，离 200 只剩 16% 余量，再忙一点就误报。
#: **暴跌日恰恰是模型最活跃、最该被监控的一天，不能在那天误报。**
#:
#: 现在取 1000：
#:   · 正常日峰值 50     → 20 倍余量
#:   · 暴跌日峰值 169    → 5.9 倍余量
#:   · 事故日 17998      → 1/18
#: 真实故障与合法忙碌之间隔着两个数量级，1000 落在中间，怎么抖都掉不到一起。
AGENT_TURN_CHURN_PER_HOUR = 1000

#: 短窗快线：最近 5 分钟超过这么多个回合就报（与上面的滚动一小时**并联**）。
#:
#: 为什么需要第二条判据：滚动一小时是**累计量**，低速 runaway 发现太晚。
#: 比如每分钟 20 个（≈4800/日，已是正常日 131 行的 **36 倍**），
#: 要跑满 **48 分钟**才越过 1000 线；而 `_alert` 还有 `REPEAT_S=1800` 去重，
#: 真正推出去可能已经一小时后。
#:
#: 实测基线（生产 11-20 暴跌日，每 5 分钟窗口）：**合法**峰值约 20 个回合
#: （正常日同一窗口 5~8 个）。取 120 = 合法峰值的 6 倍。
#:
#: **注意这个值确实被顶到过 114**——但那 114 里有 **105 个是 v0.2.96 的回归**：
#: 额度尾巴上"请求超出剩余额度"被当成可重试错误，35 条闹钟各退回 3 次。
#: v0.2.97 改成直接取消之后，这段噪声消失。也就是说 114 不是"合法忙碌逼近阈值"，
#: 而是**快线差点抓住一个真 bug**——留在这里作为阈值不该再调低的证据。
AGENT_TURN_CHURN_PER_5MIN = 120


def in_watch_window(now: datetime) -> bool:
    """盘中窗口：09:15–11:30 与 13:00–15:05（午休不算）。"""
    hm = now.astimezone(SH).strftime("%H:%M")
    return "09:15" <= hm <= "11:30" or "13:00" <= hm <= "15:05"


def check(conn, clock, dispatcher, *, cfg=None, agent_turn_stale_min: int | None = None) -> list[str]:
    """返回本次判定出的告警种类（已发出）。顺序恒为 `agent_down` → `jobs_down`。"""
    # ① 维护模式：回填/备份/演练期间一律不判
    flag = conn.execute("SELECT value FROM job_state WHERE name = 'maintenance.flag'").fetchone()
    if flag is not None and str(flag[0]) == "1":
        return []

    now = clock.now()
    if not in_watch_window(now):
        return []
    try:
        if not TradingCalendar(conn).is_trading_day(clock.today()):
            return []
    except (LookupError, ValueError):
        return []

    fired: list[str] = []

    # **缺失心跳也算死**：原来 `age is None`（从没写过心跳）直接跳过——
    # 新库/首次部署时 agent 与 jobs 根本没有心跳行，watchdog 永远静默。
    # 但要给启动留宽限：首次检查只记时间戳，超过 STALE_S 仍无心跳才报，
    # 否则每次重启后的头两分钟都会误报。
    missing_ok = _grace_elapsed(conn, now)
    agent = _beat(conn, "agent")
    agent_age = _age_s(agent, now)
    if (agent_age is None and missing_ok) or (agent_age is not None and agent_age > STALE_S):
        if _alert(conn, clock, dispatcher, "agent_down", "agent 进程无响应", now):
            fired.append("agent_down")

    jobs = _beat(conn, "jobs")
    jobs_age = _age_s(jobs, now)
    # 规则 3（§7.6.4）：**agent 心跳正常但 `agent_turn` 15 分钟无新行** → 调度卡死。
    # 心跳还在、回合不出 = 最隐蔽的故障：进程活着，但从没说也没记。
    # **用 agent_age 判健康，不能用「本次是否发了告警」**：agent_down 可能因为
    # 30 分钟去重而没发出来，那不代表进程是活的——否则会对着一个死进程喊「心跳正常」。
    agent_healthy = agent_age is not None and agent_age <= STALE_S
    if agent_healthy and not _budget_exhausted(conn):
        turn_age = _last_turn_age(conn, now, first_check=_first_check(conn))
        if turn_age is not None and turn_age > AGENT_TURN_STALE_S:
            if _alert(conn, clock, dispatcher, "agent_stalled",
                      f"agent 心跳正常但 {int(turn_age // 60)} 分钟没有回合", now,
                      level="P1"):
                fired.append("agent_stalled")

    # 决定 B3：**连续 N 个回合都 timeout/error** → LLM 不可用（P0）。
    # 只看「连续」：中途成功一次就清零。放在这里而不是 agent 进程内，是因为
    # agent 自己挂掉时也需要有人（api）替它说出来。
    if _llm_down(conn, cfg, now) and not _budget_exhausted(conn):
        if _alert(conn, clock, dispatcher, "llm_down", "LLM 连续不可用", now):
            fired.append("llm_down")

    # 规则 5：**回合数暴增 = 空转死循环**（v0.2.85）。
    #
    # 2026-11-19 实测：`release_alarm()` 把失败的闹钟退回 `pending`，而 `_once()`
    # 每 2 秒轮询一次、`due_alarms()` 返回所有 `due_at <= now` 的行——一条已过期的
    # 闹钟陷入「到期 → 认领 → 失败 → 退回 → 2 秒后又到期」的死循环，一直转到收盘。
    # 全天写了 **17998 行** `agent_turn`（正常日 78–131 行）。
    #
    # **为什么原有四条规则一条都没报**：`agent_down` 看心跳（进程活得好好的）、
    # `agent_stalled` 看"很久没有回合"（恰恰相反，回合多得离谱）、
    # `llm_down` 看连续失败（每个空转回合都是 `error`，确实"连续失败"——
    # 但它在 11:24 报过一次后进入 30 分钟去重，而空转是"看起来在干活"，
    # 从"连续失败"升级成"疯狂失败"没有任何人察觉）。
    #
    # 这类故障的特征是**量级异常**，所以判据也必须是量级。
    # **必须有上界 `ts <= now`**：没有它就不是"滚动一小时"，而是"这一小时之后的所有
    # 行"。实时运行时 `now` 就是当下，看不出区别；但只要 `now` 是注入的
    # （回放/干跑/测试用 `FixedClock`），统计量就会把"未来"算进来——实测干跑
    # 11-18 14:30 得到 18066，而正确的窗口值是 45。这种错在线上永远不触发，
    # 却会让所有离线排查得出相反结论。
    window_start = (now - timedelta(hours=1)).isoformat()
    recent = conn.execute(
        "SELECT count(*) FROM agent_turn WHERE ts > ? AND ts <= ?",
        (window_start, now.isoformat())).fetchone()[0]
    # **这里刻意不加 `_budget_exhausted(conn)` 的例外**——虽然另外两条规则都加了。
    # 理由反过来想就清楚：预算耗尽能解释「没有回合」「没有成功回合」，
    # 但它**解释不了「一小时内 18000 个回合」**。恰恰相反，这次事故的根因
    # 就是预算耗尽后闹钟无限重试：预检立刻返回 error → 回合记账 → 闹钟退回 →
    # 2 秒后再来。也就是说 `budget_exhausted` 是**这个故障的因**，不是免报的理由。
    # 加上这个 gate 就等于「最该报的那种空转恰好被永久静音」。
    if recent > AGENT_TURN_CHURN_PER_HOUR:
        if _alert(conn, clock, dispatcher, "agent_churn",
                  f"最近一小时内 {recent} 个回合（正常日约 50、暴跌日约 170，"
                  f"阈值 {AGENT_TURN_CHURN_PER_HOUR}），疑似空转死循环", now):
            fired.append("agent_churn")

    # **短窗快线**：累计量对低速 runaway 太迟钝，5 分钟窗口先报。
    short_start = (now - timedelta(minutes=5)).isoformat()
    burst = conn.execute(
        "SELECT count(*) FROM agent_turn WHERE ts > ? AND ts <= ?",
        (short_start, now.isoformat())).fetchone()[0]
    if burst > AGENT_TURN_CHURN_PER_5MIN:
        if _alert(conn, clock, dispatcher, "agent_churn",
                  f"最近 5 分钟 {burst} 个回合（正常日约 5~8、暴跌日峰值约 20，"
                  f"阈值 {AGENT_TURN_CHURN_PER_5MIN}），疑似空转死循环", now):
            fired.append("agent_churn")

    # 心跳本身断了就只判心跳：心跳陈旧时 `loop_last_tick_at` 必然也旧，
    # 两个判据同时命中只会让文案在「进程死了」和「循环卡了」之间反复横跳。
    loop_at = _loop_ts(jobs, now) if jobs_age is not None and jobs_age <= STALE_S else None
    if ((jobs_age is None and missing_ok) or (jobs_age is not None and jobs_age > STALE_S)
            or (loop_at is not None and loop_at > STALE_S)):
        why = "（无心跳）" if jobs_age is None else (
            "（循环未推进）" if loop_at is not None and loop_at > STALE_S else "")
        text = "盘中监控已停止" + why
        if _alert(conn, clock, dispatcher, "jobs_down", text, now):
            fired.append("jobs_down")

    return fired


def _budget_exhausted(conn) -> bool:
    """`agent` 心跳 detail 里的预算耗尽标志。

    **预算耗尽是预期停止，不是调度卡死**：不抑制的话，暴跌日下午每 30 分钟一条误报 P1，
    而正确行为（停下不花额度）看起来跟故障一模一样。
    """
    import json

    row = conn.execute("SELECT detail FROM service_heartbeat WHERE service = 'agent'").fetchone()
    if row is None or not row[0]:
        return False
    try:
        detail = json.loads(row[0]) or {}
    except (TypeError, ValueError):
        return False
    return detail.get("budget_exhausted") is True


def _first_check(conn) -> datetime | None:
    row = conn.execute(
        "SELECT value FROM job_state WHERE name = 'watchdog.first_check'").fetchone()
    return _aware(row[0]) if row and row[0] else None


def _session_start(now: datetime) -> datetime:
    """本时段起点：09:15 或 13:00（午休分界）。

    agent 在午休与隔夜**不唤醒**，所以「距上一回合多久」必须从**本时段起点**算，
    不能从昨天/上午的最后一回合算。
    """
    local = now.astimezone(SH)
    start_hm = (13, 0) if local.strftime("%H:%M") >= "13:00" else (9, 15)
    return local.replace(hour=start_hm[0], minute=start_hm[1], second=0, microsecond=0)


def _last_turn_age(conn, now: datetime, first_check: datetime | None = None) -> float | None:
    """`agent_turn` 最后一行的年龄，**下限是本时段起点**。

    **从未产生过回合时不能返回 None**：那样规则 3 永远不触发，而「心跳在、一次回合都
    没有」恰恰是最该报的情形。改用启动宽限起点算龄，与 `/api/status` 的
    `agent_turn_stale_min` 同一口径。

    **基线取 `max(最后一回合, 本时段起点)`**：agent 在 11:30–13:00 和隔夜休眠，
    直接用全表 `max(ts)` 会让每个交易日 13:00 首轮就报「100 分钟没有回合」——
    上午的最后一回合本来是正常的，误报会把真故障淹掉。
    """
    row = conn.execute("SELECT max(ts) FROM agent_turn").fetchone()
    if not row or not row[0]:
        # 从未产生过回合：用启动宽限起点（**不能**退回时段起点，否则启动 15 分钟后
        # 就报「一次回合都没有」，而那可能只是还没到第一个心跳）
        return None if first_check is None else (now - first_check).total_seconds()
    ts = _aware(row[0])
    if ts is None:
        return None
    # 有回合时基线是 `max(最后一回合, 本时段起点)`：午休/隔夜 agent 不唤醒，
    # 用昨天的最后一回合算龄会让每个交易日开盘首轮都误报。
    return (now - max(ts, _session_start(now))).total_seconds()


def _llm_down(conn, cfg, now: datetime) -> bool:
    """LLM 连续不可用（决定 B3）。两个条件**任一成立**即报：

    1. 连续 `consecutive` 个回合都是 timeout/error；
    2. 已经 `minutes` 分钟没有成功回合了（期间至少有过失败）。

    只写第 1 条时，`minutes` 参数不生效：失败次数不够的门槛下，哪怕已经断了半小时
    也不会报。预算耗尽（`budget_exhausted`）不算——那是预期停止。
    """
    try:
        need = int(cfg.agent.llm_down.consecutive)
        span_min = int(cfg.agent.llm_down.minutes)
    except AttributeError:
        return False
    last_ok = conn.execute(
        "SELECT max(ts) FROM agent_turn WHERE decision IN ('speak','silent')").fetchone()
    last_ok_at = _aware(last_ok[0]) if last_ok and last_ok[0] else None
    # ② 很久没有成功回合。**`last_ok_at` 为 None 时不能直接跳过**：
    # 「从上线起一次都没成功、只零星失败了两次」正是最该报的情形，
    # 用首个失败（没有就用心跳起点）当基线。
    base = last_ok_at
    if base is None:
        # **只看最近 `minutes` 之内**：老数据库里十天前的一条失败会让新部署
        # 一启动就判 llm_down（进程还没机会成功一次）。窗口内没有失败就不报。
        # **基线取窗口内最早的那次失败，且窗口起点本身也算边界**：只用
        # 「now - span」做判据时，正好 10 分钟为 true、10.01 分钟又变 false
        # （两次查询之间基线漂移）。把基线钉在固定的时间点上就没有这个问题。
        #
        # **基线必须是"这一轮故障的起点"，不能每轮从移动窗口里重挑。**
        # 旧实现取「窗口内最早的那次失败」：随着时间推移，最早的失败滑出窗口，
        # 基线就跳到更新的那条失败、门槛重新计时。结果「一直失败」反而
        # 永远凑不满 10 分钟——实测 10:00/10:05 两次失败、无成功记录时，
        # 10:10 判 true，10:10:01 就变回 false，告警在真故障里自己消失。
        #
        # 现在的取法：最近一次成功之后的第一条失败（没有成功记录时，
        # 就是全部历史里的第一条失败）。窗口只用来挡住"陈年旧失败"——
        # 用 idle 起点（`now - minutes`）与故障起点取较大者，
        # 故障持续得越久，`now - base` 越大，门槛只会更容易满足，不会反转。
        fault_start = conn.execute(
            "SELECT min(ts) FROM agent_turn WHERE decision IN ('timeout','error')"
            " AND ts >= ?", (last_ok_at.isoformat() if last_ok_at else "",)).fetchone()
        base = _aware(fault_start[0]) if fault_start and fault_start[0] else None
        if base is None:
            return False                     # 从没有过失败：不判 llm_down
        # 故障起点早于窗口（= 陈年旧账，新部署刚起来）：按窗口起点算
        window_start = now - timedelta(minutes=span_min)
        if base < window_start - timedelta(minutes=span_min):
            return False                     # 太久以前的事，本轮不据此报警
        base = max(base, window_start)
    if base is not None and (now - base).total_seconds() >= span_min * 60:
        failed_since = conn.execute(
            "SELECT count(*) FROM agent_turn WHERE decision IN ('timeout','error') AND ts >= ?",
            (base.isoformat(),)).fetchone()[0]
        if failed_since:
            return True
    # ① 连续失败
    rows = conn.execute(
        "SELECT ts, decision FROM agent_turn ORDER BY id DESC LIMIT ?", (need,)).fetchall()
    if len(rows) < need:
        return False
    if any(r[1] not in ("timeout", "error") for r in rows):
        return False
    return True


def _grace_elapsed(conn, now) -> bool:
    """首次检查记下时间戳；超过 STALE_S 仍无心跳行才算「真的没起」。

    没有这一步，全新库的第一次检查会立刻报两条 P0。
    """
    row = conn.execute("SELECT value FROM job_state WHERE name = 'watchdog.first_check'").fetchone()
    if row is None:
        with conn:
            conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES"
                         " ('watchdog.first_check', ?)", (now.isoformat(),))
        return False
    first = _aware(row[0])
    if first is None:
        return True
    return (now - first).total_seconds() > STALE_S


def _beat(conn, service: str):
    return conn.execute("SELECT last_beat_at, detail FROM service_heartbeat WHERE service = ?",
                        (service,)).fetchone()


def _aware(value: str | datetime) -> datetime | None:
    """解析成带时区的时刻。**naive 一律按上海时间理解**——naive 与 aware 相减会抛
    `TypeError`，而调用方（lifespan）用 `except Exception: pass` 兜住，结果是
    「心跳全停也不告警」的永久静默。"""
    try:
        ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts.replace(tzinfo=SH) if ts.tzinfo is None else ts.astimezone(SH)


def _age_s(row, now: datetime) -> float | None:
    if row is None or not row[0]:
        return None
    ts = _aware(row[0])
    return None if ts is None else (now - ts).total_seconds()


def _loop_ts(row, now: datetime) -> float | None:
    """`detail.loop_last_tick_at` 距今多久（秒）。它是 epoch 秒或 ISO 串两种写法都接受。"""
    if row is None or not row[1]:
        return None
    import json

    try:
        detail = json.loads(row[1]) or {}
    except ValueError:
        return None
    value = detail.get("loop_last_tick_at")
    if not value:
        return None                       # None / 0 / 空串 = 循环今天还没推进过，无从判「停滞」
    if isinstance(value, (int, float)):
        # epoch 秒；负值或明显越界（比 now 还晚一天以上）说明是脏数据/上一交易日的值
        age = now.timestamp() - float(value)
        return age if 0 <= age <= 86400 else None
    ts = _aware(value)
    if ts is None:
        return None
    age = (now - ts).total_seconds()
    return age if 0 <= age <= 86400 else None


def _alert(conn, clock, dispatcher, kind: str, text: str, now: datetime,
           level: str = "P0") -> bool:
    """返回是否真的发了（30 分钟内已成功告警 → False）。"""
    cutoff = (now - timedelta(seconds=REPEAT_S)).isoformat()
    recent = conn.execute(
        "SELECT 1 FROM push_log WHERE kind = ? AND status = 'accepted' AND ts > ? LIMIT 1",
        (kind, cutoff),
    ).fetchone()
    if recent is not None:
        return False                        # 30 分钟内已成功告警过，不重复
    # 告警直接由 api 发（§7.6.4）：jobs/agent 挂了还要靠它们转发的设计没有意义
    dispatcher.alert(kind, text, level=level)
    return True
