"""回调票据的 HMAC 签名（§7.2.6、D21）。

**purpose 与 exp_ts 必须进签名原文**。旧写法 `f"{id}:{date}:read"` 把 purpose 写死成
`:read`、有效期写死成自然日，于是同一个 `id` 在 `signals` 与 `agent_turn` 两张表上
（都是自增、都从 1 开始）会算出同一张票据：通知里可见的「标记已读」URL 可以被改投成
写操作端点。把 purpose 与绝对过期时刻一起签进原文，两类票据就互不通用。
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, time
from typing import Literal

from core.clock import SH

# `session` 是「网页访问票据」：推送里的链接带上它，点开即自动登录。
# 与 read/confirm 用不同 purpose，三类票据互不通用（同一 id 也签不出同一张票）。
Purpose = Literal["read", "confirm", "session"]
_SECRET_BYTES = 32


def ensure_secret(conn) -> bytes:
    """取 HMAC 密钥；`app_secret` 里没有就生成并写入。**重启不换**（换了所有在飞的票据全失效）。"""
    row = conn.execute("SELECT value FROM app_secret WHERE name='hmac_key'").fetchone()
    if row is not None:
        value = row[0]
        if isinstance(value, bytes):
            return value
        return bytes.fromhex(value)
    secret = os.urandom(_SECRET_BYTES)
    try:
        with conn:
            # `INSERT OR IGNORE`：api 与 jobs 首启会同时走到这里，REPLACE 会让后到者
            # 覆盖先到者的密钥，败者进程手里的票据全部验签失败直到重启
            conn.execute("INSERT OR IGNORE INTO app_secret(name, value) VALUES ('hmac_key', ?)",
                         (secret.hex(),))
    except Exception:                      # noqa: BLE001 —— 撞锁就重读下面那次
        pass
    row = conn.execute("SELECT value FROM app_secret WHERE name='hmac_key'").fetchone()
    if row is not None:
        return bytes(row[0]) if isinstance(row[0], bytes) else bytes.fromhex(row[0])
    return secret


def sign(secret: bytes, purpose: Purpose, ref_id: int, exp_ts: int) -> str:
    """`hmac_sha256(secret, f"{purpose}:{ref_id}:{exp_ts}")` 的十六进制。"""
    payload = f"{purpose}:{ref_id}:{exp_ts}".encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify(secret: bytes, purpose: Purpose, ref_id: int, exp_ts: int, token: str) -> bool:
    """签名匹配。**调用方还必须自己判 `time.time() < exp_ts`**——本函数只看签名。"""
    if not token:
        return False
    return hmac.compare_digest(sign(secret, purpose, ref_id, exp_ts), str(token))


def day_end_ts(trade_date: int) -> int:
    """该交易日 23:59:59（Asia/Shanghai）的 epoch 秒。"""
    d = datetime.strptime(str(trade_date), "%Y%m%d").replace(
        hour=23, minute=59, second=59, tzinfo=SH)
    return int(d.timestamp())


def is_expired(exp_ts: int, now: float | None = None) -> bool:
    """票据是否过期：`verify` 之外的第二个必要条件，必须单独调。"""
    import time as _time

    return (now if now is not None else _time.time()) >= exp_ts
