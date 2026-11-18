"""分级派发层：把 `NewEvent` 变成通知，并按规则去重 / 合并 / 熔断（§5.4.3、§7.2）。

规则顺序（每一步都从配置读阈值，不写死字面量）：

1. **同 code 同 kind 去重**：`dedupe_min`（30）分钟内已推过 → `suppressed`。查库不靠内存，
   所以重启后仍然生效（Review Focus #4）。
2. **当日熔断**：`daily_cap`（60）条后只放行 P0。计数只认 `pending/accepted`——
   `suppressed`/`failed` 不是「已推」，算进去会形成「越压越压」的正反馈。
3. **池子上溢**：`pool_overflow`（150）只以上，所有 P1 进 5 分钟汇总。
4. **P1 合并**：`p1_merge`（60 秒内第 3 条起）合成一条 P2。
5. **板块级合并**：同行业 `sector_merge.window_s` 内第 `count` 只起并入 P2；P0 也适用（D7）。
"""
from __future__ import annotations

import json
from datetime import timedelta

from core.intraday.detector import level_for
from notify.hmac_sign import ensure_secret
from notify.policy import NON_PUSH_KINDS, pushed_today, recent_independent_push
from notify.drill import drill_skip
from notify.templates import (BriefRow, CloseStats, Message, SigRow, render_alert, render_batch,
                              render_breakdown, render_breakout, render_close_digest,
                              render_open_brief)

_EVENT_KINDS = ("breakout", "breakdown")


