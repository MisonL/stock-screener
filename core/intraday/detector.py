"""沿触发事件检测器：把每 tick 的判定列表变成「只在新条件成立时才发生一次」的事件。

三条容易写错的执行口径：

1. **沿触发**：同一 `(kind, target)` 只在「未武装 → 成立」时产生一次事件；条件不成立即重新武装。
   否则上沿附近来回摆动一上午会刷出几十条同样的突破。
2. **重启不重复**：`seed_today()` 从今天的 `event_log` 恢复 `seq` 与武装状态，重启后条件仍
   成立也不会再推一次（Review Focus #4）。
3. **分级只有一个来源**：`level_for(det, cfg)` 同时被 `signals.level` 与 Task 23 的推送判级使用。
   `extended`（现价已超箱体上沿 ×c3）**最优先**——排在 breakout 之前，否则高分的追高票永远
   降不到 P2（spec §4.2 C 组：C3 只降级、不发否）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from core.intraday.rules import Detection
from core.intraday.salience import salience
from core.market import classify_market

# 哪些 kind 是「价格回到原位即重新武装」的在线条件
_PRICE_KINDS = {"breakout", "breakdown", "near_breakout", "stall", "watch_rule"}
_CHANNEL_A = {"breakout", "breakdown"}          # 通道 A：硬信号，写 signals 且不过 salience 门控


@dataclass(frozen=True)
class NewEvent:
    event_id: int
    signal_id: int | None
    detection: Detection
    salience: float
    seq: int


def level_for(det: Detection, cfg) -> str:
    """P0 / P1 / P2。阈值来自 `cfg.notify.p0`，不在这里写死 80 / 2.5。"""
    p0 = cfg.notify.p0
    if det.extended:
        return "P2"                       # 必须最先判：追高只降级，不发否
    if det.kind == "breakdown":
        return "P0"                       # panic 不降级
    if det.kind == "breakout":
        score = det.payload.get("score") or 0.0
        multiple = det.payload.get("box_vol_multiple") or 0.0
        if score >= p0.score and multiple >= p0.vol:
            return "P0"
        # **`notify.p1_score` 真的生效**（v0.2.105）：审计发现这个键全仓零读取，
        # 旧实现是"非 P0 的突破一律 P1"，60 分这个闸门完全不存在——
        # 运维以为 P1 有分数门槛，实际不分青红皂白全 P1，
        # 直接放大 `p1_merge` 的合并压力与"该不该响"的误判。
        #
        # 默认 **0 = 不设闸门**，与旧行为完全一致（接线不改变运行时行为）；
        # 真要求闸门时把 `p1_score` 改成 60 之类即可。
        p1 = getattr(cfg.notify, "p1_score", 0) or 0
        return "P1" if score >= p1 else "P2"
    return "P2"


def _scope_of(det: Detection) -> str:
    if det.kind == "index_move":
        return "market"
    if det.kind == "sector_move":
        return "sector"
    return "stock"


def _market_of(det: Detection) -> str:
    if det.kind == "index_move":
        return "index"
    if det.kind == "sector_move":
        return "sector"
    return classify_market(det.code) or ""


class EventDetector:
    def __init__(self, conn, clock, cfg):
        self.conn = conn
        self.clock = clock
        self.cfg = cfg
        self._armed: dict[tuple, bool] = {}
        self._seq: dict[tuple, int] = {}
        self._last_fire: dict[tuple, datetime] = {}
        self._daily_fired: set[tuple] = set()

    # ---- 重启恢复 --------------------------------------------------------

    def seed_today(self) -> None:
        today = self.clock.today()
        for row in self.conn.execute(
            "SELECT kind, target, payload, dedupe_key, ts FROM event_log WHERE trade_date = ?", (today,)
        ):
            fire_key = (row["kind"], row["target"])
            if row["kind"] == "data_anomaly":
                # 除权/停牌按 reason 各记一次：同一只票当天既除权又停牌是两件事
                fire_key = (row["kind"], f"{row['target']}:{_reason_of(row['payload'])}")
            # seq 仍按 (kind, target) 递增——dedupe_key 的形状是计划的公共接口，不塞 reason
            seq_key = (row["kind"], row["target"])
            seq = int(str(row["dedupe_key"]).rsplit(":", 1)[1])
            self._seq[seq_key] = max(self._seq.get(seq_key, 0), seq)
            self._armed[fire_key] = False     # 已触发、未重新武装
            self._daily_fired.add(fire_key)
            # 冷却的计时基准是 fire 时间：重启后从 event_log.ts 恢复，否则重启一次冷却清零。
            # **价格类也要恢复**：只恢复 index/sector 时，重启后第一次满足条件立刻再报
            # 一条（实测 11-11 当天重启 5 次，每两次重启之间同票都重报过）。
            if row["kind"] in ("index_move", "sector_move") or row["kind"] in _PRICE_KINDS:
                ts = datetime.fromisoformat(row["ts"])
                prev = self._last_fire.get(fire_key)
                if prev is None or ts > prev:                  # 行序不保证：取最大
                    self._last_fire[fire_key] = ts

    # ---- 主入口 ----------------------------------------------------------

    def ingest(self, dets: list[Detection], focus_of) -> list[NewEvent]:
        out: list[NewEvent] = []
        present = {(d.kind, d.code) for d in dets}
        p = self.cfg.strategy.attention

        # 本 tick 没再出现的价格类条件 → 重新武装，但**必须过冷却期**。
        # 只靠「一个 tick 没出现就重置」时，价格在上沿附近抖动（量能条件时满时不满足）
        # 会让同一只票几秒内反复触发：实测 300052 在 8 分钟里出了 19 条 breakout，
        # 还挤进同一批推送（`batch-804` 里同一只重复 8 次）。
        cooldown_s = float(self.cfg.strategy.intraday.price_rearm_min) * 60
        now = self.clock.now()
        for key in list(self._armed):
            if key[0] in _PRICE_KINDS and key not in present:
                last = self._last_fire.get(key)
                if last is None or (now - last).total_seconds() >= cooldown_s:
                    self._armed[key] = True

        for det in dets:
            fire_key = self._key_of(det)
            if not self._should_fire(fire_key, det):
                continue
            seq_key = (det.kind, det.code)
            seq = self._seq.get(seq_key, 0) + 1
            self._seq[seq_key] = seq
            self._armed[fire_key] = False
            self._last_fire[fire_key] = self.clock.now()
            if det.kind == "data_anomaly":
                self._daily_fired.add(fire_key)

            value = salience(det.kind, _scope_of(det), self._focus(det, focus_of),
                             merged_n=_merged_n(det), p=p)
            out.append(self._persist(det, seq, value))
        return out

    @staticmethod
    def _key_of(det: Detection) -> tuple:
        if det.kind == "data_anomaly":
            return (det.kind, f"{det.code}:{det.payload.get('reason')}")
        return (det.kind, det.code)

    def _should_fire(self, key, det: Detection) -> bool:
        kind, code = key
        if kind == "data_anomaly":
            return key not in self._daily_fired
        if kind in ("index_move", "sector_move"):
            last = self._last_fire.get(key)
            cooldown = self._cooldown_min(kind)
            if last is not None and (self.clock.now() - last).total_seconds() < cooldown * 60:
                return False
            return True
        return self._armed.get(key, True)

    def _cooldown_min(self, kind: str) -> int:
        intraday = self.cfg.strategy.intraday
        if kind == "index_move":
            return intraday.index_move.cooldown_min
        return intraday.sector_move.cooldown_min

    def _focus(self, det: Detection, focus_of) -> float:
        p = self.cfg.strategy.attention
        if det.watch:
            return float(p.focus.watch)       # D6：自选股直接读 det.watch，不靠 kind
        if det.kind == "index_move":
            return float(p.focus.market)
        if det.kind == "sector_move":
            return float(p.focus.sector)
        raw = focus_of(det.code) if focus_of else "pool"
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(p.focus.get(str(raw), p.focus.outside))

    # ---- 落库 ------------------------------------------------------------

    def _persist(self, det: Detection, seq: int, value: float, merged_n: int = 1) -> NewEvent:
        now = self.clock.now()
        today = self.clock.today()
        market = _market_of(det)
        scope = _scope_of(det)
        payload = {
            **det.payload,
            "watch": det.watch,
            "extended": det.extended,
            "panic": det.panic,
            "sealed": det.sealed,
        }
        body = json.dumps(payload, ensure_ascii=False)
        threshold = float(self.cfg.strategy.attention.salience_min)
        escalated = det.kind in _CHANNEL_A or value >= threshold
        status = "new" if escalated else "suppressed"
        dedupe_key = f"{det.kind}:{det.code}:{today}:{seq}"

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO event_log(ts, trade_date, market, kind, scope, target, payload, salience, "
                "agent_status, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now.isoformat(), today, market, det.kind, scope, det.code, body, value, status, dedupe_key),
            )
            event_id = cur.lastrowid
            signal_id = None
            if det.kind in _CHANNEL_A:
                cur = self.conn.execute(
                    "INSERT INTO signals(ts, trade_date, market, code, kind, level, event_id, seq, payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (now.isoformat(), today, market, det.code, det.kind, level_for(det, self.cfg),
                     event_id, seq, body),
                )
                signal_id = cur.lastrowid
        return NewEvent(event_id, signal_id, det, value, seq)


def _reason_of(payload) -> str:
    try:
        return str(json.loads(payload).get("reason") or "")
    except (TypeError, ValueError):
        return ""


def _merged_n(det: Detection) -> int:
    return int(det.payload.get("merged_n") or 1)
