"""jobs 容器的主进程：BlockingScheduler 驻留 + 启动恢复（D10）+ 心跳（D9）+ 自检（D26）。

**必须是 BlockingScheduler**：`start()` 阻塞主线程直到进程被杀，这才是容器要的。
换成 `BackgroundScheduler` 后主线程一 `return` 进程就退出，16:00 清理、15:05 eod、
23:30 备份全都永远跑不到，而容器看起来是「正常运行」。

**除权/停牌刷新既有启动补跑、也有周期任务**：`startup_recovery` 按「距上次
≥7 天」补跑一次；`actions_refresh*` 几个 cron 在非交易时段每 20 分钟滚动推进
一小段（上游限频决定了一次拉不完全市场，见 `mcp/westock.py::batch_dividend`）。
能力缺失（客户端没有这两个方法）时回调为 `None`，两处都直接跳过并打 warning。

**PID 1 必须自己装 SIGTERM**：容器里 `python -m jobs.main` 是 1 号进程，没装 handler
时 SIGTERM 不执行默认终止，`docker stop` 会等满 10 秒再 SIGKILL，正在写的事务被硬杀。
"""
from __future__ import annotations

import json
import inspect
import logging
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from jobs.backup import backup_db
from jobs.cleanup import cleanup
from jobs.intraday import IntradayTick
from jobs.snapshot import SnapshotLoops, calls_by_scope
from core.clock import SH, SystemClock, TradingCalendar

HEARTBEAT_S = 30
def _null_lock():                                  # pragma: no cover - 无锁测试兜底
    import contextlib

    return contextlib.nullcontext()


JOB_DEFAULTS = dict(misfire_grace_time=300, coalesce=True, max_instances=1)
# 启动后多久补跑一次 westock 续期。给 60 秒是让启动恢复先跑完
# （盘中重启时要先起循环），同时不至于拖太久。
STARTUP_RENEW_DELAY_S = 60

log = logging.getLogger("jobs.main")


def _is_locked(exc: BaseException) -> bool:
    """是否 SQLite 的锁冲突（`database is locked` / `database table is locked`）。"""
    return "locked" in str(exc).lower()


