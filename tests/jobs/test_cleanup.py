

def test_disk_check_alerts_before_the_disk_is_full(conn, tmp_path, monkeypatch):
    """**磁盘告警必须提前，而不是等到写不进去**（v0.2.108）。

    2026-11-21：Docker 虚拟磁盘写满，PostgreSQL 报
    `could not write lock file "postmaster.pid": No space left on device`。
    当时磁盘检查挂在**每日 16:00 的 cleanup** 里、阈值 85/95——
    一天只查一次，而从 70% 涨到 100% 只花了几个小时，
    等 cleanup 跑起来时已经晚了。

    现在 `check_disk()` 每 10 分钟跑一次，并且 **75% 就提前喊**。
    """
    from jobs import cleanup
    from jobs.cleanup import DISK_WARN_PCT, check_disk, disk_used_pct

    calls = []

    class D:
        def alert(self, kind, text, level="P1"):
            calls.append((kind, level))

    check_disk(conn, dispatcher=D(), path=tmp_path)
    # **这里必须用 `disk_used_pct()` 的判据**（2026-11-26 修）。
    # 原来用 `shutil.disk_usage().used/total` 自己算一遍，而实现用的是
    # `used/(used+free)`（不含 ext4 给 root 保留的 5%，见 `disk_used_pct`
    # 的注释）。两个百分比差 4 个点左右，于是**磁盘一涨过 75% 这条用例就必红**
    # ——红的时候报的是「磁盘才 74% 就喊了」，把人引向阈值逻辑，
    # 而真正错的是这条用例自己的算式。
    used = disk_used_pct(str(tmp_path))
    assert used is not None, "拿不到水位，后面的断言没有意义"
    if used < DISK_WARN_PCT:
        assert calls == [], f"磁盘才 {used:.0f}% 就喊了：{calls}"
    else:
        assert calls, f"磁盘已 {used:.0f}% 却没喊"

    # **阈值边界必须确定性地验一遍**，不能只靠"当前这台机器恰好没满"：
    # 水位由 `monkeypatch` 直接给定，与本机磁盘无关。
    # `None`（读不到水位）**必须告警**：静默才是真正危险的形态。
    for pct, should_alert in [(50.0, False), (74.9, False),
                              (75.0, True), (96.0, True), (None, True)]:
        monkeypatch.setattr(cleanup, "disk_used_pct", lambda _p, v=pct: v)
        calls.clear()
        cleanup.check_disk(conn, dispatcher=D(), path=str(tmp_path))
        if should_alert:
            assert calls, f"水位 {pct}% 却没告警"
        else:
            assert calls == [], f"水位 {pct}% 却告警了：{calls}"


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


def test_single_missed_night_actually_alerts(tmp_path):
    """**单晚断档必须真的发出告警**（v0.2.114，评审抓的行为级假绿）。

    上一版这条只断言「常量落在(9.42,33.42)之间」——不调用
    `check_backup_freshness`。评审的存活变异：常量仍是29，但把函数默认参数
    写死成 `stale_h=36`，于是**真实阈值退回旧 bug、测试全绿**。

    这里直接构造「最新备份是33.4小时前」，调函数，断言真的 `alert`了。
    """
    import os
    from datetime import datetime, timezone

    from jobs.cleanup import check_backup_freshness

    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    #11-2023:30的备份，检查时刻是11-2208:55 →33.4h
    f = data / "backups" / "market-20261120.db"
    f.write_bytes(b"x")
    check_at = datetime(2026,11,22,8,55, tzinfo=timezone.utc)
    ts = check_at.timestamp() -33.42 *3600
    os.utime(f, (ts, ts))

    calls = []

    class D:
        def alert(self, kind, text, level="P1"):
            calls.append((kind, level))

    age = check_backup_freshness(None, data, now=check_at, dispatcher=D())

    assert age is not None and age >33, f"算出来的 age不对：{age}"
    assert calls and calls[0][0] == "backup_stale", (
        f"单晚断档（{age:.1f}h）**没有**发出 backup_stale告警：{calls}")


def test_recent_backup_does_not_alert(tmp_path):
    """正常情况（9.4h）不能误报——否则每天都会响。"""
    import os
    from datetime import datetime, timezone

    from jobs.cleanup import check_backup_freshness

    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    f = data / "backups" / "market-20261121.db"
    f.write_bytes(b"x")
    check_at = datetime(2026,11,22,8,55, tzinfo=timezone.utc)
    ts = check_at.timestamp() -9.42 *3600
    os.utime(f, (ts, ts))

    calls = []

    class D:
        def alert(self, kind, text, level="P1"):
            calls.append((kind, level))

    age = check_backup_freshness(None, data, now=check_at, dispatcher=D())

    assert age is not None and age <10
    assert calls == [], f"备份很新却报警：{calls}"
