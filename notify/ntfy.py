"""ntfy 客户端（§7.2.6）。

**发布形态固定用 header**（`X-Title` / `X-Actions` 等），不用 JSON publish：
spec 的全部实测证据（`X-Sequence-ID` 可用、`X-Message-Id` 静默失效）都来自 header 形态，
JSON publish 能否设 `sequence_id` 从未实测。代价是 `X-Actions` 的分隔符规则要自己守：
**同一按钮内用逗号、按钮之间用分号**。

**非 ASCII 头值以 UTF-8 字节发出**：中文标题与按钮 label 是常态，而 `httpx` 对 `str`
类型的头值强制 ASCII 编码（`UnicodeEncodeError`），`bytes` 则原样透传。ntfy 接受
UTF-8 字节的头值（spec §7.2.6 的实测就是中文标题）。若某次升级后服务端拒收，
再退到 RFC 2047（`=?UTF-8?B?…?=`）——那时要连带把按钮 label 一起编码。

`http` 默认值必须放在函数体里求值：写成 `http=httpx.Client(...)` 会在 import 期只建一次，
三个进程内多个实例共享同一个 client，测试也无法注入 `MockTransport`。
"""
from __future__ import annotations

import httpx

from notify.templates import Message, NtfyAction


def _action(value: NtfyAction) -> str:
    """一个按钮 → `key=value,key=value`。标签里的逗号/分号会拆坏格式，先替换掉。"""
    label = value.label.replace(",", "，").replace(";", "；")
    parts = [f"action={value.action}", f"label={label}", f"url={value.url}"]
    if value.method:
        parts.append(f"method={value.method}")
    if value.clear:
        parts.append("clear=true")
    return ",".join(parts)


def _header_value(text: str):
    """含非 ASCII 时返回 UTF-8 字节（httpx 对 str 头值强制 ascii 会直接抛错）。"""
    return text.encode("utf-8") if any(ord(ch) > 127 for ch in text) else text


def actions_header(actions: list[NtfyAction]) -> str:
    return ";".join(_action(a) for a in actions)


class NtfyClient:
    def __init__(self, base_url: str, topic: str, token: str | None = None, http=None):
        self.base_url = base_url.rstrip("/")
        self.topic = topic
        self.token = token
        self.http = http or httpx.Client(trust_env=False, timeout=10.0)

    def publish(self, m: Message) -> str:
        """发一条通知，返回服务端 `id`；非 2xx 抛 `httpx.HTTPStatusError`（由 Dispatcher 计失败）。"""
        headers = {"X-Title": _header_value(m.title), "X-Priority": str(m.priority),
                   "X-Markdown": "true"}
        if m.tags:
            headers["X-Tags"] = ",".join(m.tags)
        if m.click:
            headers["X-Click"] = m.click
        if m.actions:
            headers["X-Actions"] = _header_value(actions_header(m.actions))
        if m.seq_id:
            headers["X-Sequence-ID"] = m.seq_id
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        resp = self.http.post(f"{self.base_url}/{self.topic}", content=m.body.encode(), headers=headers)
        resp.raise_for_status()
        try:
            return str(resp.json().get("id", ""))
        except ValueError:
            return ""