class Dispatcher:
    def __init__(self, conn, ntfy, cfg, clock):
        self.conn = conn
        self.ntfy = ntfy
        self.cfg = cfg
        self.clock = clock
        self.secret = ensure_secret(conn)
        # 合并缓冲存 `(入队时刻, 信号)`：**窗口没到不能 flush**——每秒一 flush 的话
        # 缓冲区永远只有 1 条，「第 3 只起才合并」就退化成「每条各发一条 P2」，
        # 普跌日 30 只跌破 = 28 条独立推送，正是 spec §5.4.3 要拦的洪峰。
        self._pending: list[tuple[datetime, SigRow]] = []     # P1 60 秒合并缓冲
        self._sector: list[tuple[datetime, SigRow]] = []      # 板块级合并缓冲
        self._overflow: list[tuple[datetime, SigRow]] = []    # 池子上溢 5 分钟汇总
        self._fail_streak = 0

    # ---- 事件入口 --------------------------------------------------------

    def handle(self, events: list) -> None:
        for ev in events:
            det = ev.detection
            if det.kind not in _EVENT_KINDS:
                continue                          # 只有通道 A 的两类进推送
            sig = load_sig(self.conn, ev.event_id)
            if sig is None:
                continue                          # 数据缺块：少推一条，不让循环崩
            self._route(sig)
        # 合并缓冲**不在这里 flush**：`handle` 由 pool/sector 线程调用，合并窗口是「秒级」
        # 概念，flush 交给每秒的 `tick()`（与 spec 的 60 秒窗口一致）。

    def _route(self, sig: SigRow) -> None:
        if self._deduped(sig):
            self._log(sig, "suppressed")
            return
        pinned = sig.level == "P0"
        # **额度不再在这里判**：事务外读到的是过期快照，会和另一个服务一起超限。
        # 真正的判定在 `_push_rows` 的 `BEGIN IMMEDIATE` 里（写锁到手后再读）。
        # 这里只负责把"超限"表达成 suppressed 日志——代价是合并缓冲也要走一遍，
        # 但额度检查本来就是廉价查询。
        now = self.clock.now()
        if not pinned and self._pool_overflowing():
            self._overflow.append((now, sig))
            return
        if self._sector_merge_due(sig):
            self._sector.append((now, sig))
            return
        if not pinned and self._p1_merge_due(sig):
            self._pending.append((now, sig))
            return
        self._send(sig, self._render(sig), sig.level)

    # ---- 合并缓冲区 ------------------------------------------------------

    def tick(self) -> None:
        """每秒 job 调：到点的合并缓冲在这里发出（也是唯一 flush 点）。"""
        # **先丢跨日的，再 flush**：反过来的话，隔夜的缓冲会被判定「窗口早就到了」
        # 而立刻发出去，正是要避免的陈旧推送
        self._drop_stale()
        self._flush_due()

    def _drop_stale(self) -> None:
        """跨日残留缓冲直接丢弃：15:05 后 tick 不再跑，最后进缓冲的项会等到**次日**
        才发出——`seq_id`、文案、`trade_date` 全是昨天的，比"少推一条"糟得多。"""
        today = self.clock.today()
        dropped = 0
        for name in ("_pending", "_sector", "_overflow"):
            buf = getattr(self, name)
            keep = [(t, s) for (t, s) in buf if int(t.strftime("%Y%m%d")) == today]
            dropped += len(buf) - len(keep)
            setattr(self, name, keep)
        return dropped

    def _flush_due(self) -> None:
        """到点的缓冲才发：**窗口从最早一条入队时刻算起**，未到点继续攒。"""
        now = self.clock.now()
        for buf_name, window in (("_sector", self.cfg.notify.sector_merge.window_s),
                                 ("_pending", self.cfg.notify.p1_merge.window_s),
                                 ("_overflow", self.cfg.notify.overflow_digest_s)):
            buf = getattr(self, buf_name)
            if not buf:
                continue
            oldest = buf[0][0]
            if (now - oldest).total_seconds() < float(window):
                continue                       # 窗口未满：继续等，合并才有意义
            batch = [sig for _, sig in buf]
            setattr(self, buf_name, [])
            if buf_name == "_sector":
                # 同窗口里不同行业/方向的混在一批发出去，标题取首只行业——
                # 「银行」里混进券商 breakdown。按 (industry, kind) 分组发。
                groups: dict[tuple, list] = {}
                for sig in batch:
                    groups.setdefault((sig.industry, sig.kind), []).append(sig)
                for group in groups.values():
                    self._send_batch(group, level="P2")
            else:
                self._send_batch(batch, level="P2")

    def _send_batch(self, batch: list[SigRow], level: str = "P2") -> None:
        # **同批同票去重**：检测层的冷却期是防止重复触发的主力，这里是最后一道
        # 兜底——实测 300052 在一批里出现 9 次，正文「11 只同向异动」实际只有 4 只。
        # 保留首条（信号顺序即时间顺序）。
        seen: set[tuple[str, str]] = set()
        uniq: list[SigRow] = []
        for sig in batch:
            key = (sig.code6, sig.kind)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(sig)
        batch = uniq
        if not batch:
            return
        msg = render_batch(batch, self.secret, self.cfg)
        # seq_id 与 render_batch 同口径（batch-{min event_id}）：两处不一致的话，
        # 同一条推送落库的 seq 与实际发出的序列号分叉，事后按 X-Sequence-ID 对不上账
        seq_id = f"batch-{min(b.event_id for b in batch)}"
        # **批次内每一只都写一行**：只写首只的话其余成员不进 push_log，30 分钟去重
        # 对它们失效（同批第二只 `_deduped` 为 False，窗口内会再推一次）。
        # 同一 seq_id、同一 body、segment=1，第二段仍按 event_id 取到代表行。
        rows = [self._row(sig, kind=sig.kind, level=level, seq_id=seq_id) for sig in batch]
        # 合并批次也是**行情信号**（通道 A），照旧受日额度约束
        self._push_rows(rows, msg, claim=True, enforce_cap=True)

    # ---- 规则判定 --------------------------------------------------------

    def _deduped(self, sig: SigRow) -> bool:
        """同 code 同 kind 的去重。**`kind` 必须传**：只按 code 查会把「同一票先突破
        后跌破」误判成重复，把新事实压掉。口径与批2 共用 `notify/policy.py` 一份。"""
        return recent_independent_push(self.conn, sig.code6, kind=sig.kind,
                                       minutes=int(self.cfg.notify.dedupe_min),
                                       now=self.clock.now()) is not None

    def _capped(self) -> bool:
        day = self.clock.today()
        # 按 seq_id 去重：合并批为每只成员各写一行，按行数算等于把 1 条消息算成 N 条，
        # 日熔断会被提前触发。
        # 口径与批2 共用 policy 一份：只计「新通知」——第二段是同一 seq 的更新、
        # 确认卡是交互卡、confirm_result 是审计行，三者都不占额度
        n = pushed_today(self.conn, exclude_segments=(2,), exclude_kinds=NON_PUSH_KINDS,
                         day=day)
        return n >= self.cfg.notify.daily_cap

    def _pool_overflowing(self) -> bool:
        sizes = self._pool_sizes()
        return sum(sizes.values()) > int(self.cfg.strategy.universe.pool_overflow)

    def _p1_merge_due(self, sig: SigRow) -> bool:
        window_start = (self.clock.now() - timedelta(
            seconds=self.cfg.notify.p1_merge.window_s)).isoformat()
        # 只数**真的发出去过**的 P1：把 suppressed 也算进来会让合并提前触发，越压越合并
        n = self.conn.execute(
            "SELECT count(*) FROM push_log WHERE ts > :w AND level = 'P1'"
            " AND kind IN ('breakout','breakdown') AND status IN ('pending','accepted')",
            {"w": window_start}).fetchone()[0]
        return n + 1 >= int(self.cfg.notify.p1_merge.count)

    def _sector_merge_due(self, sig: SigRow) -> bool:
        if not sig.industry:
            return False
        window_start = (self.clock.now() - timedelta(
            seconds=self.cfg.notify.sector_merge.window_s)).isoformat()
        n = self.conn.execute(
            "SELECT count(*) FROM push_log p JOIN stock_meta m ON m.code LIKE '%' || p.code"
            " WHERE p.ts > :w AND m.industry = :industry AND p.kind = :kind"
            " AND p.status IN ('pending','accepted')",
            {"w": window_start, "industry": sig.industry, "kind": sig.kind},
        ).fetchone()[0]
        return n + 1 >= int(self.cfg.notify.sector_merge.count)

    # ---- 渲染与落库 ------------------------------------------------------

    def _render(self, sig: SigRow) -> Message:
        if sig.kind == "breakdown":
            return render_breakdown(sig, self.secret, self.cfg)
        return render_breakout(sig, self.secret, self.cfg)

    def _row(self, sig: SigRow, kind: str, level: str, seq_id: str) -> dict:
        return {"code": sig.code6, "kind": kind, "level": level, "seq_id": seq_id,
                "event_id": sig.event_id, "signal_id": sig.signal_id, "segment": 1,
                "trade_date": sig.event_trade_date}

    def _send(self, sig: SigRow, msg: Message, level: str) -> None:
        # **必须带 kind**：同一只票同一天首次突破与首次跌破都是 seq=1，
        # 只按 code+date+seq 生成会让第二条撞上认领键，被静默吞掉
        seq_id = _seq_id(sig)
        # **P0 豁免额度**（原来是 `_route` 里 `not pinned` 挡住检查）：
        # 判定移进事务后，豁免要跟着传下去，否则 P0 会被额度挡掉。
        self._push_log(row=self._row(sig, sig.kind, level, seq_id=seq_id), msg=msg,
                       claim=True, enforce_cap=sig.level != "P0")

    def _push_log(self, row: dict, msg: Message, claim: bool = False,
                  enforce_cap: bool = False) -> None:
        """默认**不**受日额度约束。

        额度（`notify.daily_cap`）是给**行情信号**用的：防止「一只票一个异动推一条」
        把手机淹掉。运维告警（`alert`）、开盘简报、收盘摘要、确认结果都不是这东西——
        它们要么一天只有一两条，要么本来就不该被压。

        **这是 v0.2.69 引入的回归**：把额度判定从 `_route` 移进 `_push_rows` 事务时，
        默认值取成了 `True`，于是 `alert()` 这条根本没被 `_route` 管过的路径
        开始受额度限制——日额度打满后，P0 通道故障告警会被静默 `suppressed`。
        v0.2.68 的 `_push_rows` 里压根没有额度检查，这里恢复成那个语义。
        """
        self._push_rows([row], msg, claim=claim, enforce_cap=enforce_cap)

    def _push_rows(self, rows: list[dict], msg: Message, claim: bool = False,
                   enforce_cap: bool = True) -> bool:
        """一次写事务认领这批待发行 + 一次发布 + 同状态回写。

        认领键是 `seq_id`：api 与 jobs 各自持一个 `Dispatcher`、共用一个 SQLite 库，
        两边同时对同一个 signal 派发时，只有先提交的那一边能把行写成 `pending`；
        后到者看到 `seq_id` 已有 `pending/accepted` 行就放弃（返回 False），不再双发。
        """
        now = self.clock.now().isoformat()
        # **必须 BEGIN IMMEDIATE**：`with conn` 是延迟事务，SELECT 不开启写事务，
        # api 与 jobs 会同时查到「没有」，然后同时插入——认领等于没做（实测双发 2 条）。
        # IMMEDIATE 在语句开始时就拿写锁，把「查 + 插」变成真正的跨进程原子操作。
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # **额度判断也放进这个事务**：原来 `_route` 在事务外先查一次
            # `_capped()`，等真正写 `pending` 时额度可能已经被另一个服务
            # （agent 的通道 B）用掉——两边各自读到 59/60 然后都放行，实际发到 61。
            # `BEGIN IMMEDIATE` 已经拿到写锁，这里读到的就是最新已提交值。
            if enforce_cap and self._capped():
                # 超限：不写 `pending`（不占位），但**要留 suppressed 审计行**——
                # 事后要能回答「今天为什么没推这条」，没有这行就等于静默丢弃。
                self.conn.rollback()
                for row in rows:
                    self.conn.execute(
                        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body,"
                        " event_id, signal_id, trade_date, status)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,'suppressed')",
                        (now, row["code"], row["kind"], row["level"], row["seq_id"],
                         row["segment"], msg.body, row["event_id"], row["signal_id"],
                         row["trade_date"]))
                self.conn.commit()
                return False
            if claim:
                claimed = self.conn.execute(
                    "SELECT 1 FROM push_log WHERE seq_id = ? AND status IN ('pending','accepted')"
                    " LIMIT 1", (rows[0]["seq_id"],)).fetchone()
                if claimed is not None:
                    self.conn.rollback()
                    return False
            ids = []
            for row in rows:
                cur = self.conn.execute(
                    "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, event_id,"
                    " signal_id, trade_date, status) VALUES (?,?,?,?,?,?,?,?,?,?,'pending')",
                    (now, row["code"], row["kind"], row["level"], row["seq_id"], row["segment"],
                     msg.body, row["event_id"], row["signal_id"], row["trade_date"]),
                )
                ids.append(cur.lastrowid)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if drill_skip("dispatcher", kind=getattr(msg, "seq_id", "")):
            # 演练：**不发送、也不把行标成 accepted**（演练数据不能进真实推送统计）
            with self.conn:
                # 状态只能取 CHECK 里的四个值；演练记 `suppressed` + error 说明，
                # 既不算 accepted（不污染统计）也留得下痕迹
                self.conn.executemany(
                    "UPDATE push_log SET status = 'suppressed', error = 'drill' WHERE id = ?",
                    [(i,) for i in ids])
            return []
        try:
            ntfy_id = self.ntfy.publish(msg)
            status, error = "accepted", None
            self._fail_streak = 0
        except Exception as exc:                  # noqa: BLE001 —— 上游挂了也要记一行
            ntfy_id, status, error = None, "failed", str(exc)[:200]
            self._fail_streak += 1
        with self.conn:
            self.conn.executemany(
                "UPDATE push_log SET status = ?, ntfy_id = ?, error = ? WHERE id = ?",
                [(status, ntfy_id, error, i) for i in ids],
            )
        if self._fail_streak >= 3:
            self._channel_down()
        return True

    def _channel_down(self) -> None:
        """D20：推送通道连续失败 3 次 → 写一条本地失败留痕，api 侧据此报 `notify_ok=false`。"""
        now = self.clock.now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, trade_date, status, error)"
                " VALUES (?, NULL, 'push_channel_down', 'P2', ?, 1, ?, 'failed', ?)",
                (now.isoformat(), f"pcd-{int(now.timestamp())}", self.clock.today(),
                 f"连续 {self._fail_streak} 次发布失败"),
            )
        self._fail_streak = 0

    def _log(self, sig: SigRow, status: str) -> None:
        now = self.clock.now().isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, event_id,"
                " signal_id, trade_date, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now, sig.code6, sig.kind, sig.level,
                 _seq_id(sig), 1, "", sig.event_id,
                 sig.signal_id, sig.event_trade_date, status),
            )

    # ---- 非事件推送 ------------------------------------------------------

    def pool_alerts(self, report) -> None:
        """出池后把 `pool_low` / `pool_overflow` 推给用户。

        `jobs/after_close.py::_dispatch` 一直是 `getattr(dispatcher, "pool_alerts", None)`
        调这个方法——而本类**从来没有它**，于是每次出池都只记一条
        `WARNING ... 告警未推送`，池子告警一次都没到过手机。`getattr` 的兜底让出池
        本身不失败，缺陷因此静默了很久（当时 `cn_star` 只有十几只触发
        `pool_low` 才暴露）。

        `pool_low` 是 P1：池子过小意味着当天几乎没有符合形态的票，这不是故障，
        但用户需要知道「今天没什么可看的」而不是以为系统坏了。
        `pool_overflow` 是 P2：池子超上限被截断，属于正常波动。
        """
        labels = {
            "pool_low": ("池子过小", "P1"),
            "pool_overflow": ("池子超上限被截断", "P2"),
        }
        for alert in report.alerts or ():
            title, level = labels.get(alert, (alert, "P1"))
            self.alert(
                "pool",
                f"{report.market} {title}：候选 {report.candidates} 只、入池 {report.size} 只"
                f"（{alert}）",
                level,
            )

    #: 同一 `kind` 的告警多久内不重复发（秒）。`api/watchdog.py` 的 `REPEAT_S` 是同一量级。
    ALERT_REPEAT_S = 1800

    def alert(self, kind: str, text: str, level: str = "P1") -> None:
        """运维告警。**同 kind 30 分钟内只发一次**。

        起因：`agent/main.py` 的主循环 `except Exception` 里会调 `alert`，
        而主循环约 2 秒一轮——异常持续时（比如库锁死）每个周期都推一条 P1，
        几分钟就能把 `notify.daily_cap` 烧穿；额度一满，**真正的告警也被压掉**。
        watchdog 自己早就有 30 分钟冷却（`api/watchdog.py::_alert`），
        这条路径没有，属于同一个问题只修了一半。
        """
        # 维护模式（备份/回填/演练）期间不告警：那段时间的失败是预期的
        flag = self.conn.execute(
            "SELECT value FROM job_state WHERE name = 'maintenance.flag'").fetchone()
        if flag is not None and str(flag[0]) == "1":
            return
        now = self.clock.now()
        # 冷却判据用 **`status='accepted'` 或 `suppressed`**：发成功要冷却，
        # 被额度压掉的也算"已经尝试过"——否则额度满时反而每 2 秒重试一次。
        cutoff = (now - timedelta(seconds=self.ALERT_REPEAT_S)).isoformat()
        recent = self.conn.execute(
            "SELECT 1 FROM push_log WHERE kind = ? AND status IN ('accepted','suppressed')"
            " AND ts > ? LIMIT 1", (kind, cutoff)).fetchone()
        if recent is not None:
            return
        seq_id = f"alert-{kind}-{int(now.timestamp())}"
        n = self.conn.execute("SELECT count(*) FROM push_log WHERE seq_id LIKE ?",
                              (f"{seq_id}%",)).fetchone()[0]
        if n:
            seq_id = f"{seq_id}-{n}"
        self._push_log(row={"code": None, "kind": kind, "level": level, "seq_id": seq_id,
                            "event_id": None, "signal_id": None, "segment": 1,
                            "trade_date": self.clock.today()},
                       msg=render_alert(kind, text, self.cfg, level=level,
                                        secret=self.secret))

    def open_brief(self, rows: list[BriefRow], n: int, overflow=None, pool_date=None,
                   baseline_days=None, auction_detections=None) -> None:
        msg = render_open_brief(rows, n, self.secret, self.cfg, overflow=overflow,
                                pool_date=pool_date, baseline_days=baseline_days,
                                auction_detections=auction_detections)
        self._push_log(row={"code": None, "kind": "open_brief", "level": "P2",
                            "seq_id": msg.seq_id, "event_id": None, "signal_id": None,
                            "segment": 1, "trade_date": self.clock.today()}, msg=msg)

    def build_close_stats(self) -> CloseStats:
        """当日推送统计（**纯读，不发送**，便于单测与网页复用）。

        「已推送」必须与 `_capped()` 同一口径：只算**真正发出去的新通知**。
        原来只按 `status='accepted'` 数，于是 `confirm_result`（确认回调里
        直接以 accepted 插入、从未发送的审计行）也被算成了已推送——
        同一份语义两处口径不一致，摘要会虚高。
        """
        day = self.clock.today()
        # 按 seq_id 去重：合并批的成员行是同一「条」，按行数算等于把 1 条算成 N 条
        pushed = pushed_today(self.conn, exclude_segments=(2,),
                              exclude_kinds=NON_PUSH_KINDS, day=day)
        suppressed = self.conn.execute(
            "SELECT count(*) FROM push_log WHERE trade_date=? AND status='suppressed'", (day,)).fetchone()[0]
        by_kind = {r[0]: r[1] for r in self.conn.execute(
            "SELECT kind, count(*) FROM signals WHERE trade_date=? GROUP BY kind", (day,))}
        by_level = {r[0]: r[1] for r in self.conn.execute(
            "SELECT level, count(*) FROM signals WHERE trade_date=? GROUP BY level", (day,))}
        return CloseStats(pushed=pushed, suppressed=suppressed,
                          signals_n=sum(by_kind.values()),
                          by_kind=by_kind, by_level=by_level, pool_sizes=self._pool_sizes())

    def close_digest(self) -> None:
        day = self.clock.today()
        stats = self.build_close_stats()
        msg = render_close_digest(stats, self.cfg, trade_date=day,
                                      secret=self.secret)
        self._push_log(row={"code": None, "kind": "close_digest", "level": "P2",
                            "seq_id": msg.seq_id, "event_id": None, "signal_id": None,
                            "segment": 1, "trade_date": day}, msg=msg)

    # ---- 辅助 ------------------------------------------------------------

    def _pool_sizes(self) -> dict[str, int]:
        day = self.clock.today()
        # 日期过滤必须在 WHERE 里：写成 `GROUP BY market HAVING trade_date = ...` 时
        # HAVING 里的裸列取组内任意一行、而 COUNT 统计的是**该市场全部历史日期**——
        # 昨天 5 只 + 前日 200 只会被算成 205（或 0），上溢判定方向随机。
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT market, count(*) FROM pool"
            " WHERE trade_date = (SELECT max(trade_date) FROM pool WHERE trade_date < ?)"
            " GROUP BY market", (day,))}