def make_scheduler(cfg, conn, clock, *, loops=None, tick=None, dispatcher=None,
                   reg=None, data_dir="data", mcp=None, westock_runner=None,
                   westock_env_path=None):
    """装配调度器。返回 `(scheduler, state)`；`state` 里放心跳与自检需要的共享字段。

    `mcp` / `westock_runner` / `westock_env_path` 是批2 Task 6 的三个注入口子：
    前者喂 09:00 的自选股导入；后两者喂 08:40 的 token 续期（测试不真的起 node
    子进程、也不真的去写 `/state/westock.env`）。
    """
    sched = BlockingScheduler(timezone="Asia/Shanghai", job_defaults=JOB_DEFAULTS)
    # ISO 串而不是 epoch 秒：与 `last_beat_at` 同一口径，watchdog 不必猜单位；
    # 非交易时段置 None——否则隔夜一直是昨天 15:04 的值，次日 09:15 一开闸就误报「循环未推进」
    state = {"loop_last_tick_at": None, "loops": loops, "tick": tick, "self_check": None}
    # 心跳与自检各自的写互斥（同一个 30s 心跳行），而写库串行化交给 loops.lock（tick 共享）
    _heartbeat_lock = threading.RLock()

    def _write_lock(loops_obj):
        return loops_obj.lock if loops_obj is not None else _null_lock()
    # 交易日历按日缓存：tick 每秒一次，每次重建会读一遍 sh000300 全历史
    cal_cache: dict = {"day": None, "cal": None}
    cal_lock = threading.Lock()

    def is_trading_day(now):
        day = int(now.strftime("%Y%m%d"))
        if cal_cache["day"] == day and cal_cache["cal"] is not None:
            return cal_cache["cal"].is_trading_day(day)
        with cal_lock:                      # 先构造再发布，避免另一线程读到 cal=None
            if cal_cache["day"] != day or cal_cache["cal"] is None:
                cal_cache["cal"] = TradingCalendar(conn)
                cal_cache["day"] = day
            cal = cal_cache["cal"]
        return cal.is_trading_day(day)

    def tick_job():
        now = clock.now()
        if not _in_session(now) or not is_trading_day(now):
            state["loop_last_tick_at"] = None
            state["tick_last_at"] = None
            return
        tick.tick()
        state["tick_last_at"] = now.isoformat()
        # **loop 标记必须反映快照循环，不是 tick 自己**：tick 每秒都在空转，
        # 用它当标记会让「快照循环已经卡死」在心跳里显示成「一切正常」。
        epoch = getattr(loops, "last_tick_at", 0.0) if loops is not None else 0.0
        state["loop_last_tick_at"] = (
            datetime.fromtimestamp(epoch, tz=now.tzinfo).isoformat() if epoch else None)

    def heartbeat_job():
        # 心跳与自检写同一行 service_heartbeat：整体 REPLACE 会把对方写的 detail 盖掉，
        # 所以两处都在共享锁里做「读-改-写」
        with _write_lock(loops), _heartbeat_lock:
            detail = {"loop_last_tick_at": state["loop_last_tick_at"],
                      "tick_last_at": state.get("tick_last_at"),
                      "calls_by_scope": calls_by_scope(conn, clock.now())}
            if state.get("self_check") is not None:
                detail["self_check"] = state["self_check"]
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO service_heartbeat(service, last_beat_at, detail)"
                        " VALUES ('jobs',?,?)",
                        (clock.now().isoformat(), _json(detail)),
                    )
            except sqlite3.OperationalError as exc:
                # **锁冲突时安静跳过**：EOD 的事务会持锁数分钟，而 busy_timeout 只有 5 秒。
                # 抛给 apscheduler 等于每分钟刷一条 ERROR（曾经就是这样，日志被刷爆）。
                # 跳过是安全的：30 秒后还有下一次，判活阈值是 120 秒。
                if not _is_locked(exc):
                    raise
                log.warning("心跳写库撞锁，跳过本次（30 秒后重试）")

    def health_touch_job():
        """健康标记：**不碰数据库、不碰写锁**，独立成任务。

        原先 `Path("/tmp/jobs_healthy").touch()` 写在 `heartbeat_job` 里，于是
        EOD 持写锁约 4 分钟时它跟着被阻塞。compose 的健康检查是
        `find /tmp/jobs_healthy -mmin -2`、每 60 秒一次、连 3 次失败判 unhealthy——
        正好会被那 4 分钟打穿。标记只反映「进程还在」，不该受写锁影响。
        """
        Path("/tmp/jobs_healthy").touch()

    def eod_job():
        from jobs.after_close import run as after_close
        from jobs.eod import run_eod

        # **整条写路径都必须在同一把写锁里**：`after_close` 也写 `pool` 表。
        # 它原先跑在锁外（只锁了 run_eod），结果心跳写库撞上它、
        # busy_timeout（5 秒）耗尽后抛 `database is locked`。EOD 事务本身持锁约 3 分钟、
        # after_close 选三个市场约 46 秒，而判活阈值只有 STALE_S=120 秒——
        # 连续几个心跳周期失败就会让 watchdog 误判 jobs 已死（P0 误报）。
        # `loops.lock` 是 RLock，after_close 内部不会再取它，可安全包进来。
        with _write_lock(loops):               # 与 tick 共用连接，写库必须串行
            report = run_eod(cfg, reg, conn, clock)
            if not report.skipped:
                after_close(cfg, conn, clock, eod_report=report, dispatcher=dispatcher)
            # marker 也必须在锁内：它是**写** `job_state`，与上面两段共用同一把锁
            # （原先它写在 `with _write_lock` 之外，靠 `with conn` 单独提交）
            with conn:
                conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES (?, 'done')",
                             (f"eod.{clock.today()}",))

    def auction_job():
        """09:25:30：用 pool/market 快照缓存里的报价与 `auction_daily` 基线出简报。"""
        if tick is None or not is_trading_day(clock.now()):
            return
        quotes = _cached_quotes(conn, "market") or _cached_quotes(conn, "pool")
        if not quotes:
            return
        tick.capture_auction(quotes, _auction_baselines(conn, clock.today(), cfg), tick.focus_of())

    def close_digest_job():
        """15:10：当日推送/信号统计。与其它写库线程共用 `tick.lock`。"""
        if dispatcher is None or not is_trading_day(clock.now()):
            return
        # 统一走 `_write_lock`：`close_digest` 经 `_push_log` 写 `push_log`。
        # 原来分成 `with tick.lock:` / 裸调用两支，裸调用那支在无 tick 时会绕过锁；
        # `_write_lock(None)` 返回 nullcontext，所以一条写法覆盖两种情况。
        with _write_lock(loops):
            dispatcher.close_digest()

    def members_job():
        """08:50：刷行业成分股（非交易日不跑）。"""
        if reg is None or not is_trading_day(clock.now()):
            return
        from jobs.eod import fetch_sector_members, write_sector_members

        try:
            # **先抓完、再加锁写**：抓几十个板块要好几分钟，若连网络请求一起放在
            # `loops.lock` 里，每秒的 tick 和 30 秒的心跳会全程堵着——
            # 曾经因为等锁超过 30 秒，心跳被 APScheduler 直接跳过。
            sectors = fetch_sector_members(reg.sina)
            with _write_lock(loops):
                write_sector_members(conn, sectors)
                with conn:
                    conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES (?, 'done')",
                                 (f"sector_members.{clock.today()}",))
        except Exception:                          # noqa: BLE001 —— 一次刷新失败不能拖死调度器
            if dispatcher is not None:
                # `alert` 经 `_push_log` 写 `push_log`，必须在写锁内
                with _write_lock(loops):
                    dispatcher.alert("members", "成分股刷新失败", "P1")
            return

    def archive_job():
        from jobs.archive import run as archive_run

        try:
            # **必须与 tick 共用 loops.lock**：归档是「插归档 → 置空引用 → 删原表」
            # 三步，而本进程里每 30 秒的心跳 / 每秒的 tick 共用一个 conn；
            # 线程交错时对方的 `with conn` 会把归档的中间状态提交掉，
            # 三步就失去原子性（可能出现「归档表有、原表也还在」或「删了没归档」）。
            with _write_lock(loops):
                out = archive_run(conn, clock)
                # **leftover 非零必须留痕**：那意味着有行的 id 与归档表里已有行冲突，
            # 既没搬走也没删除。静默留着会一天天堆，最后没人知道为什么原表越来越大。
            if out.get("leftover"):
                logging.getLogger("jobs").warning(
                    "agent_turn 归档遗留 %s 行（id 冲突，已保留未删）", out["leftover"])
        except Exception as exc:                              # noqa: BLE001
            if dispatcher is not None:
                with _write_lock(loops):
                    dispatcher.alert("archive", f"agent_turn 归档失败：{exc}", "P1")
            return
        # 归档量不写日志也行：表里的行数本身就是证据，多一行日志反而要防重复
        _ = out

    def cleanup_job():
        # **必须与其它写库任务共用 loops.lock**：cleanup 写 6 张表
        # （push_log / event_log / pool / chat_message / chat_session / agent_schedule /
        # upstream_calls），还跑 `PRAGMA optimize`（读全库统计）。
        # 本进程的 tick（每秒）、心跳（30 秒）、after_close 选池共用**同一个** conn，
        # 线程交错时对方的 `with conn` 会把 cleanup 的中间状态提前提交，
        # 多条 DELETE 就失去原子性。本文件里其它写库任务（eod / members / archive /
        # self_check）都已经在锁里了，只有这里漏了。
        with _write_lock(loops):
            cleanup(conn, clock, dispatcher=dispatcher)

    def backup_job():
        try:
            path = backup_db(conn, Path(data_dir), clock=clock)
        except Exception:                          # noqa: BLE001 —— 备份失败不能让调度器死掉
            path = None
        if path is None and dispatcher is not None:
            # **只包告警，不包 `backup_db`**：备份要拷 1GB+，占着写锁会把 tick 饿死；
            # 而 `backup_db` 用的是 `conn.backup(dest)`（读源库、写目标文件），
            # 不写源库，本来就不需要写锁。
            with _write_lock(loops):
                dispatcher.alert("backup", "备份失败或完整性校验未通过", "P0")

    def self_check_job():
        result = self_check(cfg, conn, clock, reg=reg)
        state["self_check"] = result        # 心跳下一次会把 self_check 一起写进去，不会互相覆盖
        with _write_lock(loops), _heartbeat_lock:
            row = conn.execute(
                "SELECT detail FROM service_heartbeat WHERE service='jobs'").fetchone()
            detail = {**_json_load(row[0] if row else None), "self_check": result}
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO service_heartbeat(service, last_beat_at, detail)"
                    " VALUES ('jobs',?,?)",
                    (clock.now().isoformat(), _json(detail)),
                )
        if not all(result.values()) and dispatcher is not None:
            failed = ",".join(k for k, v in result.items() if not v)
            with _write_lock(loops):
                dispatcher.alert("self_check", f"自检失败：{failed}", "P1")

    # ---- 批2 Task 6：westock token 续期与自选股导入 ----------------------

    def westock_renew_job():
        """08:40：剩余 < 24h 就静默续期；「每天至多一次」的落点在 app_secret。

        失败后 08:46 还有一次（`westock_renew_retry` 任务），再失败才 P1 让人扫码。
        """
        from jobs.westock_renew import renew_if_needed

        runner = westock_runner
        if runner is None:
            import subprocess

            runner = subprocess.run
        kwargs = {} if westock_env_path is None else {"env_path": westock_env_path}
        # 写 `app_secret`（每天至多一次的落点 + 到期时间）：同进程每秒的 tick
        # 共用同一个 conn，不串行就会交错。启动补跑是 DateTrigger(+60s)，
        # 可能落在盘中任何时刻，所以不能只按「08:40 不在盘中」推断安全。
        #
        # **锁只包住写库那几句，不包脚本**：`_run` 会等扫码，最长 600 秒。
        # 原来整段都在锁里，续期期间每秒的 tick、心跳、快照缓存全部停摆，
        # watchdog 会判死，而独立健康文件照常刷新——Docker 绿着，业务已经停了。
        return renew_if_needed(conn, clock, runner, dispatcher=dispatcher,
                               write_lock=_write_lock(loops), **kwargs)

    def actions_refresh_job():
        """滚动推进除权/停牌刷新（每轮一小段，见 `make_refresh_hooks`）。"""
        # **每轮留一行文件痕迹**：容器 stdout 在部分宿主机上不落 docker logs
        # （实测连 `logger.info` 都进不去），只靠日志排障会瞎。
        # 文件小、每轮一行、自动截断，排查"这次到底跑没跑"时直接 cat。
        _trace(f"actions_refresh 开始 (mcp={type(mcp).__name__ if mcp is not None else 'None'})")
        refine_actions, refine_susp = make_refresh_hooks(mcp, cfg, conn)
        if refine_actions is None:
            _trace("actions_refresh 跳过：westock 客户端不具备取数能力")
            return
        try:
            # 取数在锁外、写库在锁内，见 `_actions` 的说明
            n = refine_actions(_write_lock(loops))
            # 停牌很便宜（1 个请求），每轮顺手带上；**写库同样要持锁**
            # （它写 suspension_calendar，与 tick 共享同一个 conn）
            if refine_susp is not None:
                refine_susp(_write_lock(loops))
        except Exception as exc:                                  # noqa: BLE001
            _trace(f"actions_refresh 异常：{type(exc).__name__}: {exc}")
            log.warning("除权/停牌滚动刷新异常：%s: %s", type(exc).__name__, exc)
            return
        _trace(f"actions_refresh 结束：写入 {n} 行")
        if n is not None:
            log.info("除权滚动刷新：本段写入 %s 行", n)

    def westock_import_job():
        """09:00：手机自选股单向导入 `watch_stock(source='westock')`（只增不改）。"""
        if mcp is None:
            return 0
        from mcp.westock import import_watchlist

        # 写 `watch_stock`：与 tick 共用同一个 conn，必须串行
        with _write_lock(loops):
            return import_watchlist(conn, mcp)

    sched.add_job(tick_job, "interval", seconds=1, id="tick", **JOB_DEFAULTS)
    sched.add_job(heartbeat_job, "interval", seconds=HEARTBEAT_S, id="heartbeat", **JOB_DEFAULTS)
    # 健康标记独立调度：与心跳同频，但**不取写锁**（见 health_touch_job 的说明）
    sched.add_job(health_touch_job, "interval", seconds=HEARTBEAT_S, id="health_touch",
                  **JOB_DEFAULTS)
    # **每个 CronTrigger 都要显式 timezone**：容器是 UTC，不传时 APScheduler 按本地时区
    # 解释 hour/minute，15:05 的 eod 会跑到北京时间 23:05。
    sched.add_job(eod_job, CronTrigger(hour=15, minute=5, day_of_week="mon-fri", timezone=SH),
                  id="eod", **JOB_DEFAULTS)
    sched.add_job(cleanup_job, CronTrigger(hour=16, minute=0, day_of_week="mon-fri", timezone=SH),
                  id="cleanup", **JOB_DEFAULTS)
    # 15:30 归档 `agent_turn`（90 天前移到 archive 表，不删）。**不注册就永远不跑**：
    # 批1 的 cleanup 不管这张表，回合表会无限增长
    sched.add_job(archive_job, CronTrigger(hour=15, minute=30, day_of_week="mon-fri",
                                           timezone=SH), id="archive", **JOB_DEFAULTS)
    sched.add_job(backup_job, CronTrigger(hour=23, minute=30, timezone=SH), id="backup",
                  misfire_grace_time=3600, coalesce=True, max_instances=1)
    sched.add_job(self_check_job,
                  CronTrigger(hour=8, minute=55, day_of_week="mon-fri", timezone=SH),
                  id="self_check", **JOB_DEFAULTS)
    sched.add_job(members_job,
                  CronTrigger(hour=8, minute=50, day_of_week="mon-fri", timezone=SH),
                  id="members", **JOB_DEFAULTS)
    sched.add_job(auction_job,
                  CronTrigger(hour=9, minute=25, second=30, day_of_week="mon-fri", timezone=SH),
                  id="auction", **JOB_DEFAULTS)
    sched.add_job(close_digest_job,
                  CronTrigger(hour=15, minute=10, day_of_week="mon-fri", timezone=SH),
                  id="close_digest", **JOB_DEFAULTS)
    # 08:40 续期 + 08:46 唯一一次重试（间隔 ≥5 分钟，与 renew_if_needed 的退避一致）；
    # 09:00 自选股导入（09:15 开盘前完成，不占用盘中资源）
    #
    # **续期必须每天跑，不能只挂 mon-fri**：token 到期时刻由腾讯决定，不认交易日历。
    # 续期到期日可能正好落在周末：只挂工作日 cron 的话周六周日都不跑，
    # 直到周一 08:40 才动——那时已过期 18.8 小时，期间任何 westock 调用都失败
    # （除权事件、停牌日历）。续期本身很便宜（剩 ≥24h 直接 skip），
    # 多跑周末两天没有成本。
    sched.add_job(westock_renew_job,
                  CronTrigger(hour=8, minute=40, timezone=SH),
                  id="westock_renew", **JOB_DEFAULTS)
    sched.add_job(westock_renew_job,
                  CronTrigger(hour=8, minute=46, timezone=SH),
                  id="westock_renew_retry", **JOB_DEFAULTS)
    # **启动补跑**：`misfire_grace_time` 只有 5 分钟，08:40 那一刻进程不在
    # （部署、重启、崩溃）这次就整个丢了。补跑是幂等的——
    # `renew_if_needed` 剩 ≥24h 直接 skip、当天已成功也 skip，
    # 所以每次启动都调一次不会重复打腾讯。
    # 用 DateTrigger（跑一次就没了），若改成 interval 等于把频率调成每分钟。
    #
    # **`run_date` 用占位值，真正的时刻在 `sched.start()` 之前由
    # `arm_startup_renew()` 重算**。原因：本函数在 `startup_recovery()` **之前**执行，
    # 而后者会补跑最多 5 天 EOD（每只票都发请求），耗时可达数十分钟。若在这里把
    # `run_date` 定成 `now+60s`，等到 `sched.start()` 时它早已过期，
    # 超过 `misfire_grace_time`(300s) 就被 APScheduler **静默丢弃**——
    # 启动补跑一次都不跑，而且不报错。那正是这道保险要兜的场景。
    sched.add_job(westock_renew_job, "date",
                  run_date=clock.now() + timedelta(seconds=STARTUP_RENEW_DELAY_S),
                  id="westock_renew_startup", **JOB_DEFAULTS)

    def arm_startup_renew() -> None:
        """在调度器启动前重算补跑时刻（`startup_recovery` 之后调用）。"""
        sched.modify_job("westock_renew_startup",
                         next_run_time=clock.now() + timedelta(seconds=STARTUP_RENEW_DELAY_S))

    sched.add_job(westock_import_job,
                  CronTrigger(hour=9, minute=0, day_of_week="mon-fri", timezone=SH),
                  id="westock_import", **JOB_DEFAULTS)
    # **除权/停牌滚动刷新**：上游限频决定了一次拉不完全市场（实测连发 8–10 个
    # 请求就限频、单次返回也残缺），所以每 20 分钟推进一小段（游标见
    # `jobs/eod.py::actions_cursor`），几天滚完一遍全市场。
    #
    # 避开盘中：15:05 的 eod 与 09:15–15:10 的
    # tick 才是主链路。除权数据不是实时数据，晚几小时无所谓，所以只在
    # 非交易时段（开盘前 + 收盘后 + 全天周末）推进，把白天的限频额度留给行情。
    # **盘中禁区 09:15–15:10**：`minute="*/20"` 会把 15:00 也算进去（落在禁区内
    # 且紧贴 15:05 的 eod），所以小时段只留 6–8 点与 16–23 点。
    #
    # **间隔从 20 分钟压到 2 分钟**（v0.2.70）：实测单轮只花 1.2 秒（4 批 × 10 只），
    # 而 20 分钟一轮意味着一天只有 31 轮、全市场 5583 只要跑 4.5 天。
    # 逐级加压实测（同一轮里连发不同批数）：
    #   批数 4 → 1.2 秒收到 24 只；批数 6/8/10 → 触发限频、退避 90 秒后只收到 13/46/30 只
    # 也就是说**加大每轮批数反而慢 40 倍**，真正的瓶颈是"多久跑一次"。
    # 2 分钟一轮 = 一天约 310 轮，全市场约 7 小时跑完一圈（单轮 1.2 秒，占用极低）。
    # 开盘前（09:00、09:10）与收盘后（15:20、15:40）用独立的分钟级触发补上。
    sched.add_job(actions_refresh_job,
                  CronTrigger(hour="6-8,16-23", minute="*/2", day_of_week="mon-fri",
                              timezone=SH),
                  id="actions_refresh", **JOB_DEFAULTS)
    sched.add_job(actions_refresh_job,
                  CronTrigger(hour=9, minute="0,10", day_of_week="mon-fri", timezone=SH),
                  id="actions_refresh_premarket", **JOB_DEFAULTS)
    sched.add_job(actions_refresh_job,
                  CronTrigger(hour=15, minute="20,40", day_of_week="mon-fri", timezone=SH),
                  id="actions_refresh_postclose", **JOB_DEFAULTS)
    # 周末同样避开 09:20–15:00（虽然没行情，但保持同一套口径、也少打扰用户）
    sched.add_job(actions_refresh_job,
                  CronTrigger(hour="6-8,16-23", minute="*/2", day_of_week="sat,sun",
                              timezone=SH),
                  id="actions_refresh_weekend", **JOB_DEFAULTS)
    sched.add_job(actions_refresh_job,
                  CronTrigger(hour="9", minute="0,10", day_of_week="sat,sun", timezone=SH),
                  id="actions_refresh_weekend_am", **JOB_DEFAULTS)
    sched.add_job(actions_refresh_job,
                  CronTrigger(hour="15", minute="20,40", day_of_week="sat,sun", timezone=SH),
                  id="actions_refresh_weekend_pm", **JOB_DEFAULTS)
    state["arm_startup_renew"] = arm_startup_renew
    return sched, state


