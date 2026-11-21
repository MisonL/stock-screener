

def test_disk_check_alerts_before_the_disk_is_full(conn):
    """**磁盘告警必须提前，而不是等到写不进去**（v0.2.108）。

    2026-11-21：Docker 虚拟磁盘写满，PostgreSQL 报
    `could not write lock file "postmaster.pid": No space left on device`。
    当时磁盘检查挂在**每日 16:00 的 cleanup** 里、阈值 85/95——
    一天只查一次，而从 70% 涨到 100% 只花了几个小时，
    等 cleanup 跑起来时已经晚了。

    现在 `check_disk()` 每 10 分钟跑一次，并且 **75% 就提前喊**。
    """
    from jobs.cleanup import check_disk

    calls = []

    class D:
        def alert(self, kind, text, level="P1"):
            calls.append((kind, level))

    check_disk(conn, dispatcher=D(), path="/")
    # 用真实的 `/` 水位：只要没到 75% 就不该喊
    import shutil
    used = shutil.disk_usage("/").used / shutil.disk_usage("/").total * 100
    if used < 75:
        assert calls == [], f"磁盘才 {used:.0f}% 就喊了：{calls}"
    else:
        assert calls, f"磁盘已 {used:.0f}% 却没喊"


def test_disk_thresholds_are_ordered_and_low_enough():
    """三档阈值必须递增，且最低档要留出清理时间。

    原来的 85/95 两档在"一天一查"的前提下等于没有预警；
    留 25 个百分点给运维清理，是因为 230 GB 的可写层不是一分钟能删完的。
    """
    from jobs.cleanup import DISK_CRIT_PCT, DISK_HIGH_PCT, DISK_WARN_PCT

    assert DISK_WARN_PCT < DISK_HIGH_PCT < DISK_CRIT_PCT
    assert DISK_WARN_PCT <= 80, f"最低档 {DISK_WARN_PCT}% 太晚：来不及清理"


def test_backup_freshness_alerts_when_a_night_was_skipped(tmp_path):
    """**备份漏跑必须被喊出来**（v0.2.108）。

    2026-11-21 实测到的真实缺口：`data/backups/` 里有 11-13/14/16/17/18/19，
    独独缺 11-20——那天晚上连着做了几次部署，jobs 容器在 23:30 前后被重建，
    而备份只有 `misfire_grace_time=3600` 兜底，过了就**静默跳过**。
    7 份保留位里出现一个空洞，直到真要从备份恢复的那天才会发现。
    """
    import os
    from datetime import datetime, timedelta, timezone
    from jobs.cleanup import check_backup_freshness

    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    now = datetime(2026, 11, 21, 8, 55, tzinfo=timezone.utc)

    def _make(name: str, hours_ago: float):
        p = data / "backups" / name
        p.write_bytes(b"x")
        ts = now.timestamp() - hours_ago * 3600
        os.utime(p, (ts, ts))

    calls = []

    class D:
        def alert(self, kind, text, level="P1"):
            calls.append((kind, level))

    # 新鲜：昨晚 23:30 的备份，约 9.4 小时前
    _make("market-20261120.db", 9.4)
    assert check_backup_freshness(None, str(data), now=now, dispatcher=D()) < 10
    assert calls == [], f"备份很新却报警：{calls}"

    # 陈旧：昨天 11-19 的备份，约 33 小时前 —— 11-20 那晚漏了
    calls.clear()
    _make("market-20261120.db", 40)
    age = check_backup_freshness(None, str(data), now=now, dispatcher=D())
    assert age > 36
    assert calls and calls[0][0] == "backup_stale", f"漏跑一夜没被喊出来：{calls}"


def test_disk_alert_levels_do_not_suppress_each_other(conn):
    """**水位升级必须发得出去**（v0.2.109，Codex 复核 S2）。

    `Dispatcher.alert` 的 30 分钟去重判据是 `kind + 时间`，**不看 level**。
    三档如果共用 `kind="disk"`，75% 先发一条 P2 之后，10 分钟内升到
    95% 的 P0 会被"同 kind 刚发过"直接压掉——**最该升级的那次反而发不出去**。
    分档 kind（`disk_p2`/`disk_p1`/`disk_p0`）让它们各自去重。
    """
    from jobs.cleanup import _alert_disk

    kinds = []

    class D:
        def alert(self, kind, text, level="P1"):
            kinds.append((kind, level))

    d = D()
    _alert_disk(d, 76.0)
    _alert_disk(d, 86.0)
    _alert_disk(d, 96.0)

    assert [k for k, _ in kinds] == ["disk_p2", "disk_p1", "disk_p0"], (
        f"三档共用了 kind，升级会被去重压掉：{kinds}")
    assert [lv for _, lv in kinds] == ["P2", "P1", "P0"]


def test_single_missed_night_crosses_the_stale_threshold():
    """**单晚备份中断必须摸到告警阈值**（v0.2.112，评审抓的假绿）。

    备份在 23:30 跑，次日 08:55 自检，若昨晚那晚没跑成，age 恰好是
    **33.4 小时**。我原来设的阈值是 36——单晚断档永远摸不到它，
    连漏两晚（57.4h）才报警。等于对最常发生的故障完全失明。
    """
    from jobs.cleanup import BACKUP_STALE_H

    # 正常：23:30 备份 → 次日 08:55 检查 = 9.42h
    # 单晚断档：最新备份退回前一天的 23:30 → 33.42h
    normal = 9.42
    single_night = 24 + normal
    # 阈值要大于正常间隔（否则天天误报）、小于单晚断档（否则单晚漏报）
    assert normal < BACKUP_STALE_H < single_night, (
        f"阈值 {BACKUP_STALE_H}h 没落在（正常 {normal:.1f}h，单晚 {single_night:.1f}h）"
        "之间：偏大则单晚断档不报警，偏小则天天误报")


def test_self_check_runs_every_day_including_weekends():
    """**备份陈旧自检不能只在工作日跑**（v0.2.112）。

    周五 23:30 若被中断，周六 08:55 不跑、周日也不跑，到周一才检查——
    age 已 80+ 小时，当然会报，但**周末两天没有任何告警窗口**。
    数据库 7×24 在写，备份检查没有理由跳过周末。
    """
    import inspect
    from jobs import main as jobs_main

    src = inspect.getsource(jobs_main.make_scheduler)
    idx = src.index('CronTrigger(hour=8, minute=55')
    line_end = src.index(")", idx)
    chunk = src[idx:line_end]
    assert "day_of_week" not in chunk, (
        f"自检仍限工作日：{chunk} —— 周末断档会没有告警窗口")