def _seq_id(sig) -> str:
    """推送序列号：`sig-代码-事件日-kind-seq`，如 `sig-600519-20261029-breakout-1`。

    **用 `event_trade_date`（事件所属交易日），不是池子日**：池子是 T−1 数据，
    用它会把这儿的信号记到昨天，池子某天没重建时连续两天还会撞同一个号。
    `kind` 必须在里面，否则同一只票同一天首次突破与首次跌破都是 seq=1，
    会撞认领键被静默吞掉。格式必须与 `notify/templates.py::_seq_id` 完全一致。
    """
    return f"sig-{sig.code6}-{sig.event_trade_date}-{sig.kind}-{sig.seq}"


def load_sig(conn, event_id: int) -> SigRow | None:
    """按 `event_id` 拼出 `SigRow`：event_log + signals + pool(T−1) + meta + snapshot_cache。

    任一处缺（例如盘中快照过期）就返回 `None`——少推一条远好过让整条循环崩。
    """
    ev = conn.execute(
        "SELECT id, ts, trade_date, market, kind, target, payload, dedupe_key FROM event_log WHERE id = ?",
        (event_id,)).fetchone()
    if ev is None:
        return None
    code = ev["target"]
    payload = json.loads(ev["payload"] or "{}")
    event_date = int(ev["trade_date"])

    sig_row = conn.execute(
        "SELECT id, seq, level FROM signals WHERE event_id = ? ORDER BY id LIMIT 1",
        (event_id,)).fetchone()
    meta = conn.execute("SELECT name, industry FROM stock_meta WHERE code = ?", (code,)).fetchone()

    from core.clock import TradingCalendar

    try:
        pool_date = TradingCalendar(conn).prev(event_date)
    except (LookupError, ValueError):
        pool_date = None
    pool = conn.execute(
        "SELECT trade_date, box_high_raw, box_low_raw, box_days, factors FROM pool"
        " WHERE code = ? AND trade_date = ?", (code, pool_date)).fetchone() if pool_date else None
    if pool is None or meta is None:
        return None

    snap = _snapshot_row(conn, code)
    if snap is None:
        return None
    price = float(snap.get("price") or 0.0)
    prev_close = snap.get("prev_close")
    if not price or not prev_close:
        return None
    chg = (price / float(prev_close) - 1) * 100

    factors = json.loads(pool["factors"] or "{}")
    b4 = factors.get("b4", factors.get("B4"))

    up = None
    if snap.get("up"):
        up = float(snap["up"])
    elif ev["kind"] == "breakout":
        up = _limit_of(conn, code, event_date, float(prev_close), 0)
    elif ev["kind"] == "breakdown":
        # `up` 字段装的是「本方向那个限价」：breakout 取涨停、breakdown 取跌停
        up = _limit_of(conn, code, event_date, float(prev_close), 1)

    return SigRow(
        id=int(sig_row["id"]) if sig_row else 0,
        code6=code[2:] if len(code) > 6 else code,
        code=code,
        name=meta["name"],
        industry=meta["industry"],
        kind=ev["kind"],
        level=(sig_row["level"] if sig_row else "P2"),
        score=float(payload.get("score") or 0.0),
        price=price,
        chg=chg,
        box_high=float(pool["box_high_raw"]),
        box_low=float(pool["box_low_raw"]),
        box_vol_multiple=payload.get("box_vol_multiple"),
        days=int(pool["box_days"]),
        b4=b4,
        up=up,
        a_group_discounted=bool(payload.get("a_group_discounted", 0)),
        extended=bool(payload.get("extended")),
        panic=bool(payload.get("panic")),
        sealed=bool(payload.get("sealed")),
        trade_date=int(pool["trade_date"]),
        event_id=event_id,
        signal_id=int(sig_row["id"]) if sig_row else None,
        seq=int(sig_row["seq"]) if sig_row else _seq_of(ev),
        event_trade_date=event_date,
    )