def self_check(cfg, conn, clock, reg=None, http=None) -> dict[str, bool]:
    """08:55 自检四项：上游快照 / ntfy / 池子 / LLM。键固定，心跳与告警共用这一份。"""
    out = {"upstream": False, "ntfy": False, "pool": False, "llm": False}
    try:
        if reg is not None:
            quotes, _ = reg.snapshot(["sh000001"])
            out["upstream"] = bool(quotes)
    except Exception:                              # noqa: BLE001
        out["upstream"] = False
    try:
        import httpx

        resp = httpx.get(f"{cfg.notify.ntfy_url}/v1/health", timeout=10, trust_env=False)
        out["ntfy"] = resp.status_code == 200 and (resp.json() or {}).get("healthy") is True
    except Exception:                              # noqa: BLE001
        out["ntfy"] = False
    try:
        cal = TradingCalendar(conn)
        prev = cal.prev(clock.today())
        out["pool"] = conn.execute(
            "SELECT 1 FROM pool WHERE trade_date = ? LIMIT 1", (prev,)).fetchone() is not None
    except Exception:                              # noqa: BLE001
        out["pool"] = False
    try:
        out["llm"] = _llm_probe(cfg)
    except Exception:                              # noqa: BLE001
        out["llm"] = False
    return out


def _llm_probe(cfg) -> bool:                       # pragma: no cover - 网络，单独单测
    """发一次最小补全，判断 LLM 链路是否可用。

    三条口径（每条都对应一次真实误判）：

    1. **url 从 yaml 或环境变量取**：`config/llm.env` 这个文件从来就不存在
       （密钥在 `~/.config/stock-screener/llm.env`，由 compose `env_file` 注入成
       环境变量）。旧实现读那个路径永远读不到 → `llm` 恒 False → 每天 08:55
       误报 P1，把真故障淹掉。
    2. **必须带密钥**：不带 Authorization 时服务端返 401。
    3. **401/403 是故障，不是健康**：旧判据 `status_code < 500` 把鉴权失败当正常，
       密钥过期时自检照样报绿——最该告警的情况恰好不告警。
    """
    import os

    import httpx

    # **环境变量优先**，与 `llm/client.py` 的覆盖顺序一致：
    # 演练要用 `LLM_BASE_URL=http://blackhole:1` 把链路指走，
    # 探测若反着取（yaml 优先）就永远探不到真实配置，演练也失效。
    url = (os.environ.get("LLM_BASE_URL")
           or getattr(getattr(cfg, "llm", None), "base_url", None) or "").strip()
    if not url:
        return False
    key_env = str(getattr(getattr(cfg, "llm", None), "api_key_env", "") or "LLM_API_KEY")
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get(key_env, "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    # **必须用配置里的真实模型名**：写死 "probe" 时，凡是"正常调用可用、
    # 但不存在叫 probe 的模型"的端点（很常见）都会被自检判成故障，
    # 08:55 那次自检就会推一条假告警。
    model = (os.environ.get("LLM_MODEL")
             or getattr(getattr(cfg, "llm", None), "model", None) or "").strip()
    if not model:
        return False
    resp = httpx.post(f"{url.rstrip('/')}/chat/completions", timeout=10, trust_env=False,
                      headers=headers,
                      json={"model": model, "messages": [{"role": "user", "content": "ping"}],
                            "max_tokens": 1})
    # 4xx 一律不算健康：401/403 是密钥问题，404/400 是路径或参数问题，
    # 这些正是自检该报出来的；只有 2xx/3xx 才说明链路通。
    return resp.status_code < 400


def _install_stack_dump() -> None:                  # pragma: no cover - 排障用
    """收到 `SIGUSR1` 时把所有线程的调用栈打到 stderr。

    容器里没有 gdb / py-spy，进程卡住时只能靠这个。日志管道不通时，
    `/proc/<pid>/fd/2` 仍能直接读到这段输出。
    """
    import faulthandler
    import signal

    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        log.info("已注册 SIGUSR1 栈转储（kill -USR1 <pid>）")
    except (AttributeError, ValueError):
        pass


def _trace_path() -> Path:
    """trace 文件路径（测试可以 monkeypatch 掉）。"""
    return Path("/tmp/actions_refresh.trace")


#: `_trace()` 的互斥锁。滚动任务和启动恢复会并发调用它，而"读-改-写"
#: 三步不是原子的：不加锁会互相覆盖，丢行。
_TRACE_LOCK = threading.Lock()


def _trace(msg: str) -> None:                       # pragma: no cover - 排障用
    """往 `/tmp/actions_refresh.trace` 追加一行（保留最近 200 行）。

    容器 stdout 在部分宿主机上不落 `docker logs`（实测 `logger.info` 也进不去），
    定时任务"到底跑没跑"就只能靠这条文件痕迹，否则排障只能猜。
    """
    from datetime import datetime

    path = _trace_path()
    try:
        with _TRACE_LOCK:
            lines = (path.read_text(encoding="utf-8").splitlines()[-200:]
                     if path.exists() else [])
            lines.append(f"{datetime.now(SH):%Y-%m-%d %H:%M:%S} {msg}")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _own_connection(conn):
    """给启动恢复的 EOD 补跑开一份**独立连接**。

    共享连接是 `check_same_thread=False`，多个线程可以在它上面调用，
    但它们**共享同一个事务状态**：一个线程 `with conn` 提交时，
    会把另一个线程还没写完的事务一起提交掉（实测能留下半条 EOD 数据）。
    EOD 补跑是一个跨表长事务，必须与快照线程隔离。
    """
    import sqlite3

    row = conn.execute("PRAGMA database_list").fetchone()
    path = row[2] if row and row[2] else None
    if not path:
        return conn                          # 内存库（测试）：无法隔离，退回共享连接
    own = sqlite3.connect(path, check_same_thread=False)
    own.row_factory = sqlite3.Row
    own.execute("PRAGMA journal_mode = WAL")
    # **要比共享连接的 5 秒长得多**：EOD 一个事务要写几千行、跑 1～3 分钟，
    # 期间快照线程会在另一条连接上要写锁。5 秒一过就 `database is locked`，
    # 而 EOD 一旦中途失败会整轮回滚、重跑一遍又是同样结果。
    # 给足 60 秒，让 SQLite 自己排队等，而不是让 EOD 输掉这场竞争。
    own.execute("PRAGMA busy_timeout = 60000")
    own.execute("PRAGMA foreign_keys = ON")
    return own


def _call_with_lock(fn, lock):
    """回调声明了参数就传 `lock`，否则原样调用。

    除权/停牌回调需要一把共享锁才能安全写库；但零参回调（早期测试桩）也不该因此
    报错。用 `inspect` 判一次，不做 try/except TypeError 那种会吞掉内部错误的写法。
    """
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return fn()
    # 只有存在"能接收一个位置参数"的形参时才传 lock。
    # 光看"参数列表非空"会把 `*args` 形式的包装回调也算进去，
    # 于是 lock 被塞进它的 args 里——那个回调拿到的是一把锁而不是自己想要的参数。
    accepts_positional = any(
        p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params)
    return fn(lock) if accepts_positional else fn()


def startup_recovery(cfg, conn, clock, *, reg, tick=None, dispatcher=None, loops=None,
                     refresh_actions=None, refresh_suspensions=None, run_eod=None) -> dict:
    """D10 启动恢复：盘中立即起循环；跨日补跑 eod；除权刷新按「距上次 ≥7 天」补。"""
    out = {"loops_started": False, "eod_days": [], "eod_ran": [], "eod_skipped": [],
           "eod_failed": [], "refresh_actions": False}
    now = clock.now()
    today = clock.today()

    # **无条件起循环**：只在「启动那一刻正在盘中」才起的话，09:14 前重启的容器
    # 当天就再也不会抓快照（容器重启、VM 重启都落在这个坑里）。
    # 请求频率与时段由循环内部的 `_in_session` / 交易日闸控制。
    if tick is not None:
        tick.detector.seed_today()
    if loops is not None:
        loops.start()
        out["loops_started"] = True
    # **起了循环之后就有并发写者了**：`loops.start()` 立刻拉起快照线程，
    # 它们经 `_write_cache` 写共享连接。所以下面凡是要写**共享连接**的地方
    # （marker、除权/停牌回调的写库）都走 `write_lock`。
    # 例外是 EOD 补跑：它跑在自己的连接上（见 `_own_connection`），
    # 事务与共享连接天然隔离，因此不持这把锁、也不会被快照线程打断。
    # （无 loops 时是 nullcontext，单测里照常可跑。）
    import contextlib

    write_lock = loops.lock if loops is not None else contextlib.nullcontext()

    # **节假日表过期要主动喊**：`builtin_holidays` 是硬编码的（只覆盖到 2027-10-07），
    # 过期后每个工作日都会被当成交易日。这本身不致命——EOD 会被上游快照的日期闸
    # 挡成 `skipped`，而 `skipped` 立即 `break`、不会反复刷请求——但**没人会注意到
    # 表已经过期**，直到某天在节假日里看到莫名其妙的重试。
    from core.clock import builtin_holidays

    if builtin_holidays and _ymd(today) > max(builtin_holidays):
        log.warning("节假日表已过期：末条 %d，今日 %d。之后的节假日会被当成交易日，"
                    "请更新 core/clock.py 的 builtin_holidays",
                    max(builtin_holidays), _ymd(today))

    # 跨日缺口：从「最后一次成功的 eod」之后逐日补跑（最多 5 天，避免一次开机跑一整天）
    cal = TradingCalendar(conn)
    done = _last_eod_date(conn)
    closed = [d for d in sorted(cal.days) if d < _ymd(today) and (done is None or d > done)]

    # **当天也要补**：cron 的 misfire_grace_time 只有 5 分钟，进程只要恰好在
    # 15:05–15:10 之间不在（部署、重启、崩溃），当天 EOD 就永远不跑——
    # 而候选原本只有 `d < today`，重启也救不回来。缺了当天的 bar，
    # 次日 EOD 的 `prev_day` 会直接跨过这一天。
    #
    # 但收盘前不能补：那时 bar 还没定盘，写进去就是错的。
    # 判据用与 `eod_job` 同一个 `close_time + CLOSE_BUFFER_MIN`，不另立一套字面量。
    # **不能用 `today_ymd in cal.days`**：交易日历来自 `sh000300` 的 bar，
    # 而今天那根 bar 正是 EOD 写进去的。EOD 从没跑过时日历必然不含今天，
    # 于是「今天该补跑」这个条件恰好在最需要它的时候不成立（鸡生蛋）。
    # `is_trading_day` 对超出日历最大日期的日期按工作日 + 内置节假日判定。
    today_ymd = _ymd(today)
    if cal.is_trading_day(today_ymd):
        close_time = cal.close_time(today_ymd) or str(cfg.markets.session.close_time)
        from jobs.eod import _minutes, CLOSE_BUFFER_MIN, EodReport  # noqa: F401

        if _minutes(clock.now().time()) >= _minutes(close_time) + CLOSE_BUFFER_MIN:
            marker = conn.execute("SELECT 1 FROM job_state WHERE name = ?",
                                  (f"eod.{today_ymd}",)).fetchone()
            if marker is None:
                closed.append(today_ymd)

    out["eod_days"] = closed[-5:]
    runner = run_eod
    if runner is None:
        from jobs.eod import run_eod as _run_eod

        runner = _run_eod
    # **EOD 补跑用独立连接，不持写锁**。
    #
    # 两个约束同时成立：
    # ① 不能持锁——`run_eod` 会取全市场快照、还可能回填缺口，一个交易日
    #    十几秒到几十秒，持锁期间每秒的 tick 全被堵死。
    # ② 不能用**共享连接**——`check_same_thread=False` 只允许跨线程调用，
    #    WAL 也不会为同一连接的不同线程开独立事务。`run_eod` 有一个跨表长事务，
    #    期间若快照线程在同一连接上 `with conn` 提交，会把 EOD 的**半成品事务
    #    一起提交掉**（实测：第一根 bar 残留、第二根回滚、adj 表为空）。
    #
    # 独立连接让两边的事务彻底隔离；WAL 下双方照常并发，写冲突由
    # `busy_timeout` 和 SQLite 自身的写锁串行化。
    eod_conn = _own_connection(conn)
    for day in out["eod_days"]:
        try:
            # 用钉在该日的时钟：run_eod 只认 clock.today()，快照日期闸保证不会写错日
            report = runner(cfg, reg, eod_conn, _DayClock(clock, day))
        except Exception as exc:               # noqa: BLE001 —— 补跑失败停在这里并留痕
            out["eod_failed"].append({"date": day, "error": repr(exc)})
            break
        if getattr(report, "skipped", False):
            # **跳过就停**：`skipped` 的成因是「快照日期不是该日」（节假日/上游还停在
            # 上个交易日），后续日期必然同样跳过。继续 `continue` 会把 5 天各跑一遍，
            # 每次重启都会白发一批重复的快照请求（`job_state` 为空时必然如此）。
            # 不写 marker 是对的（那天确实没跑成），但没必要把后面几天也试一遍。
            out["eod_skipped"].append(day)
            break
        # marker 走共享连接，由共享写锁保护
        with write_lock, conn:
            conn.execute("INSERT OR REPLACE INTO job_state(name, value) VALUES (?, 'done')",
                         (f"eod.{day}",))
        out["eod_ran"].append(day)

    # refresh_actions：键不存在或距上次 ≥7 天就补跑（周日不是交易日，不能用 contains 判）
    row = conn.execute("SELECT value FROM job_state WHERE name='refresh_actions.last'").fetchone()
    last_refresh = int(row[0]) if row and str(row[0]).isdigit() else None
    # **必须按日期差**：YYYYMMDD 当整数减，20261101-20261031 = 70，跨月会被误判成「满 7 天」
    due = last_refresh is None or _days_between(_ymd(today), last_refresh) >= 7
    if due and refresh_actions is not None:
        # **这里不能先 `with write_lock` 再调回调**：回调遵循 `f(lock=None)` 契约，
        # 会在**写库那一步**自己取这把锁；而生产锁是 `threading.Lock()`（不可重入），
        # 同线程二次取得就死锁——`sched.start()` 在恢复之后，主线程一卡，
        # 调度器永远起不来，快照线程也跟着等锁。
        #
        # 正确做法：把锁**传进**去，由回调自己在写库时持有（取数仍在锁外）。
        # **`None` = 取数失败，`0` = 查到了但确实没有**。
        # 两者必须分开：停牌返回 0 行是正常的（今天没人停牌），
        # 而"取数失败"如果被当成成功，就会按「距上次 ≥7 天」把重试推到下周
        # ——0.2.33 上线时正是这样，`corporate_actions` 官方行一直是 0。
        # **先停牌后分红**：停牌只要 1 个请求，分红要 100 多个。
        # 反过来的话，等分红那一百多个请求打完，停牌请求正好撞上上游
        # 「服务限频」返回 isError，于是整次刷新被判失败、时间戳不盖、
        # 下次启动从头再来一遍——实测就是这么卡住的。
        # **停牌失败不连坐除权**：停牌只有 1 个请求，撞上个限频很容易；
        # 若因此判定整次刷新失败、不盖时间戳，分红那边已经写进去的数据
        # 每次启动都要重拉一遍，反而持续消耗配额、持续撞限频。
        # 所以：除权失败才不盖戳；停牌单独记失败，下次启动再试。
        susp = (_call_with_lock(refresh_suspensions, write_lock)
                if refresh_suspensions is not None else None)
        wrote = _call_with_lock(refresh_actions, write_lock)
        failed = wrote is None
        if refresh_suspensions is not None and susp is None:
            log.warning("停牌刷新取数失败（不连坐除权；除权本次写入 %s 行）", wrote)
        # **一条都没写就不要盖时间戳**：盖了会按「距上次 ≥7 天」把重试推到下周，
        # 而失败（取数空、账号没配、上游改结构）通常不会抛异常——0.2.33 上线时
        # 就是这样：写入 0 行、时间戳照样落下，之后一周都不会再试一次。
        # 不盖的话每次启动都会重试，日志里也看得出来。
        if not failed:
            # **盖戳本身也是写库**，必须和回调的写库串行（快照线程在用同一个 conn）
            with write_lock, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO job_state(name, value)"
                    " VALUES ('refresh_actions.last', ?)", (str(_ymd(today)),))
            out["refresh_actions"] = True
        else:
            log.warning("除权/停牌刷新取数失败（除权=%s，停牌=%s），不盖时间戳（下次启动重试）",
                        wrote, susp)
    return out


