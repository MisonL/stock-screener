"""双池日预算（§7.5.1）。

**两个池各算各的**：对话把额度烧爆不该让盯盘闭嘴——那是两件优先级完全不同的事。

口径钉死：
- `day` 是**自然日**（`YYYYMMDD`），不是交易日：周末/节假日对话同样花当天的钱。
- 字段名必须是 SDK 的 `prompt_tokens` / `completion_tokens`；写成 `total_tokens`
  会让日预算恒 0、永远不触发。
"""
from __future__ import annotations

import time

#: 记账故障标志置位期间，每隔这么久放行一次"真扣一次"的探测。
#: 太短会在锁竞争时反复撞 busy_timeout（5 秒），太长会让自愈延迟。
WRITE_FAILED_PROBE_S = 30.0

from core.clock import SystemClock as _SystemClock


class Budget:
    def __init__(self, conn, pool: str, limits, clock=None):
        if pool not in ("agent", "chat"):
            raise ValueError(f"未知的预算池 {pool!r}")
        try:
            self.limit_in, self.limit_out = int(limits[0]), int(limits[1])
        except (IndexError, TypeError) as exc:
            raise ValueError(f"预算上限必须是 (输入, 输出) 两维，收到 {limits!r}") from exc
        self.conn = conn
        self.pool = pool
        # **必须按上海时区的自然日**：容器 TZ=UTC 时 `datetime.now()` 会在北京时间
        # 00:00–08:00 把用量记到前一天，额度要到早上 8 点才重置
        self.clock = clock if clock is not None else _SystemClock()

    # ---- 读 --------------------------------------------------------------

    @property
    def day(self) -> int:
        return int(self.clock.now().strftime("%Y%m%d"))

    def _row(self):
        return self.conn.execute(
            "SELECT tokens_in, tokens_out FROM llm_budget WHERE day = ? AND pool = ?",
            (self.day, self.pool)).fetchone()

    def _used(self) -> tuple[int, int]:
        row = self._row()
        return (row[0], row[1]) if row else (0, 0)

    def remaining(self) -> tuple[int, int]:
        used_in, used_out = self._used()
        return max(0, self.limit_in - used_in), max(0, self.limit_out - used_out)

    def allow(self, est_in: int, est_out: int) -> bool:
        """按**未截零**的原始总量判断：已经超限时 `remaining` 是 0，
        用剩余量比较会让 `allow(0, 1)` 在超限后依然为真、额度形同虚设。"""
        used_in, used_out = self._used()
        return used_in + est_in <= self.limit_in and used_out + est_out <= self.limit_out

    # ---- 保守扣减（硬上限的唯一落点）-------------------------------------

    def deduct(self, est_in: int, est_out: int) -> bool:
        """**不可退还地**扣掉一次网络请求的保守额度，够了返回 True。

        这就是硬上限的全部实现：每个 attempt 发出前扣「输入估算 + max_tokens」，
        之后**没有任何退款路径**。预算表因此是「已扣保守值」，永远不会少记。

        为什么不要「预留 + 对账」：那套机制要求调用方在结束时把估计换成实际，
        于是产生了三类无法补干净的漏洞（第九轮实测）——部分 attempt 未知与已知
        相抵、在途取消按零 usage 退款、跨池共享故障标记被无关成功清除。
        保守扣减把「花超」的可能性从机制上排除，代价是提前耗尽额度。

        记账故障在这里 fail-closed，且**按池隔离**：agent 写失败不该锁死 chat。
        """
        est_in = max(0, int(est_in or 0))
        est_out = max(0, int(est_out or 0))
        # **故障标志不能"一置永置"**（v0.2.93 修的 S-a）。
        #
        # 原来的写法是 `if self._write_failed(): return False` —— 直接短路。
        # 而自动清除挂在 `_try_deduct()` 里，于是**生产路径永远执行不到清除**：
        # 一次 SQLite 写锁抖动置上标志，之后每次 `deduct()` 都在 syscall 前返回
        # False，标志再也无人清除。`job_state` 的键里没有日期，所以这不是
        # "当天失效"，是**永久**失效——只能手工 DELETE，而 `preflight.py`
        # 又会因这个键拦住下一次部署。
        #
        # 现在改成**定期放行探测**：连续失败时仍 fail-closed（不让调用方
        # 一次次去撞锁），但每隔 PROBE 秒真试一次，写恢复正常就自愈。
        if self._write_failed() and not self._probe_allowed():
            return False
        try:
            return self._try_deduct(est_in, est_out)
        except Exception:                                  # noqa: BLE001
            # **SQLite 写锁/写失败必须 fail-closed**：让 `OperationalError` 抛出去，
            # 调用方会把它当成 "LLM 调用异常"，于是一次**根本没发出去**的请求
            # 被记成一整轮（F-34 的「一轮收场」口径被污染），而且下次唤醒照样重试。
            # 返回 False 才是「预算不可用」的正确语义。
            #
            # **同时留下按池标志**：`_write_failed()` 的读方（`agent/loop.py` 的
            # 推送前检查、`agent/main.py` 的心跳 detail、preflight）都要能看到
            # 「这个池的记账坏了」。没有写入方时这些检查是死代码。
            self._mark_write_failed()
            return False

    def _probe_allowed(self) -> bool:
        """故障标志在置位期间，是否到了可以再试一次的时刻。

        **必须定期真试**：`_try_deduct()` 成功时会清标志，若这里永远返回 False，
        清除路径就成了死代码（这正是 v0.2.91 犯的错）。
        """
        now = time.monotonic()
        last = getattr(self, "_last_probe_at", None)
        if last is not None and now - last < WRITE_FAILED_PROBE_S:
            return False
        self._last_probe_at = now
        return True

    def _mark_write_failed(self) -> None:
        """置本池故障标志。**写不进去也不能抛**：调用方已经在失败路径上。"""
        import logging

        # **置位同时记下探测起点**：否则"刚失败"和"很久没试过"无法区分，
        # 下一次 `deduct()` 会立刻放行探测，等于没有 fail-closed。
        self._last_probe_at = time.monotonic()
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO job_state(name, value) VALUES (?, '1')",
                    (f"budget.write_failed:{self.pool}",))
        except Exception:                                  # noqa: BLE001
            logging.getLogger("budget").warning(
                "预算写失败标志也无法写入（池 %s）", self.pool, exc_info=True)

    def _try_deduct(self, est_in: int, est_out: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE llm_budget SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?"
                " WHERE day = ? AND pool = ? AND tokens_in + ? <= ? AND tokens_out + ? <= ?",
                (est_in, est_out, self.day, self.pool, est_in, self.limit_in,
                 est_out, self.limit_out))
            if cur.rowcount:
                # **成功扣减就清掉故障标志**（v0.2.90 修的 M5）。
                # 原来 `clear_write_failed()` 在生产里**零调用者**：一次写锁抖动置上
                # `budget.write_failed:agent` 之后，`agent/main.py` 会把它读成
                # `budget_exhausted=True`，于是当天剩下的时间**所有闹钟都被跳过**
                # （回访一条都不跑），而且 watchdog 的两条规则还会因同一标志静音。
                # 一个能自己恢复的瞬时故障不该有"永久"的效果。
                # 这里是最自然的清除点：扣减成功就证明记账恢复正常了。
                try:
                    self.conn.execute("DELETE FROM job_state WHERE name = ?",
                                      (f"budget.write_failed:{self.pool}",))
                except Exception:                          # noqa: BLE001
                    pass                                   # 清不掉不影响扣减结果
                return True
            if self._row() is not None:
                return False
            self.conn.execute(
                "INSERT OR IGNORE INTO llm_budget(day, pool, tokens_in, tokens_out)"
                " VALUES (?,?,0,0)", (self.day, self.pool))
        return self.deduct(est_in, est_out)

    def audit(self, usage, *, unknown: bool = False, est_in: int = 0,
              est_out: int = 0, attempt_seq: int = 1) -> None:
        """把**真实用量**写进审计表（`token_audit`），不动预算表。

        预算表记的是保守已扣值，二者不相等是设计而不是缺陷；差异只能在审计里
        看（`estimated=1` 表示 SDK 没给 usage，那一行没有真实值）。
        """
        real_in = real_out = None
        is_unknown = bool(unknown) or usage is None
        if not is_unknown:
            real_in = int(getattr(usage, "prompt_tokens", 0) or 0)
            real_out = int(getattr(usage, "completion_tokens", 0) or 0)
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO token_audit(ts, pool, real_in, real_out, estimated,"
                    " reserve_in, reserve_out, attempt_seq) VALUES (?,?,?,?,?,?,?,?)",
                    (self.clock.now().isoformat(), self.pool, real_in, real_out,
                     1 if is_unknown else 0, int(est_in or 0), int(est_out or 0),
                     int(attempt_seq or 1)))
        except Exception:                                  # noqa: BLE001
            # 审计写失败不影响预算安全（额度已经扣了），但必须留痕
            import logging

            logging.getLogger("budget").warning("token_audit 写入失败", exc_info=True)

    def _write_failed(self) -> bool:
        """本池上一次记账是否失败（**按池隔离**）。"""
        try:
            row = self.conn.execute(
                "SELECT value FROM job_state WHERE name = ?",
                (f"budget.write_failed:{self.pool}",)).fetchone()
        except Exception:                                  # noqa: BLE001
            return True                                    # 读不到就 fail-closed
        return bool(row and row[0])

    def clear_write_failed(self) -> None:
        """确认本池记账恢复正常后清除故障标志。"""
        with self.conn:
            self.conn.execute("DELETE FROM job_state WHERE name = ?",
                              (f"budget.write_failed:{self.pool}",))

    def charge(self, usage, *, est_in: int = 0, est_out: int = 0) -> None:
        """兼容入口：等价于「先按估计扣减，再审计真实用量」。

        新代码请直接用 `deduct()`（发请求前）+ `audit()`（收到结果后）。
        """
        self.deduct(est_in, est_out)
        self.audit(usage, unknown=usage is None, est_in=est_in, est_out=est_out)