def _limit_of(conn, code: str, trade_date: int, prev_close: float, which: int) -> float | None:
    from core.market import classify_market, limit_prices

    market = classify_market(code)
    if market is None:
        return None
    st = "normal"
    row = conn.execute(
        "SELECT st_status FROM stock_status_history WHERE code = ? AND start_date <= ?"
        " AND (end_date IS NULL OR end_date >= ?) ORDER BY start_date DESC LIMIT 1",
        (code, trade_date, trade_date)).fetchone()
    if row is not None:
        st = row[0]
    limits = limit_prices(round(prev_close, 2), market, st, trade_date)
    return None if limits is None else limits[which]


def _snapshot_row(conn, code: str) -> dict | None:
    """从 `snapshot_cache('pool')` 取该票现价。**不 import 任何 Task 24 的模块**。"""
    row = conn.execute("SELECT payload FROM snapshot_cache WHERE scope = 'pool'").fetchone()
    if row is None:
        return None
    try:
        rows = json.loads(row[0]).get("rows") or []
    except ValueError:
        return None
    for item in rows:
        if item.get("code") == code:
            return item
    return None


def _seq_of(ev) -> int:
    """`signals` 行缺失时退回 `dedupe_key` 的第 4 段（D3/D4：两者同源）。"""
    try:
        return int(str(ev["dedupe_key"]).rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 1