def maintenance_on(conn) -> bool:
    """维护模式：`job_state('maintenance.flag') == '1'` 时跳过全部告警。"""
    row = conn.execute("SELECT value FROM job_state WHERE name='maintenance.flag'").fetchone()
    return bool(row) and str(row[0]) == "1"


def _warn_if_placeholder_config(cfg) -> None:  # pragma: no cover - 进程入口
    """占位域名/默认口令要在启动时喊出来，别让用户以为「跑起来了」。

    `public_host: stock.example.com` 出厂占位时，`api/routes.py` 的同源校验
    （`Origin == https://{public_host}`）会拒绝所有写操作——而健康检查、选盘、推送
    都正常，用户只会看到「网页按钮点了没反应」。同理 `SS_WEB_PASS` 未配置时网页不鉴权，
    公网部署等于裸奔。
    """
    from core.config import is_placeholder_host

    host = getattr(getattr(cfg, "notify", None), "public_host", None)
    if is_placeholder_host(host):
        log.warning("public_host 仍是占位值 %s：同源校验会拒绝所有写操作"
                    "（网页按钮点了没反应）。改 config/notify.yaml 与 deploy/ntfy/server.yml",
                    host)
    # **口令只在 api 侧检查**：`web.env` 按 compose 只挂给 api（agent/jobs 不对外），
    # jobs 里永远读不到 SS_WEB_PASS，在这里查只会产生一条恒真的误报告警。

    # westock 登录态是**单文件 bind mount**：宿主机上文件不存在时 docker 会建一个
    # 同名**目录**顶替，之后脚本写它直接 EISDIR。这个坑只在别人机器上出现
    # （作者本机文件早就有），所以必须在启动时明确说出来，而不是让人去翻容器日志。
    for path in ("/host-config/wb_client.json", "/host-config/wb_session.json"):
        p = Path(path)
        if p.exists() and p.is_dir():
            log.error("%s 是**目录**而不是文件：单文件 bind mount 在宿主机对应路径"
                      "不存在时会建空目录顶替。处理：在宿主机删掉该目录后 "
                      "`touch` 同名文件（见 README 快速开始第 1 步）", path)
        elif not p.exists():
            log.warning("%s 不存在：westock 续期会要求重新扫码（扫码需要人工）", path)


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


