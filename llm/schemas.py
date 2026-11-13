"""**轻量 schema 与信封**：不依赖策略栈。

为什么单独成文件：`mcp/westock.py` 只需要 `MCP_TOOL_SCHEMAS` 和 `envelope` 两个名字，
但原先从 `llm/tools.py` 导入——那条链会连带 `core.strategy.*` → `talib` / `pandas` / `numpy`。
实测 `import mcp.westock` 因此要 4.8 秒；`agent.main()` 启动时先构造 `WestockMcp`，
把这段耗时全算在启动路径上，导致进程级冒烟测试（等 20 秒心跳）在负载高时失败。

本模块只做纯数据结构，import 成本近乎为零。`llm/tools.py` 从这里再导出，保持兼容。
"""

from __future__ import annotations


def _opt(t: str) -> dict:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


def _fn(name: str, description: str, properties: dict[str, dict] | None = None) -> dict:
    props = properties or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(props.keys()),   # strict 模式：全部属性必须出现在 required
                "additionalProperties": False,
            },
            "strict": True,
        },
    }


_CODE = {"type": "string", "description": "证券代码，如 sh600519"}
_DATE_OPT = _opt("integer")

# 6 个 westock-mcp 封装（D22）：读 2 写 4；写工具 description 必须标「需用户确认」
MCP_TOOL_SCHEMAS = [
    _fn("mcp_read_watchlist", "读用户手机腾讯自选股里的自选股（只读，可自动调）",
        {"group": _opt("string"), "market": _opt("string")}),
    _fn("mcp_read_alerts", "读已设的股价提醒（只读，可自动调）"),
    _fn("mcp_add_watch", "需用户确认：加自选股（同步到手机 App）；模型只能发起，须经确认卡执行",
        {"code": _CODE, "note": _opt("string")}),
    _fn("mcp_remove_watch", "需用户确认：移除自选股（软删除）；模型只能发起，须经确认卡执行",
        {"code": _CODE}),
    _fn("mcp_set_alert", "需用户确认：设「涨到/跌到/涨跌幅超 N%」提醒；模型只能发起，须经确认卡执行",
        {"code": _CODE, "kind": {"type": "string", "description": "up / down / change"},
         "threshold": {"type": "number"}}),
    _fn("mcp_cancel_alert", "需用户确认：取消某类提醒；模型只能发起，须经确认卡执行",
        {"code": _CODE, "kind": {"type": "string", "description": "up / down / change"}}),
]

def envelope(data=None, *, ok: bool = True, reason: str | None = None, as_of: str, adjust: str | None = None) -> dict:
    """统一信封：五个键永远都在（§5.4.5 / §7.5.2 第 3、5 条）。`as_of` 必传。"""
    return {"ok": bool(ok), "data": data, "as_of": as_of, "reason": reason, "adjust": adjust}
