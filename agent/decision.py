"""`emit_decision` 与 `TurnDecision`（计划 Task 9，spec §5.4.4）。

三条「说反就完了」的防护：

1. **`direction` 由事件 kind 覆盖**（决定 B7）：模型正文写「放量突破」，而这一轮是
   `breakdown` 唤醒的，方向必须以事件为准。方向词是结构化字段，不是模型的措辞。
2. **沉默必须带理由**：`speak=False` 而 `why_silent` 为空 → 整条决定按 `invalid` 处理。
3. **`arguments` 空/坏 JSON 是 `invalid`，不是「模型拒答」**：思考模式下空正文 +
   `finish_reason=length` 同样走 `invalid`，交给 Task 2 的重试分类（翻倍 token 再试），
   而不是当成一次沉默收场。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

# `pending_action` 是**对象**（strict schema 保证结构），不是自由文本：
# 写工具只能从确认卡回调执行，参数必须在此时就结构化地抓下来。
EMIT_DECISION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "emit_decision",
        "description": "本轮回合的收场决定：说一句话，或者给一个沉默的理由。",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "speak": {"type": "boolean", "description": "是否开口说一句话"},
                "level": {"type": "string", "enum": ["P0", "P1", "P2"],
                          "description": "说话时的级别；沉默可空"},
                "direction": {"type": "string",
                              "enum": ["breakout", "breakdown", "none"],
                              "description": "方向（以结构化字段为准）"},
                "title": {"type": "string", "description": "标题；direction 为 none 时才用"},
                "body": {"type": "string", "description": "要说的话，数字须带出处标记"},
                "why_silent": {"type": "string", "description": "沉默的一句话理由；speak=false 必填"},
                "schedule_next": {"type": "integer", "description": "下次回访距今分钟数"},
                "schedule_note": {"type": "string", "description": "回访要看什么"},
                "evidence": {"type": "array", "items": {"type": "string"},
                             "description": "依据的工具路径"},
                "pending_action": {
                    "type": ["object", "null"],
                    "properties": {
                        "tool": {"type": "string", "description": "mcp_* 写工具名"},
                        "args": {"type": "object", "description": "该工具的参数"},
                    },
                    "required": ["tool", "args"],
                    "additionalProperties": False,
                    "description": "要用户确认的写操作；静默或已确认时为空",
                },
            },
            "required": ["speak", "body"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class TurnDecision:
    kind: Literal["speak", "silent", "invalid", "timeout", "error"] = "silent"
    speak: bool = False
    level: str | None = None
    direction: Literal["breakout", "breakdown", "none"] | None = None
    title: str | None = None
    body: str | None = None
    why_silent: str | None = None
    schedule_next: int | None = None
    schedule_note: str | None = None
    evidence: list[str] = field(default_factory=list)
    pending_action: dict | None = None
    why: str | None = None                      # invalid/timeout/error 的原因，落 agent_turn.why

    @classmethod
    def timeout(cls, text: str = "回合超时") -> "TurnDecision":
        return cls(kind="timeout", speak=False, why=text, why_silent=text)

    @classmethod
    def invalid(cls, text: str = "决定不可解析") -> "TurnDecision":
        return cls(kind="invalid", speak=False, why=text, why_silent=text)

    @classmethod
    def error(cls, msg: str) -> "TurnDecision":
        return cls(kind="error", speak=False, why=msg, why_silent=msg)


def title_for(d: TurnDecision, name: str) -> str:
    """标题跟着**方向**走，模型自己写的 `title` 只在 `none` 时用。

    方向词与标题必须一致：正文写「放量突破」而方向是 `breakdown`，标题也必须说「跌破」
    ——否则手机上看到的是自相矛盾的两句。
    """
    if d.direction == "breakout":
        return ("🚨 " if d.level == "P0" else "📈 ") + f"{name} 放量突破"
    if d.direction == "breakdown":
        return f"⚠️ {name} 跌破箱体下沿"
    return d.title or ""


def parse_decision(args_json: str, channel: str, event_kind: str | None) -> TurnDecision:
    """解析模型给出的 `emit_decision` 参数。

    `args_json` 为空串或坏 JSON → `TurnDecision.invalid`（**不是沉默**）：那通常是
    思考 token 吃光了输出预算，翻倍 `max_tokens` 再试很可能就好了。
    """
    if not args_json or not str(args_json).strip():
        return TurnDecision.invalid("emit_decision 参数为空（思考 token 可能占满了输出预算）")
    try:
        args = json.loads(args_json)
    except (TypeError, ValueError) as exc:
        return TurnDecision.invalid(f"emit_decision 参数不是 JSON：{exc}")
    if not isinstance(args, dict) or not isinstance(args.get("speak"), bool):
        return TurnDecision.invalid("emit_decision 参数缺少 speak 布尔值")

    pending = args.get("pending_action")
    if pending in (None, "", {}):
        pending = None
    elif not isinstance(pending, dict) or "tool" not in pending or "args" not in pending:
        return TurnDecision.invalid("pending_action 必须是 {tool, args} 或 null")
    elif not isinstance(pending["args"], dict):
        return TurnDecision.invalid("pending_action.args 必须是对象")

    body = args.get("body")
    speak = bool(args["speak"])
    if not speak and not args.get("why_silent"):
        return TurnDecision.invalid("沉默必须带理由（why_silent 为空）")
    if speak and not (isinstance(body, str) and body.strip()):
        return TurnDecision.invalid("开口却没有正文")

    direction = args.get("direction") or "none"
    if channel == "A" and event_kind in ("breakout", "breakdown"):
        # 决定 B7：通道 A 的方向以事件 kind 为准，模型给的覆盖掉
        direction = event_kind

    return TurnDecision(
        kind="speak" if speak else "silent",
        speak=speak,
        level=args.get("level"),
        direction=direction,
        title=args.get("title"),
        body=body if speak else None,
        why_silent=args.get("why_silent") if not speak else None,
        schedule_next=args.get("schedule_next"),
        schedule_note=args.get("schedule_note"),
        evidence=list(args.get("evidence") or []),
        pending_action=pending,
    )
