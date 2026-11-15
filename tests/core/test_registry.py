"""多源降级链与健康探测。"""
import pytest

from core.datasource.registry import HealthTracker, SourceRegistry


def test_falls_back_after_three_failures(fake_sources):
    reg = SourceRegistry(fake_sources.boom, fake_sources.ok, mono=fake_sources.mono)
    for _ in range(3):
        reg.kline_daily("sh600519", 10)
    bars, used = reg.kline_daily("sh600519", 10)
    assert used == "sina" and fake_sources.boom.calls == 3      # 第 4 次不再打主源


def test_slow_response_triggers_fallback(fake_sources):     # 测试 F-26：响应 > 3 秒也计一次失败
    reg = SourceRegistry(fake_sources.slow, fake_sources.ok, mono=fake_sources.mono)
    for _ in range(3):
        _, used = reg.kline_daily("sh600519", 10)
        assert used == "sina"          # 慢响应也要走备源
    assert not reg.health.available()  # 三次慢响应 = 主源不可用


def test_parse_error_triggers_fallback(fake_sources):       # {"code":0,"data":[]} → UpstreamError 计失败
    reg = SourceRegistry(fake_sources.boom, fake_sources.ok, mono=fake_sources.mono)
    _, used = reg.kline_daily("sh600519", 10)
    assert used == "sina"


def test_403_triggers_fallback(fake_sources):               # UpstreamBlocked 计失败
    reg = SourceRegistry(fake_sources.blocked, fake_sources.ok, mono=fake_sources.mono)
    _, used = reg.kline_daily("sh600519", 10)
    assert used == "sina"


def test_snapshot_also_falls_back(fake_sources):
    reg = SourceRegistry(fake_sources.upstream_fail, fake_sources.ok, mono=fake_sources.mono)
    quotes, used = reg.snapshot(["sh600519"])
    assert used == "sina" and "sh600519" in quotes


def test_recovers_after_cooldown(fake_sources):
    reg = SourceRegistry(fake_sources.boom, fake_sources.ok, mono=fake_sources.mono)
    for _ in range(3):
        reg.kline_daily("sh600519", 10)
    fake_sources.mono.advance(301)
    reg.kline_daily("sh600519", 10)
    assert fake_sources.boom.calls == 4        # 冷却结束，重新打主源


def test_health_tracker_threshold_and_cooldown():
    from tests.helpers import FakeMono

    mono = FakeMono()
    h = HealthTracker(fail_threshold=2, cooldown_s=10, mono=mono.now)
    assert h.available()
    h.record_failure()
    assert h.available()
    h.record_failure()
    assert not h.available()
    mono.advance(11)
    assert h.available()


def test_health_tracker_default_is_per_instance():   # 可变默认值会让所有 registry 共享失败状态
    a = SourceRegistry(None, None)
    b = SourceRegistry(None, None)
    assert a.health is not b.health


def test_snapshot_units_identical_across_sources(tencent_with_snapshot, sina_fixture):
    # §6.1「降级不改变单位语义」的硬断言
    tencent_q = tencent_with_snapshot.fetch_snapshot(["sh600519"])["sh600519"]   # f[6]=37283.83 手（手→股 ×100）
    sina_q = sina_fixture.fetch_snapshot(["sh600519"])["sh600519"]               # 3728383 股
    assert tencent_q.volume == sina_q.volume == 3728383


@pytest.mark.parametrize("value", ["sina", "1"])
def test_force_fallback_env(fake_sources, monkeypatch, value):
    monkeypatch.setenv("SS_FORCE_FALLBACK", value)
    reg = SourceRegistry(fake_sources.ok, fake_sources.other_ok, mono=fake_sources.mono)
    # 主源是 "ok"（永远成功），但强制降级后必须走备源
    _, used = reg.kline_daily("sh600519", 10)
    assert used == "tencent" and fake_sources.ok.calls == 0


def test_force_fallback_argument_wins_over_env(fake_sources, monkeypatch):
    monkeypatch.setenv("SS_FORCE_FALLBACK", "sina")
    reg = SourceRegistry(fake_sources.ok, fake_sources.other_ok, mono=fake_sources.mono, force_fallback=False)
    _, used = reg.kline_daily("sh600519", 10)
    assert used == "sina"


def test_registry_exposes_sina(fake_sources):
    reg = SourceRegistry(fake_sources.ok, fake_sources.ok, mono=fake_sources.mono)
    assert reg.sina is fake_sources.ok        # Task 12：refresh_sector_members 用 reg.sina
