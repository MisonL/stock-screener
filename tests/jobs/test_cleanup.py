

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