def main() -> int:                                  # pragma: no cover - 进程入口
    from core.config import load_config
    from core.db import connect, migrate
    from core.datasource.registry import SourceRegistry
    from core.datasource.sina import SinaSource
    from core.datasource.tencent import TencentSource
    from core.http import Http, RateLimiter
    from core.intraday.detector import EventDetector

    _setup_logging()
    _install_stack_dump()
    cfg = load_config(Path("config"))
    _warn_if_placeholder_config(cfg)
    conn = connect()
    migrate(conn)
    clock = SystemClock()
    from jobs.snapshot import make_recorder

    # 唯一出口带记账钩子：eod / members / backfill / auction 的 scope 才不会再恒为 0
    http = Http({"tencent": RateLimiter(0.3), "sina": RateLimiter(0.3)},
                on_request=make_recorder(conn, clock))
    reg = SourceRegistry(TencentSource(http), SinaSource(http))

    detector = EventDetector(conn, clock, cfg)
    detector.seed_today()
    dispatcher = _make_dispatcher(conn, cfg, clock)
    tick = IntradayTick(cfg, conn, detector, dispatcher, clock)
    loops = SnapshotLoops(
        cfg, reg, conn, clock,
        on_pool=tick.on_pool, on_sector=tick.on_sector,
        # 不接这行，「数据源连续失败 60 秒发 P0」永远不触发（告警只写在纸面上）
        on_alert=lambda kind, text, level="P0": dispatcher.alert(kind, text, level),
        lock=tick.lock)

    # 09:00 自选股导入读的是 `/state/westock.env`（D13 的可写挂载）
    _westock = _make_westock()
    sched, state = make_scheduler(cfg, conn, clock, loops=loops, tick=tick,
                                  dispatcher=dispatcher, reg=reg, mcp=_westock)
    # `startup_recovery` **自己管理锁**：它先 `loops.start()` 拉起快照线程，
    # 之后凡写共享连接的动作（marker、除权/停牌回调）都在 `loops.lock` 内；
    # 只有 EOD 补跑例外——它用独立连接，事务与共享连接隔离。
    # 早先这里写过「调度器没起来就没有并发写者」，那是错的——漏了 `loops.start()`
    # 拉起的快照线程（Codex 用 LockSpy 观测到 lock_enters=0 且线程已启动）。
    # **心跳线程必须在启动恢复之前起**：`startup_recovery` 可能要拉几分钟的
    # 除权数据（滚动刷新），而容器 healthcheck 判的是「/tmp/jobs_healthy 2 分钟内
    # 被 touch 过」、`start_period` 只有 300 秒。放在恢复之后会导致容器在恢复
    # 期间被判 unhealthy（实测 Up 13 分钟 unhealthy，FailingStreak=9）。
    threading.Thread(target=_touch_loop, args=(state,), daemon=True).start()

    refine_actions, refine_susp = make_refresh_hooks(_westock, cfg, conn)
    startup_recovery(cfg, conn, clock, reg=reg, tick=tick, dispatcher=dispatcher, loops=loops,
                     refresh_actions=refine_actions, refresh_suspensions=refine_susp)
    # **必须在 startup_recovery 之后再算一次**：它可能耗时数十分钟，
    # 而补跑任务的 run_date 是在 make_scheduler 里基于那时的 now 算的。
    state["arm_startup_renew"]()

    def _graceful(*_):
        sched.shutdown(wait=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    sched.start()                                   # 阻塞主线程直到进程被杀
    return 0


def _make_dispatcher(conn, cfg, clock):
    """真实派发层：token 只从 env `NTFY_TOKEN` 读（密钥不进 yaml）。"""
    import os

    from notify.dispatcher import Dispatcher
    from notify.ntfy import NtfyClient

    ntfy = NtfyClient(cfg.notify.ntfy_url, cfg.notify.topic, os.environ.get("NTFY_TOKEN"))
    return Dispatcher(conn, ntfy, cfg, clock)


def _make_westock():                                   # pragma: no cover - 进程入口
    """`/state/westock.env` 是可写挂载点（D13）；token 缺失时整个封装降级、不抛。"""
    from mcp.westock import WestockMcp

    return WestockMcp(env_path=Path("/state/westock.env"))


def make_refresh_hooks(mcp, cfg, conn):
    """返回 `(refresh_actions, refresh_suspensions)`；客户端不支持时返回 `(None, None)`。

    **这两个刷新从来没有真正跑过**：`startup_recovery` 有「键缺失或距上次 ≥7 天就补跑」
    的分支，但 `main()` 一直没把回调传进去（参数默认 None）。生产库的证据：
    `corporate_actions` 27 行**全部**是 `snapshot_implied`（快照反推），
    `suspension_calendar` 0 行，`refresh_actions.last` 从未写入。
    而 `jobs/eod.py::refresh_actions` 需要的 `westock.batch_dividend()` /
    `westock.suspension()` 在 `mcp/westock.py::WestockMcp` 上**并不存在**
    （测试用的是 `FakeWestockCli`，那个 CLI 客户端没实现）。

    现在 `WestockMcp` 已实现 `batch_dividend()` / `suspension()`（真实数据源是
    MCP 的 `data_dividend` / `data_suspension`），所以正常路径会返回真回调。
    能力缺失时仍然返回 `(None, None)` 并打 warning——**不假装接上了**。
    """
    missing = [m for m in ("batch_dividend", "suspension") if not hasattr(mcp, m)]
    if missing:
        log.warning("除权/停牌周刷新未接线：westock 客户端缺少 %s；"
                    "`corporate_actions` 只会来自快照反推、`suspension_calendar` 会一直是空表",
                    "、".join(missing))
        return None, None

    from jobs.eod import (
        ACTIONS_MAX_REQUESTS,
        actions_cursor,
        advance_actions_cursor,
        store_suspensions,
    )

    all_codes = [r[0] for r in conn.execute("SELECT code FROM stock_meta ORDER BY code")]

    def _actions(lock=None):
        """取一段、写库、前移游标。

        `lock` 给定时**取数在锁外、写库在锁内**：取数要发 4 个请求还带退避，
        放进写锁里会把每秒的 tick 堵住（`test_members_job_does_not_fetch_inside_the_lock`
        记录过这类故障）。
        """
        from contextlib import nullcontext

        from jobs.eod import fetch_actions, store_actions

        # 一轮只拉游标之后的一小段（上游限频决定了一次拉不完），下一轮接着走
        tail = actions_cursor(conn, all_codes)
        batch = tail[: ACTIONS_MAX_REQUESTS * 10]
        actions, done = fetch_actions(mcp, batch, max_requests=ACTIONS_MAX_REQUESTS)
        if actions is None:
            return None                      # 失败：游标不动，下次重试同一段
        with (lock if lock is not None else nullcontext()):
            written = store_actions(conn, actions)
            # **游标只按"确实收到响应的代码"推进**：限频伪装成空/残缺数据时，
            # 没出现的票不能算处理过，否则滚动一圈约 3 天才再遇到。
            # 用 `done` 而不是整个 `batch`，残缺返回也不会把缺失的票跳过去。
            advance_actions_cursor(conn, all_codes, done,
                                   start=batch[0] if batch else None)
        return written

    def _suspensions(lock=None):
        """取数在锁外、写库在锁内（与 `_actions` 同一套理由）。"""
        from contextlib import nullcontext

        from jobs.eod import fetch_suspensions

        rows = fetch_suspensions(mcp)
        if rows is None:
            return None
        from core.clock import TradingCalendar

        # **必须传交易日历**：`suspension()` 给的是复牌日，而 `end_date` 的契约是
        # 「最后一个停牌日」。不传 cal 就会退化成"复牌日 - 1 天"的自然日近似，
        # 周一复牌会落成周日，`resume_dates()` 的 `end_date == 上一交易日`
        # 永远对不上，`resume_cooldown` 迟一天（节后首日迟得更多）。
        cal = TradingCalendar(conn)
        with (lock if lock is not None else nullcontext()):
            return store_suspensions(conn, rows, cal=cal, codes=all_codes)

    return _actions, _suspensions


def _touch_loop(state) -> None:                     # pragma: no cover
    while True:
        Path("/tmp/jobs_healthy").touch()
        time.sleep(30)


def _in_session(now: datetime) -> bool:
    hm = now.strftime("%H:%M")
    return "09:15" <= hm <= "15:05"


def _ymd(d) -> int:
    return int(d.strftime("%Y%m%d")) if hasattr(d, "strftime") else int(d)


def _days_between(a: int, b: int) -> int:
    """两个 YYYYMMDD 之间的**自然日**差（不是整数差）。"""
    return (datetime.strptime(str(a), "%Y%m%d") - datetime.strptime(str(b), "%Y%m%d")).days


class _DayClock:
    """把补跑钉在某个交易日：`run_eod` 只接受 `Clock`，且只看 `today()`。"""

    def __init__(self, clock, day: int):
        self._clock = clock
        self._day = day

    def today(self) -> int:
        return self._day

    def now(self) -> datetime:
        return datetime.strptime(str(self._day), "%Y%m%d").replace(
            hour=15, minute=30, tzinfo=self._clock.now().tzinfo)


def _cached_quotes(conn, scope: str) -> dict:
    """从 `snapshot_cache` 读出该 scope 的报价（`Quote` 的属性形状）。"""
    row = conn.execute("SELECT payload FROM snapshot_cache WHERE scope = ?", (scope,)).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    out = {}
    for item in payload.get("rows") or []:
        ts = item.get("ts")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                ts = None
        code = item.get("code")
        if not code:
            continue
        out[code] = SimpleNamespace(
            code=code, name=item.get("name"), price=item.get("price"),
            prev_close=item.get("prev_close"), volume=item.get("volume"),
            amount=item.get("amount"), ts=ts)
    return out


def _auction_baselines(conn, today: int, cfg) -> dict[str, list[float]]:
    """每只票最近 N 个交易日的竞价量（股），供 `eval_auction` 算量能异动。"""
    days = int(cfg.strategy.intraday.auction.baseline_days)
    out: dict[str, list[float]] = {}
    for code, vol in conn.execute(
            "SELECT code, auction_vol FROM auction_daily WHERE trade_date < ? "
            "ORDER BY trade_date DESC", (today,)):
        if vol is None:
            continue
        values = out.setdefault(code, [])
        if len(values) < days:
            values.append(float(vol))
    return out


def _last_eod_date(conn) -> int | None:
    rows = [str(r[0]) for r in conn.execute("SELECT name FROM job_state WHERE name LIKE 'eod.%'")]
    days = [int(r.split(".", 1)[1]) for r in rows if r.split(".", 1)[1].isdigit()]
    return max(days) if days else None


def _json_load(raw) -> dict:
    """心跳 detail 的读侧；坏 JSON 视为空（旧版本行没有该键）。"""
    if not raw:
        return {}
    try:
        return json.loads(raw) or {}
    except ValueError:
        return {}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":                           # pragma: no cover
    sys.exit(main())
