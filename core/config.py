"""配置加载：三份 YAML → 只读配置对象。

阈值全部住在 `config/` 的 YAML 里，代码里不写默认值；缺一个键就是配置错误。
不要用 `.get()` 兜底 —— 兜底会把「配置漏了一个阈值」静默变成一个不同的策略。
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml


class ConfigError(Exception):
    """配置缺键、市场未实现或文件不可读。"""


class FrozenDict(dict):
    """只读 dict：任何就地修改都抛 TypeError。

    不是真不可变（仍可被强转结构引用底下的 list），但在常规代码路径上足以挡住
    对配置的静默就地改写——那会让同进程后续所有判定悄悄跑偏。
    """

    _LOCKED = False

    def _block(self, *a, **k):
        raise TypeError("配置是只读的")

    __setitem__ = _block
    __delitem__ = _block
    pop = _block
    popitem = _block
    clear = _block
    update = _block
    setdefault = _block


def _freeze(d):
    """递归把 dict 变成 FrozenDict、list 变成 tuple，防就地改配置容器。"""
    if isinstance(d, dict):
        return FrozenDict({k: _freeze(v) for k, v in d.items()})
    if isinstance(d, list):
        return tuple(_freeze(v) for v in d)
    return d


class _NS:
    """嵌套 dict 的只读属性视图。

    既支持 `cfg.box.min_n`，也保留 dict 语义（`list(curve)`、`.values()`），
    因为 curve 的键是时刻、weights 要 sum(values())。深层容器已用 FrozenDict /
    tuple 冻结——只阻止属性赋值不够，`cfg.strategy._d["weights"]["A1"]=0` 会静默改配置。
    """

    __slots__ = ("_d",)

    def __init__(self, data: dict):
        object.__setattr__(self, "_d", _freeze(data))

    def __getattr__(self, name: str):
        try:
            value = self._d[name]
        except KeyError:
            raise AttributeError(f"配置里没有键 {name!r}") from None
        return _NS(value) if isinstance(value, FrozenDict) else value

    def __setattr__(self, name, value):
        raise AttributeError("配置是只读的")

    def __getitem__(self, name):
        value = self._d[name]
        return _NS(value) if isinstance(value, FrozenDict) else value

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    def __contains__(self, name):
        return name in self._d

    def keys(self):
        return self._d.keys()

    def values(self):
        return self._d.values()

    def items(self):
        return self._d.items()

    def get(self, name, default=None):
        value = self._d.get(name, default)
        return _NS(value) if isinstance(value, dict) else value


_CURVE_TIMES = ("09:45", "10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")

_MARKET_PARAMS = {
    "box_range": None,
    "daily_amp": None,
    "slope": None,
    "b2_T": None,
    "b3_T": None,
    "c1_buffer": None,
    "c3": None,
    "b8_buffer": None,
    "today_low": None,
    "a1": None,
    "a2_T": None,
    "a3_zero": None,
    "b6_T": None,
    "b9_T": None,
    "near_breakout": None,
    "min_amount_20d": None,
}

_REQUIRED_STRATEGY = {
    "direct_connection": None,
    "universe": {
        "min_bars": None,
        "box_amount_ratio": None,
        "suspend_max_in_250": None,
        "resume_cooldown": None,
        "pool_top_n": None,
        "pool_alert_min": None,
        "pool_overflow": None,
        "price_eps": None,
    },
    "box": {"min_n": None, "max_lookback": None},
    "decline": {
        "tail_max": None,
        "min_len": None,
        "discount_min_len": None,
        "oneword_down_reject_at": None,
    },
    "weights": {k: None for k in ("A1", "A2", "A3", "B2", "B3", "B4", "B5", "B6", "B7", "B9", "D1", "D2", "D3")},
    "mapping": {
        "b4": None,
        "b5_T": None,
        "b7_T": None,
        "surge_ratio": None,
        "d2_last_days": None,
        "d1_window": None,
        "d1_min_bars": None,
        "d1_sat": None,
        "atr_n": None,
        "pct_window": None,
        "macd": None,
        "macd_warmup": None,
    },
    "intraday": {
        "curve": {hm: None for hm in _CURVE_TIMES},
        "judge_start": None,
        "strict_until": None,
        "freeze_at": None,
        "c2": None,
        "c2_strict": None,
        "c2_down": None,
        "c1_over_prev_close": None,
        "halt_stale_s": None,
        "stall": {"vol": None, "window_min": None, "max_gain": None, "limit_gap": None},
        "index_move": {"window_s": None, "threshold": None, "cooldown_min": None, "codes": None},
        "sector_move": {"window_s": None, "threshold": None, "min_members": None, "cooldown_min": None},
        "watch": {
            "pct": {m: None for m in ("cn_main", "cn_gem", "cn_star", "cn_bse")},
            "vol_ratio": None,
            "near_extreme": None,
            "extreme_days": None,
        },
        "auction": {"gap": None, "vol_mult": None, "baseline_days": None},
    },
    "attention": {
        "base": {
            k: None
            for k in (
                "index_move",
                "sector_move",
                "breakdown",
                "watch_rule",
                "breakout",
                "auction_anomaly",
                "stall",
                "near_breakout",
                "data_anomaly",
            )
        },
        "breadth": {"stock": None, "sector_merged_3": None, "market": None},
        "focus": {k: None for k in ("outside", "pool", "watch", "market", "sector")},
        "salience_min": None,
        "salience_critical": None,
    },
    "by_market": {m: dict(_MARKET_PARAMS) for m in ("cn_main", "cn_gem", "cn_star", "cn_bse")},
}

_REQUIRED_MARKETS = {
    "cn_main": None,
    "cn_gem": None,
    "cn_star": None,
    "cn_bse": None,
    "hk": None,
    "us": None,
    "session": {"open_time": None, "close_time": None},
}

_REQUIRED_NOTIFY = {
    "public_host": None,
    "ntfy_url": None,
    "topic": None,
    "daily_cap": None,
    "dedupe_min": None,
    "p1_merge": {"count": None, "window_s": None},
    "sector_merge": {"count": None, "window_s": None},
    "overflow_digest_s": None,
    "open_brief_top": None,
    "p0": {"score": None, "vol": None},
    "p1_score": None,
}


# ---- 第二批：agent / llm 两份配置 ----------------------------------------
_REQUIRED_AGENT = {
    "heartbeat": {"base_min": None, "tight_min": None, "max_min": None,
                  "tight": None, "relaxed": None},
    "event": {"rounds": None, "deadline_s": None},
    "heartbeat_turn": {"rounds": None, "deadline_s": None},
    "context": {"max_chars": None, "sector_events": None, "own_history": None},
    # `context.own_history_trim` 已移除（v0.2.105）：全仓无读取点，也无明确语义
    "channel_b_daily_cap": None,
    # `budget_pace` 三键同样**故意不列进必填清单**（agent.yaml 是站点保留文件）。
    # **`channel_b_critical_extra` / `agent_note_min_gap_s` 故意不列在这里**：
    # `config/agent.yaml` 是站点保留文件（PRESERVE_ON_TARGET），旧站点没有这两个键，
    # 而 `load_config()` 对必填键缺失是**直接抛 ConfigError** 的——列进来等于
    # 让所有老站点在升级当天起不来。读取方全部用 `getattr(..., 默认值)`，
    # 缺键时行为与 `_EMPTY_AGENT` 一致。
    "fatigue": {"free": None, "step": None, "raise": None},
    # **`memory` 三键已彻底移除**（v0.2.152）：`agent/memory.py::write()` 全仓
    # 没有任何调用点，`agent_memory` 表生产上**一行都没有**，`recall()` 恒返回空，
    # 上下文的【相关记忆】块永远不出现。三个键连"调参"的对象都不存在。
    # 旧站点的 `agent.yaml` 里若还留着这三行，加载时被忽略——多余键不报错。
    "pending_action_ttl_h": None,
    "llm_down": {"consecutive": None, "minutes": None},
    "attention": {"shadow_mode": None},
}

# `llm:` 与 `retry:` 是根级并列键：重试策略读 cfg.retry，不是 cfg.llm.retry
_REQUIRED_LLM = {
    "llm": {
        "base_url": None,
        "api_key_env": None,
        "model": None,
        "thinking": None,
        "timeouts": {"agent_call_s": None, "chat_s": None},
        "max_tokens": {"plain": None, "tools": None},
        "budget": {"agent": None, "chat": None},
        "pricing": {"in_cny_per_mtok": None, "out_cny_per_mtok": None, "is_estimate": None},
    },
    "retry": {"max_attempts": None, "backoff": None, "retry_on": None, "never_retry": None},
}


def _paths(template: dict, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for key, value in template.items():
        path = prefix + (key,)
        if isinstance(value, dict):
            out.extend(_paths(value, path))
        else:
            out.append(path)
    return out


REQUIRED_KEYS: list[tuple[str, ...]] = (
    _paths(_REQUIRED_STRATEGY) + _paths(_REQUIRED_MARKETS) + _paths(_REQUIRED_NOTIFY)
    + [("agent",) + rest for rest in _paths(_REQUIRED_AGENT)]
    + [("llm",) + rest for rest in _paths(_REQUIRED_LLM)]
)

_EMPTY_AGENT = {
    "heartbeat": {"base_min": 5, "tight_min": 2, "max_min": 10, "tight": [], "relaxed": []},
    "event": {"rounds": 3, "deadline_s": 0},
    "heartbeat_turn": {"rounds": 6, "deadline_s": 0},
    "context": {"max_chars": 6000, "sector_events": 8, "own_history": 10},
    "channel_b_daily_cap": 15,
    # 预算配速：心跳让路、事件优先（见 agent/main.py::_paced_out）
    "budget_pace": {"enabled": True, "tolerance": 0.15, "floor": 0.35},
    # 极端行情可突破日上限的**额外**条数（见 agent/push.py::agent_note）
    "channel_b_critical_extra": 3,
    # 两条普通便签之间的最小间隔（秒）；0 = 不限制
    "agent_note_min_gap_s": 45,
    "fatigue": {"free": 10, "step": 5, "raise": 0.5},
    "pending_action_ttl_h": 24,
    "llm_down": {"consecutive": 5, "minutes": 10},
    "attention": {"shadow_mode": True},
}

_EMPTY_LLM = {
    "llm": {
        "base_url": "", "api_key_env": "DEEPSEEK_API_KEY", "model": "", "thinking": "disabled",
        # 0 = 不设超时（默认）。见 config/llm.yaml 的说明与 RUNBOOK。
        "timeouts": {"agent_call_s": 0, "chat_s": 0},
        "max_tokens": {"plain": 1000, "tools": 2000},
        "budget": {"agent": (300000, 40000), "chat": (200000, 20000)},
        "pricing": {"in_cny_per_mtok": 2.0, "out_cny_per_mtok": 8.0, "is_estimate": True},
    },
    "retry": {"max_attempts": 3, "backoff": (1.0, 3.0, 8.0),
              "retry_on": (), "never_retry": ()},
}

_MISSING = object()


def _lookup(data: dict, path: tuple[str, ...]):
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _load_yaml(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        raise ConfigError(f"读不到 {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} 不是合法 YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 顶层必须是映射")
    return data


class Config:
    """五份 YAML 的只读视图（批1 三份 + 批2 的 agent / llm）。

    `cfg.agent` / `cfg.llm` / `cfg.retry` 与 `cfg.strategy` 平级：`retry` 是 llm.yaml 的
    根级键，所以读法是 `cfg.retry.max_attempts`，不是 `cfg.llm.retry.max_attempts`。
    """

    def __init__(self, strategy: dict, markets: dict, notify: dict,
                 agent: dict | None = None, llm: dict | None = None):
        self._strategy_raw = strategy
        self._markets_raw = markets
        self._by_market = strategy["by_market"]
        self.strategy = _NS(strategy)
        self.markets = _NS(markets)
        self.notify = _NS(notify)
        self.agent = _NS(agent if agent is not None else _EMPTY_AGENT)
        llm_doc = llm if llm is not None else _EMPTY_LLM
        self.llm = _NS({**llm_doc.get("llm", {}), "retry": llm_doc.get("retry", {})})
        self.retry = _NS(llm_doc.get("retry", {}))

    def market(self, name: str) -> _NS:
        """该市场合并后的参数：策略公共段为底，`by_market.<name>` 覆盖。"""
        try:
            params = self._by_market[name]
        except KeyError:
            raise ConfigError(f"strategy.yaml 的 by_market 里没有市场 {name!r}") from None
        merged = copy.deepcopy(self._strategy_raw)
        merged.update(params)
        return _NS(merged)

    def enabled_markets(self) -> list[str]:
        """markets.yaml 里值为 true 的市场开关（保持文件顺序；hk/us 若为 true 早已在加载时报错）。"""
        return [name for name, on in self._markets_raw.items() if isinstance(on, bool) and on]


_MARKET_ADAPTER_MISSING = "数据适配器未实现，见计划决定 #1"


def load_config(dir: Path = Path("config")) -> Config:
    base = Path(dir)
    strategy = _load_yaml(base / "strategy.yaml")
    markets = _load_yaml(base / "markets.yaml")
    notify = _load_yaml(base / "notify.yaml")
    agent = _load_yaml(base / "agent.yaml") if (base / "agent.yaml").exists() else dict(_EMPTY_AGENT)
    llm = _load_yaml(base / "llm.yaml") if (base / "llm.yaml").exists() else dict(_EMPTY_LLM)

    for path in _paths(_REQUIRED_STRATEGY):
        if _lookup(strategy, path) is _MISSING:
            raise ConfigError("strategy.yaml 缺少配置键 " + ".".join(path))
    for path in _paths(_REQUIRED_MARKETS):
        if _lookup(markets, path) is _MISSING:
            raise ConfigError("markets.yaml 缺少配置键 " + ".".join(path))
    for path in _paths(_REQUIRED_NOTIFY):
        if _lookup(notify, path) is _MISSING:
            raise ConfigError("notify.yaml 缺少配置键 " + ".".join(path))

    for name, doc, tmpl, filename in (
        ("agent", agent, _REQUIRED_AGENT, "agent.yaml"),
        ("llm", llm, _REQUIRED_LLM, "llm.yaml"),
    ):
        if not (base / filename).exists():
            continue
        for path in _paths(tmpl):
            if _lookup(doc, path) is _MISSING:
                raise ConfigError(f"{filename} 缺少配置键 " + ".".join(path))

    unknown_switches = {name for name in markets if name not in _REQUIRED_MARKETS}
    if unknown_switches:
        raise ConfigError("markets.yaml 出现未知市场开关: " + ", ".join(sorted(unknown_switches)))
    for name, on in markets.items():
        if name == "session":
            continue
        if not isinstance(on, bool):
            raise ConfigError(f"markets.yaml 的 {name} 必须是布尔值，收到 {on!r}")

    _market_param_keys = set()
    for m, params in strategy["by_market"].items():
        if not isinstance(params, dict):
            raise ConfigError(f"strategy.yaml 的 by_market.{m} 必须是映射")
        _market_param_keys.update(params)
    collision = _market_param_keys & (set(strategy) - {"by_market"})
    if collision:
        raise ConfigError(
            "strategy.yaml 的 by_market.<市场> 参数与策略公共段同名，合并不确定: "
            + ", ".join(sorted(collision))
        )

    for market in ("hk", "us"):
        if markets.get(market):
            raise ConfigError(f"{market} {_MARKET_ADAPTER_MISSING}")

    return Config(strategy, markets, notify, agent, llm)


# **占位域名识别**：`config/notify.yaml` 出厂值用 `example.com` 系（RFC 2606 保留域）。
# 它不是真实域名，而 `api/routes.py` 的同源校验是 `Origin == https://{public_host}`——
# 于是所有写操作都会被 400 拒绝，但健康检查、选池、推送链路都照常绿，
# 用户只会看到「网页按钮点了没反应」。所以启动时要显式提示。
_PLACEHOLDER_HOSTS = ("example.com", "example.org", "example.net", "example.edu")


def is_placeholder_host(host: str | None) -> bool:
    """`host` 是否仍是 RFC 2606 的保留示例域名。"""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    return any(h == d or h.endswith("." + d) for d in _PLACEHOLDER_HOSTS)
