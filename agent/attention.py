"""注意力层（计划 Task 7，spec §5.4.3）：决定「这一批事件要不要把 agent 叫起来」。

七条判定，顺序不能变：

1. 只取 `trade_date = today` 的事件——上线首日若不滤，会把空跑期积攒的历史 `new`
   一次性消费掉，日预算当场烧光（架构 F3）。
2. 合并：同 `(industry, kind)` 在 `cfg.notify.sector_merge.window_s`（=300 秒，与批1
   dispatcher 同一份配置）内 ≥ 3 只并成一个 `merged` 组。
3. 去重：同 `(code, kind)` 30 分钟内只升级一次，多余的标 `done` 并写 `decision='deduped'`。
4. 通道 A（`breakout`/`breakdown`）直接放行；通道 B 过疲劳阈值。
5. **跑回合之前**把组内事件标 `escalated`（`agent_status` + `escalated=1` + `escalated_at`）。
6. 一个组只含一个市场。
7. `pending_retry` 只交给心跳，不进事件唤醒。

`shadow_mode=True`（默认）时只写 `agent_turn(decision='shadow')` 留痕，不升级、不跑回合。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from notify.policy import channel_b_today

# 通道归属（§5.4.3.1）：C/C′ 组（策略层自己算出来的）走 A，agent 觉得该说的走 B
CHANNEL_A_KINDS = frozenset({"breakout", "breakdown"})
CHANNEL_B_KINDS = frozenset({"index_move", "sector_move", "near_breakout", "stall",
                             "auction_anomaly", "watch_rule", "data_anomaly"})
DEDUPE_MIN = 30
MERGE_COUNT = 3
STATUS_NEW = "new"


def channel_of(kind: str) -> Literal["A", "B"]:
    """唯一的通道映射函数：`poll()` 与测试都用它，别处不得再写一份 if。"""
    return "A" if kind in CHANNEL_A_KINDS else "B"


@dataclass
class EventRow:
    """`event_log` 的一行（字段名与 §5.3 DDL 逐列同名；`payload` 已是 dict）。"""

    id: int
    ts: str
    trade_date: int
    market: str
    kind: str
    scope: str
    target: str | None
    payload: dict
    salience: float
    escalated: int = 0
    turn_id: int | None = None
    agent_status: str = "new"
    escalated_at: str | None = None
    dedupe_key: str = ""

    @property
    def code(self) -> str | None:
        return self.target or self.payload.get("code")

    @property
    def industry(self) -> str | None:
        return self.payload.get("industry") or self.payload.get("sector")


@dataclass
class WakeGroup:
    id: str
    market: str
    kind: Literal["event", "merged"]
    events: list[EventRow] = field(default_factory=list)
    channel: Literal["A", "B"] = "B"
    salience: float = 0.0
    #: 组里是否有**自选股**（`watch_rule`）事件。
    #: 自选股不吃疲劳阈值——见 `poll()` 里放行条件处的说明。
    watch: bool = False


def _is_watch(e: EventRow) -> bool:
    """这条事件是否来自**自选股**（`watch_rule`）。

    只认 kind，不看 payload 里的 `watch` 字段：`watch_rule` 全部由
    `core/intraday/watch_rules.py::eval_watch()` 产出，语义唯一。
    """
    return e.kind == "watch_rule"


class AttentionLayer:
    def __init__(self, conn, cfg, clock):
        self.conn = conn
        self.cfg = cfg
        self.clock = clock

    # ---- 读取 ------------------------------------------------------------

    def _new_events(self, today: int) -> list[EventRow]:
        rows = self.conn.execute(
            "SELECT id, ts, trade_date, market, kind, scope, target, payload, salience, escalated,"
            " turn_id, agent_status, escalated_at, dedupe_key FROM event_log"
            " WHERE agent_status IN ('new','pending_retry') AND trade_date = ?"
            " ORDER BY id", (today,)).fetchall()
        out = []
        for r in rows:
            if r[11] == "pending_retry":
                continue                     # ⑦ 只交给心跳
            try:
                payload = json.loads(r[7]) if r[7] else {}
            except (TypeError, ValueError):
                payload = {}
            out.append(EventRow(id=r[0], ts=r[1], trade_date=r[2], market=r[3], kind=r[4],
                                scope=r[5], target=r[6], payload=payload, salience=float(r[8]),
                                escalated=int(r[9] or 0), turn_id=r[10], agent_status=r[11],
                                escalated_at=r[12], dedupe_key=r[13] or ""))
        return out

    # ---- 判定 ------------------------------------------------------------

    def effective_threshold(self) -> float:
        """`salience_min × (1 + raise × max(0, n − free) / step)`（决定 B9）。

        `raise` 不能漏：写成 `1 + n/step` 时 n=20 得 1.5，比带 raise 的 1.0 高一半，
        `fatigue.raise` 就成了死配置。
        """
        f = self.cfg.agent.fatigue
        n = channel_b_today(self.conn, self.clock.today())
        extra = max(0, n - int(f.free)) / int(f.step)
        rate = float(getattr(f, "raise"))
        return float(self.cfg.strategy.attention.salience_min) * (1 + rate * extra)

    def _merge(self, events: list[EventRow], now_ts: float) -> tuple[list[WakeGroup], list[EventRow]]:
        """同 (market, industry, kind) 且**相对首条** ≤ 窗口的连续前缀 → 合并。

        **不能用首尾跨度做整桶判据**：12 只每 150 秒一只（30 分钟内）时首尾跨度 1650 秒，
        整桶会被判成「窗口外」而退化成 12 个单组——洪峰场景在生产数据上根本不成立。
        也不能不排序就用首尾：乱序 ts 会把 900 秒外的事件并进同一个组。
        """
        window = int(self.cfg.notify.sector_merge.window_s)
        buckets: dict[tuple, list[EventRow]] = {}
        singles: list[EventRow] = []
        for e in events:
            # ⑥ 一个组只含一个市场：key 必须带 market，否则 cn_main 与 cn_gem 的
            # 同行业事件会被并成一组，组上的 market 只反映第一只
            key = (e.market, e.industry, e.kind)
            if not e.industry:
                singles.append(e)
                continue
            buckets.setdefault(key, []).append(e)

        groups: list[WakeGroup] = []
        for (_market, industry, kind), items in buckets.items():
            items.sort(key=lambda e: e.ts)      # 不排序 → 乱序 ts 会把越窗事件并进来
            i = 0
            while i < len(items):
                j = i
                while j + 1 < len(items) and _within(items[i].ts, items[j + 1].ts, window):
                    j += 1
                run = items[i:j + 1]
                if len(run) >= MERGE_COUNT:
                    groups.append(WakeGroup(
                        id=f"merged-{run[0].id}-{len(run)}", market=run[0].market,
                        kind="merged", events=list(run), channel=channel_of(kind),
                        salience=_merged_salience(run, self.cfg),
                        watch=any(_is_watch(e) for e in run)))
                    i = j + 1                   # 整段消费掉
                else:
                    # **只追加锚点这一条**。整段 extend 的话，`i += 1` 后下一轮
                    # 又会把 run[1] 当锚点重新收进窗内，同一条事件被 append 两三次
                    # （非 shadow 模式会因此多跑回合、日志里能看到重复事件 id）。
                    singles.append(items[i])
                    i += 1                      # 从下一条重新起窗
        return groups, singles

    def _deduped(self, e: EventRow, now_ts: float) -> bool:
        """同 (code, kind) 30 分钟内已经放行过一次吗？

        **必须同时认 `escalated` 与 `done`**：回合成功后 `journal.record` 会把
        `escalated` 改成 `done`，只查 `escalated` 就永远匹配不到「已推过」的记录——
        去重对真实链路完全失效，同一条消息会被反复推。`gave_up`（从未送达）不算。
        """
        since = self.clock.now().timestamp() - DEDUPE_MIN * 60
        row = self.conn.execute(
            "SELECT ts FROM event_log WHERE agent_status IN ('escalated','done') AND kind = ?"
            " AND COALESCE(NULLIF(target, ''), json_extract(payload, '$.code')) = ?"
            " AND ts >= ? LIMIT 1",
            (e.kind, e.code or "", _iso_of(since, self.clock.now()))).fetchone()
        return row is not None

    # ---- 主流程 ----------------------------------------------------------

    def poll(self) -> list[WakeGroup]:
        today = self.clock.today()
        now_ts = self.clock.now().timestamp()
        events = self._new_events(today)
        if not events:
            return []

        merged, singles = self._merge(events, now_ts)
        # 默认必须是 shadow（spec §10「上线头两周只记录不升级」）：配置缺这一段时
        # 若默认成「升级」，一个漏配的 agent.yaml 会让 agent 立刻开始推消息
        attention_cfg = getattr(self.cfg.agent, "attention", None)
        shadow = getattr(attention_cfg, "shadow_mode", True) is not False

        # 去重先于疲劳：被去重的事件不该继续参与后面的判定。
        # **合并组的成员也要过**：组内某只 30 分钟内已经说过，这次合并再放行一次
        # 就是同一只票一天说两遍——合并是「少说」，不是「绕过去重」。
        kept: list[EventRow] = []
        # 事件 id → 行，用于写留痕（合并组成员手里只有 id）
        by_id = {e.id: e for e in events}
        deduped_ids: list[int] = []
        for e in singles:
            if self._deduped(e, now_ts):
                deduped_ids.append(e.id)
                continue
            kept.append(e)
        surviving_merged: list[WakeGroup] = []
        for g in merged:
            survivors = []
            for e in g.events:
                if self._deduped(e, now_ts):
                    deduped_ids.append(e.id)
                    by_id.setdefault(e.id, e)
                else:
                    survivors.append(e)
            if len(survivors) >= MERGE_COUNT:
                g.events = survivors
                surviving_merged.append(g)
            else:
                kept.extend(survivors)           # 跌破 3 只就不再是「板块级」
        merged = surviving_merged
        if deduped_ids:
            # **标记与审计行同一事务**：先 commit `done` 再写日志，中途失败会让事件
            # 退出新事件查询、却永远没有对应的 agent_turn 行——日志页看不到，
            # 事后也查不出为什么没推。
            with self.conn:
                self._mark_many(deduped_ids, "done", commit=False)
                for eid in deduped_ids:
                    self._write_turn(by_id[eid], "deduped",
                                     "30 分钟内已升级过同类事件", commit=False)

        singles_groups = [WakeGroup(id=f"event-{e.id}", market=e.market, kind="event",
                                    events=[e], channel=channel_of(e.kind), salience=e.salience,
                                    watch=_is_watch(e))
                          for e in kept]

        if shadow:
            # **shadow 在疲劳门之前分支**：§10 说「上线头两周只记录不升级」，就是为了
            # 看真实分布。这时还没有任何真实推送，`effective_threshold` 恒等于基础阈值，
            # 拿它把事件标成 suppressed 既违背口径，又会让这些事件永久沉底。
            #
            # **但必须标记状态**（v0.2.94 修的根因）。原来这里只写 shadow 回合、
            # 不改 `agent_status`，事件就一直停在 `new`；而 `poll()` 只查
            # `trade_date = 今天`，**次日它们再也取不到**，于是既不 done 也不
            # suppressed 地沉底。生产实测：shadow 观测期 11-10~11-17 每天积
            # 1~63 条，合计 142 条僵尸行。
            #
            # 用一个独立状态 `shadowed`（不是 `done`）：它们在 shadow 期被
            # **观测过但没有处理**，记成 done 会让"处理率"这类口径说谎。
            shadowed_ids: list[int] = [e.id for g in singles_groups + merged
                                       for e in g.events]
            for g in singles_groups + merged:
                self._write_turn(g.events[0], "shadow", "只记录不升级",
                                 digest=f"shadow 组={g.id} 事件={len(g.events)}",
                                 commit=False)
            # **回合行与状态标记同一事务**：先 commit 回合、再改状态，中途失败会让
            # 事件留在 `new` 却已经有 shadow 回合行——下次 poll 又写一遍（幂等挡住
            # 重复行），但状态永远不动。反过来先改状态再写回合，失败就丢观测。
            self._mark_many(shadowed_ids, "shadowed")
            return []

        threshold = self.effective_threshold()
        allowed: list[WakeGroup] = []
        suppressed: list[int] = []
        for g in singles_groups + merged:
            # ④ 通道 A 不看疲劳
            #
            # **自选股也不看疲劳**（2026-11-27）：`watch_rule` 的显著度是
            # `base 0.45 × focus.watch 1.3 = 0.585`，而疲劳阈值在当天推满 13 条后
            # 就涨到 0.65 以上——**自选股告警在历史上一条都没推出去过**
            # （全库 `event_log` 只有 2 条 `watch_rule`，全被 suppressed，
            # `push_log` 里 0 条）。用户手动加的票是**最高意图**，
            # 不该被"今天已经说够了"这类全局疲惫度压住；
            # 刷屏风险由 30 分钟去重（`notify.dedupe_min`）与通道 B 日上限兜住
            # ——实测茅台一天触发 2 次，去重后只剩 1 次。
            if g.channel == "A" or g.watch or g.salience >= threshold:
                allowed.append(g)
            else:
                suppressed.extend(e.id for e in g.events)

        now_iso = self.clock.now().isoformat()
        escalated = [e.id for g in allowed for e in g.events]
        if suppressed:
            self._mark_many(suppressed, "suppressed")
        if escalated:
            self._mark_many(escalated, "escalated", escalated_at=now_iso)   # ⑤ 跑回合之前
        return allowed

    # ---- 写 --------------------------------------------------------------

    def _mark_many(self, ids: list[int], status: str, *,
                   escalated_at: str | None = None, commit: bool = True) -> None:
        """批量改状态：**一条 UPDATE、一次 commit**。

        原来是每条事件一次 `execute` + 一次 `commit`——30 条不可合并事件就是每 2 秒
        122 条 SQL（32 SELECT + 30×(BEGIN/UPDATE/COMMIT)）。固定窗口内批量更新即可。
        """
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        if status == "escalated":
            self.conn.execute(
                f"UPDATE event_log SET agent_status='escalated', escalated=1, escalated_at=?"
                f" WHERE id IN ({marks})", (escalated_at, *ids))
        else:
            self.conn.execute(
                f"UPDATE event_log SET agent_status=? WHERE id IN ({marks})", (status, *ids))
        if commit:
            self.conn.commit()

    def _mark(self, e: EventRow, status: str, *, escalated_at: str | None = None) -> None:
        if status == "escalated":
            # `escalated` 列必须一起置 1：只改 agent_status 会让该列恒 0、无任何写者
            self.conn.execute(
                "UPDATE event_log SET agent_status = 'escalated', escalated = 1, escalated_at = ?"
                " WHERE id = ?", (escalated_at, e.id))
        else:
            self.conn.execute("UPDATE event_log SET agent_status = ? WHERE id = ?", (status, e.id))
        self.conn.commit()

    def _write_turn(self, e: EventRow, decision: str, why: str,
                    digest: str | None = None, commit: bool = True) -> None:
        """shadow / deduped 行手写 INSERT（Task 7 早于 Task 10，不能 import write_turn_row）。

        `trigger_id` 写**真实 event_log.id**（不是组 id，该列语义是「对应事件」）；
        写之前先按 `trigger_id + decision` 幂等去重——否则每 2 秒一次的 poll 会重复写，
        正好是「一天上千条」的成因。
        """
        exists = self.conn.execute(
            "SELECT 1 FROM agent_turn WHERE trigger_id = ? AND decision = ? LIMIT 1",
            (e.id, decision)).fetchone()
        if exists is not None:
            return
        self.conn.execute(
            "INSERT INTO agent_turn(ts, wake_kind, trigger_id, context_digest, rounds, decision,"
            " why, fallback, market) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.clock.now().isoformat(), "event", e.id,
             digest or f"{decision} 事件={e.id}", 0, decision, why, 0, e.market))
        if commit:
            self.conn.commit()


# ---------------------------------------------------------------- 小工具


def _within(a: str, b: str, window: int) -> bool:
    """`b` 相对锚点 `a` 是否还在合并窗口内。

    锚点是**窗内首条**，不是上一条：否则 12 只每 150 秒一只会首尾跨度 1650 秒，
    整桶被判成窗外（实测退化为 12 个单组，洪峰场景直接不成立）。
    """
    from datetime import datetime

    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()) <= window
    except (TypeError, ValueError):
        return False


def _iso_of(epoch: float, now) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch, tz=now.tzinfo).isoformat()


def _merged_salience(items: list[EventRow], cfg) -> float:
    """合并组的显著性按 §5.4.3 重算：`base × (1 + 0.30) × focus`，不是组内最大值。"""
    from core.intraday.salience import salience as salience_fn

    att = cfg.strategy.attention
    kind = items[0].kind
    # 合并组跨多只票 → 按 sector 口径重算，才有 `sector_merged_3` 的 +0.30 广度加成；
    # 沿用组内单只事件的 scope=stock 会得到 0.45（等于没合并）
    scope = "market" if items[0].scope == "market" else "sector"
    focus = att.focus.market if scope == "market" else att.focus.sector
    return salience_fn(kind, scope, float(focus), merged_n=len(items), p=att)
