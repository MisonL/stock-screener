import shutil
from pathlib import Path

import pytest

from core.config import ConfigError, load_config


def test_values_match_spec():
    cfg = load_config(Path("config"))
    main, gem = cfg.market("cn_main"), cfg.market("cn_gem")
    assert (main.box_range, main.daily_amp, main.slope) == (0.15, 0.03, 0.0015)
    assert gem.box_range == 0.30 and gem.b8_buffer == 0.96  # 缩放的是缓冲幅度（§4.4）
    assert gem.a1 == main.a1 and gem.a2_T == main.a2_T  # A1/A2 第一期 ×1（§4.4.0）
    assert sum(cfg.strategy.weights.values()) == 100
    assert list(cfg.strategy.intraday.curve) == [
        "09:45", "10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
    assert cfg.enabled_markets() == ["cn_main", "cn_gem", "cn_star"]


def test_missing_key_raises(tmp_path):
    dst = tmp_path / "config"
    shutil.copytree(Path("config"), dst)
    strategy = dst / "strategy.yaml"
    lines = strategy.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [ln for ln in lines if "max_lookback" not in ln]
    assert len(kept) == len(lines) - 1  # 确实删掉了一行，而不是键不存在
    strategy.write_text("".join(kept), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(dst)


def test_enabling_hk_fails(tmp_path):
    dst = tmp_path / "config"
    shutil.copytree(Path("config"), dst)
    markets = dst / "markets.yaml"
    text = markets.read_text(encoding="utf-8")
    assert "hk: false" in text
    markets.write_text(text.replace("hk: false", "hk: true"), encoding="utf-8")
    with pytest.raises(ConfigError, match="hk 数据适配器未实现"):
        load_config(dst)


def test_mutating_config_containers_is_blocked(CFG):
    # 只读不能只防属性赋值：容器内容被改会让同进程后续所有判定静默跑偏。
    with pytest.raises(TypeError):
        CFG.strategy.weights["A1"] = 0
    with pytest.raises(TypeError):
        CFG.strategy.intraday.curve["09:45"] = 9.9
    assert CFG.strategy.weights["A1"] == 15  # 值没被改掉


def test_list_values_are_tuples(CFG):
    a1 = CFG.market("cn_main").a1
    assert isinstance(a1, tuple) and a1 == (0.20, 0.35, 0.45, 0.60)
    with pytest.raises(AttributeError):
        a1.append(0.9)


def test_market_switch_must_be_bool(tmp_path):
    dst = tmp_path / "config"
    shutil.copytree(Path("config"), dst)
    markets = dst / "markets.yaml"
    text = markets.read_text(encoding="utf-8")
    assert "cn_gem: true" in text
    markets.write_text(text.replace("cn_gem: true", "cn_gem: 1"), encoding="utf-8")
    with pytest.raises(ConfigError, match="cn_gem"):
        load_config(dst)


def test_unknown_market_switch_rejected(tmp_path):
    dst = tmp_path / "config"
    shutil.copytree(Path("config"), dst)
    markets = dst / "markets.yaml"
    markets.write_text(
        markets.read_text(encoding="utf-8").replace("us: false", "us: false\njp: true"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="jp"):
        load_config(dst)


def test_by_market_key_colliding_with_strategy_section_rejected(tmp_path):
    dst = tmp_path / "config"
    shutil.copytree(Path("config"), dst)
    strategy = dst / "strategy.yaml"
    text = strategy.read_text(encoding="utf-8")
    assert text.count("    box_range: 0.15") == 1
    strategy.write_text(
        text.replace("    box_range: 0.15", "    box_range: 0.15\n    box: 1", 1),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="box"):
        load_config(dst)
