"""westock token 的按需续期（计划 Task 6，D13 / 场景 C8）。

**「每天至多一次」的唯一落点是 `app_secret('westock_renew.<YYYYMMDD>')`**：不存这个
键，同一天多次调用会重复问腾讯、撞风控。所以：

- 剩余有效期 ≥ 24 小时 → `skip`（还没到续期窗口）；
- 当天已经尝试过 → `skip`；**失败也要写这个键**，否则 08:40 的调度 + 任意手动调用
  会在同一天连打腾讯；
- 失败后允许隔 ≥ 5 分钟**重试 1 次**，重试本身写
  `westock_renew_retry.<YYYYMMDD>` 防无限重试；重试再失败 → P1「腾讯自选股授权需要
  扫码」并停手（扫码是只有人能做的一步，脚本再跑一百次也不会自己好）。

`app_secret` 是纯 KV 表，读写就是两行 SQL。**不要拿 `ensure_secret` 当通用 getter**：
它只负责 HMAC 密钥，名字对不上时它会现场生成一把新密钥。

`westock_expires_at` 存 **epoch 秒的字符串**——两个读者（本模块与 `jobs/main.py` 的
08:40 任务）按同一格式比较，存 ISO 串会在 `int()` 上炸。
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("westock_renew")

EXPIRES_KEY = "westock_expires_at"
RENEW_KEY = "westock_renew.{day}"
RETRY_KEY = "westock_renew_retry.{day}"
ENV_PATH = Path("/state/westock.env")
SCRIPT = Path("scripts/wb_auth_headless.mjs")
# **扫码登录态的缓存路径**：`wb_auth_headless.mjs` 默认放 `$HOME/.config/stock-screener/`，
# 但容器里的 $HOME 不是宿主机的。这里显式传给脚本（脚本支持
# `--client-cache` / `--session-cache`），路径经 `WB_CLIENT_CACHE` / `WB_SESSION_CACHE`
# 注入——宿主机与容器都能对上同一个文件。
CLIENT_CACHE_ENV = "WB_CLIENT_CACHE"
SESSION_CACHE_ENV = "WB_SESSION_CACHE"
KEEP_S = 24 * 3600                       # 剩余 ≥ 24h 不续
RETRY_AFTER_S = 300                      # 失败后至少隔 5 分钟才允许唯一一次重试
TIMEOUT_S = 600                          # 扫码要人动手，给足时间


def kv_get(conn, name: str) -> str | None:
    row = conn.execute("SELECT value FROM app_secret WHERE name = ?", (name,)).fetchone()
    return None if row is None else str(row[0])


def kv_set(conn, name: str, value: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO app_secret(name, value) VALUES (?, ?)",
                     (name, str(value)))


def renew_if_needed(conn, clock, runner=subprocess.run, *, dispatcher=None,
                    env_path=ENV_PATH, script=SCRIPT, node="node") -> str:
    """返回 `"skip"` / `"renewed"` / `"failed"`。

    `runner` 的契约与 `subprocess.run` 一致（`runner(cmd, capture_output=True,
    text=True, timeout=…)` → 有 `returncode` 的对象），测试注入假体。
    """
    now_ts = int(clock.now().timestamp())
    day = clock.today()
    day_key, retry_key = RENEW_KEY.format(day=day), RETRY_KEY.format(day=day)

    stored = kv_get(conn, EXPIRES_KEY)
    expires = int(stored) if stored and stored.isdigit() else None
    if expires is not None and expires - now_ts >= KEEP_S:
        # 每个 skip 都记日志：事后要能回答「为什么今天没续期」
        log.info("续期 skip：还剩 %.1f 小时（阈值 %d 小时），无需续",
                 (expires - now_ts) / 3600, KEEP_S // 3600)
        return "skip"
    if kv_get(conn, retry_key) is not None:
        log.info("续期 skip：当天唯一一次重试已用掉（%s）", retry_key)
        return "skip"                      # 当天唯一一次重试已用掉
    marker = kv_get(conn, day_key)
    if marker is not None:
        if marker.startswith("ok"):
            log.info("续期 skip：当天已经续过（%s=%s）", day_key, marker)
            return "skip"
        last = _ts_of(marker)
        if last is None or now_ts - last < RETRY_AFTER_S:
            log.info("续期 skip：上次失败不久（%s），退避 %d 秒内不重试",
                     marker, RETRY_AFTER_S)
            return "skip"                  # 退避窗口内：不打腾讯

    retrying = marker is not None
    import os

    ok = _run(runner, node, script, env_path,
              client_cache=Path(os.environ[CLIENT_CACHE_ENV]) if os.environ.get(CLIENT_CACHE_ENV) else None,
              session_cache=Path(os.environ[SESSION_CACHE_ENV]) if os.environ.get(SESSION_CACHE_ENV) else None)
    if not retrying:
        # 无论成败都写：这是「每天至多一次」的唯一落点
        kv_set(conn, day_key, f"{'ok' if ok else 'fail'}:{now_ts}")
    if ok:
        if retrying:
            kv_set(conn, retry_key, str(now_ts))
        exp = _read_expires(env_path)
        if exp is not None:
            kv_set(conn, EXPIRES_KEY, str(exp))
        log.info("续期 renew 成功%s：新到期时间 %s",
                 "（重试）" if retrying else "",
                 "未知" if exp is None else f"epoch={exp}")
        return "renewed"
    if not retrying:
        log.warning("续期失败（第一次）：%d 秒后还有一次重试机会", RETRY_AFTER_S)
        return "failed"                    # 第一次失败不告警：5 分钟后还有一次机会
    kv_set(conn, retry_key, str(now_ts))
    log.error("续期失败（重试也用掉）：需要人工扫码")
    if dispatcher is not None:
        dispatcher.alert("westock_renew", "腾讯自选股授权需要扫码", "P1")
    return "failed"


def _run(runner, node: str, script: Path, env_path: Path,
         client_cache: Path | None = None, session_cache: Path | None = None) -> bool:
    cmd = [node, str(script), "--out", str(env_path)]
    if client_cache is not None:
        cmd += ["--client-cache", str(client_cache)]
    if session_cache is not None:
        cmd += ["--session-cache", str(session_cache)]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except Exception:                                             # noqa: BLE001
        return False
    return getattr(proc, "returncode", 1) == 0


def _read_expires(env_path: Path) -> int | None:
    """从脚本刚写出的 env 文件里取 `WESTOCK_EXPIRES_AT`（epoch 秒）。"""
    try:
        text = Path(env_path).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("WESTOCK_EXPIRES_AT="):
            value = line.split("=", 1)[1].strip()
            return int(value) if value.isdigit() else None
    return None


def _ts_of(marker: str) -> int | None:
    """`"fail:1761800000"` → `1761800000`；坏值当「不知道上次何时跑的」。"""
    tail = marker.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else None
