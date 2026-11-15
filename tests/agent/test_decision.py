"""emit_decision 解析与「方向不可说反」。"""
import json

import pytest

from agent.decision import (EMIT_DECISION_TOOL, TurnDecision, parse_decision, title_for)


def _args(**kw):
    base = {"speak": True, "body": "放量突破（get_quote.data.price）", "direction": "none"}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def test_title_from_direction_not_body():
    """跌破事件、模型正文写「放量突破」、direction 给 breakout → 解析后必须是 breakdown。"""
    d = parse_decision(_args(direction="breakout", body="放量突破，突破箱体上沿",
                             title="放量突破"),
                       channel="A", event_kind="breakdown")
    assert d.direction == "breakdown"
    assert "跌破" in title_for(d, "贵州茅台")


def test_channel_b_keeps_model_direction():
    d = parse_decision(_args(direction="breakout"), channel="B", event_kind="index_move")
    assert d.direction == "breakout"


def test_silent_requires_reason():
    d = parse_decision(json.dumps({"speak": False, "body": ""}), channel="A",
                       event_kind="breakdown")
    assert d.kind == "invalid"


def test_silent_with_reason_is_ok():
    d = parse_decision(json.dumps({"speak": False, "why_silent": "已在 10 分钟前说过"},
                                  ensure_ascii=False),
                       channel="A", event_kind="breakdown")
    assert d.kind == "silent" and d.why_silent == "已在 10 分钟前说过"
    assert d.body is None


def test_empty_args_is_invalid_not_refusal():
    """空参数不是「模型拒答」：思考 token 吃满输出预算时就是这样，翻倍再试往往就好。"""
    for bad in ("", "   ", None):
        d = parse_decision(bad, channel="A", event_kind="breakdown")
        assert d.kind == "invalid" and d.speak is False


def test_broken_json_is_invalid():
    d = parse_decision('{"speak": tr', channel="A", event_kind="breakdown")
    assert d.kind == "invalid"


def test_speak_without_body_is_invalid():
    d = parse_decision(json.dumps({"speak": True, "body": "   "}), channel="A",
                       event_kind="breakdown")
    assert d.kind == "invalid"


def test_extra_keys_rejected_by_schema():
    """strict 模式下多给的键要能被 schema 拒掉（additionalProperties:false）。"""
    fn = EMIT_DECISION_TOOL["function"]
    assert fn["strict"] is True
    assert fn["parameters"]["additionalProperties"] is False
    # 所有属性都必须出现在 required 里（strict 模式的硬要求，漏了会被 API 拒）
    props = set(fn["parameters"]["properties"])
    assert set(fn["parameters"]["required"]) <= props


def test_pending_action_must_be_structured():
    ok = parse_decision(_args(pending_action={"tool": "mcp_set_alert",
                                              "args": {"code": "sh600519"}}),
                        channel="A", event_kind="breakout")
    assert ok.pending_action["tool"] == "mcp_set_alert"
    bad = parse_decision(_args(pending_action="设置提醒 sh600519"), channel="A",
                         event_kind="breakout")
    assert bad.kind == "invalid"


def test_pending_action_null_when_absent():
    d = parse_decision(_args(), channel="A", event_kind="breakout")
    assert d.pending_action is None


def test_title_breakout_and_p0():
    d = parse_decision(_args(direction="breakout", level="P0"), channel="A",
                       event_kind="breakout")
    assert title_for(d, "贵州茅台").startswith("🚨")
    d1 = parse_decision(_args(direction="breakout", level="P1"), channel="A",
                        event_kind="breakout")
    assert title_for(d1, "贵州茅台").startswith("📈")


def test_title_none_uses_model_title():
    d = parse_decision(_args(direction="none", title="自定义标题"), channel="B",
                       event_kind="index_move")
    assert title_for(d, "上证") == "自定义标题"


def test_failure_constructors():
    assert TurnDecision.timeout().kind == "timeout"
    assert TurnDecision.invalid("x").kind == "invalid"
    assert TurnDecision.error("boom").kind == "error"
    for d in (TurnDecision.timeout(), TurnDecision.invalid("x"), TurnDecision.error("y")):
        assert d.speak is False


def test_evidence_preserved():
    d = parse_decision(_args(evidence=["get_quote.data.price"]), channel="B", event_kind=None)
    assert d.evidence == ["get_quote.data.price"]
