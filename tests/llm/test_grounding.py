"""batch2 Task 5：出处标记抽取与服务端回核。"""
import inspect

import pytest

from llm import grounding

c = lambda s: grounding.extract(s)[0]          # noqa: E731
extract, check = grounding.extract, grounding.check

RET = {"get_pattern": [{"data": {"box_high": 33.10}}],
       "get_quote": [{"data": {"price": 32.94}}]}


def test_display_precision():
    assert not check(c("32.9（get_quote.data.price）"), {"get_quote": [{"data": {"price": 32.90}}]})


def test_precision_mismatch():
    assert check(c("32.9（get_quote.data.price）"), {"get_quote": [{"data": {"price": 32.94}}]})[0].reason == "value_differs"


def test_truncation_style_mismatch():
    """32.99 展示两位对 33.00：等长截断会误判一致，相对误差不会。"""
    bad = check(c("32.99（get_quote.data.price）"), {"get_quote": [{"data": {"price": 33.00}}]})
    assert bad and bad[0].reason == "value_differs"


def test_array_path():
    assert not check(c("11.2（get_kline.data.0.close）"), {"get_kline": [{"data": [{"close": 11.2}]}]})


def test_missing_path_fails():
    assert check(c("5（get_pattern.data.box_mid）"), {"get_pattern": [{"data": {}}]})[0].reason == "path_missing"


def test_computed_expression():
    assert not check(c("0.16（计算: get_pattern.data.box_high - get_quote.data.price）"), RET)


def test_computed_expression_uses_cjk_operators():
    assert not check(c("2（计算: 1 × 2）"), RET)


def test_expression_rejects_arbitrary_code():
    """AST 白名单：函数调用、下标越界以外的危险节点一律拒绝（不用 eval）。"""
    bad = check(c("1（计算: __import__('os').system('echo hi')）"), RET)
    assert bad and bad[0].reason == "path_missing"


def test_unmarked_number_flagged():
    assert extract("今天涨了 3.2%。")[1] == ["今天涨了 3.2%。"]


def test_marker_noise_not_flagged():
    """出处标记、6 位代码、日期、时刻都不能被当成「未溯源数字」。"""
    text = "9:35 的 600519 现价 32.90（get_quote.data.price），池子日期 20261030。"
    _, ungrounded = extract(text)
    assert ungrounded == []


def test_negative_and_percent_extracted():
    claims = c("跌了 -3.2%（get_quote.data.chg）")
    assert claims and claims[0].value == -3.2


def test_ctx_counts_as_tool():
    assert not check(c("10.06（ctx.event.price）"), {"ctx": [{"event": {"price": 10.06}}]})


def test_no_eval():
    src = inspect.getsource(grounding).replace("literal_eval", "literal")
    assert "eval(" not in src


def test_zero_actual_participates():
    """actual=0.0 是合法值，照常比；不能因为 0 就判 path_missing。"""
    bad = check(c("1（get_quote.data.price）"), {"get_quote": [{"data": {"price": 0.0}}]})
    assert bad and bad[0].reason == "value_differs"


async def test_enforce_rewrites_once():
    calls = []

    async def rewrite(text, bad):
        calls.append(len(bad))
        return "32.94（get_quote.data.price）"

    out = await grounding.enforce("32.9（get_quote.data.price）",
                                  {"get_quote": [{"data": {"price": 32.94}}]}, rewrite)
    assert calls == [1] and out.failed is False and "32.94" in out.text


async def test_enforce_falls_back_after_second_mismatch():
    async def rewrite(text, bad):
        return text                                    # 重写无效

    out = await grounding.enforce("32.9（get_quote.data.price）",
                                  {"get_quote": [{"data": {"price": 32.94}}]}, rewrite)
    assert out.failed is True and "数据校验失败，请查网页" in out.text


async def test_enforce_ok_passes_through_without_rewrite():
    called = []

    async def rewrite(text, bad):
        called.append(1)
        return text

    out = await grounding.enforce("32.94（get_quote.data.price）",
                                  {"get_quote": [{"data": {"price": 32.94}}]}, rewrite)
    assert called == [] and out.failed is False


async def test_enforce_is_async():
    assert inspect.iscoroutinefunction(grounding.enforce)

def test_uses_this_turn_last_return():
    """口径：取该工具**本轮最近一次**返回（模型引用的是最新数据）。"""
    returns = {"get_quote": [{"data": {"price": 1.0}}, {"data": {"price": 2.0}}]}
    assert not check(c("2（get_quote.data.price）"), returns)
    assert check(c("1（get_quote.data.price）"), returns)


def test_kline_real_path_is_data_bars():
    """spec 与实现必须同口径：`get_kline` 的 data 是对象，K 线在 data.bars。"""
    returns = {"get_kline": [{"data": {"code": "sh600519", "adjust": "raw",
                                       "bars": [{"close": 11.2}]}}]}
    assert not check(c("11.2（get_kline.data.bars.0.close）"), returns)
    assert check(c("11.2（get_kline.data.0.close）"), returns)[0].reason == "path_missing"
