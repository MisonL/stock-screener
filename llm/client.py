"""LLM 单通道客户端（§7.5.1、§7.5.6）。

四条约束：

1. **SDK 自带重试必须关掉**（`max_retries=0`）：它和我们自己的 3 次相乘，一次 502 会变成 9 个请求。
2. **`thinking` 注入**：值来自 `config/llm.yaml::llm.thinking`（v0.2.105 接线），
   缺省 `disabled`。漏注入会先烧推理
   token，`max_tokens` 给小还会返回空正文 + `finish_reason=length`——与「空 body 可重试」
   的判据叠加就成了无限重试。
3. **`base_url` / `api_key` 允许环境变量覆盖**：故障演练用 `LLM_BASE_URL=http://blackhole:1`
   指黑洞；没有这个读取方，演练参数传了也没人用。
4. **分类先看 `tool_calls` 再看正文**：带工具调用的空 content 是**成功**（模型把话放在工具里了）。
"""
from __future__ import annotations

import json
import os
import random
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from core.http import direct_http as _direct_http

# **`openai` 不在顶层导入**：它自己一条 `import` 要 10 秒（大量 pydantic 模型：
# openai.types.* 上百个模块）。`agent.main()` 启动路径上就会构造 `LlmClient`，
# 这 10 秒全算进容器启动——实测 `agent.main()` 到写第一个心跳要 21.9 秒，
# 而进程级冒烟测试只等 20 秒。改成首次真正调用模型时再导入。
# 异常判定也因此改成**鸭子类型**（看 `status_code` 属性），不再 isinstance。
OPENAI_IMPORT_NOTE = "延迟导入，见文件顶部说明"

Verdict = Literal["retry", "double_tokens", "fatal", "ok"]

#: 兜底硬上限（秒）。配置写 `0`（不设业务上限）时用它。
#: 只在"上游彻底不回包"时才会撞上——实测上游最慢 31 秒，300 秒是 10 倍余量，
#: 正常调用绝无可能撞上。**绝不能用 `None`**：那是"永远等"，会把回合永久卡住。
#: 也不能太小：那等于变相恢复了固定超时，又回到"慢但正常"被判死的坑里。
SAFETY_TIMEOUT_S = 300.0

_RETRY_STATUS = (502, 503, 504, 0)
_FATAL_STATUS = (400, 401, 403, 404)
TOOL_MIN_MAX_TOKENS = 2000       # 工具路径下限：低于它会稳定截断 emit_decision 的 JSON


class RetriesExhausted(Exception):
    """重试用尽。`usage` 是**所有已返回尝试**累计的用量——失败的那几次也花了钱。"""

    def __init__(self, last_error: Any, attempts: int, usage=None):
        super().__init__(f"重试 {attempts} 次仍失败: {last_error}")
        self.last_error = last_error
        self.attempts = attempts
        self.usage = usage


class NonRetryable(Exception):
    def __init__(self, status: int, payload: Any = None):
        super().__init__(f"不可重试的状态码 {status}")
        self.status = status
        self.payload = payload


@dataclass
class LlmResult:
    """一次成功的 chat 调用。

    **`content` / `tool_calls` 是 `message` 的便捷视图**：真实 SDK 把两者放在
    `choice.message` 里，而调用方（agent 回合、网页对话）历史上直接读 `result.content`。
    没有这两个属性时，生产路径每一次正常回复都会 `AttributeError`——单测里用假对象
    （自带 `content`）永远发现不了。
    """

    message: Any
    usage: Any
    attempts: int
    latency_ms: int
    finish_reason: str | None = None

    @property
    def content(self) -> str | None:
        return getattr(self.message, "content", None)

    @property
    def tool_calls(self) -> list:
        return list(getattr(self.message, "tool_calls", None) or [])


@dataclass
class Usage:
    """累计用量（SDK 的 usage 对象字段名一一对应）。

    `unknown` 为真时表示「至少一次尝试 SDK 没给 usage」。这时**不能把用量当 0**——
    真实中转站会返回 `usage: null`，记 0 等于白送一次调用、日预算永不增长。
    计费侧看到 `unknown` 会按本次请求的 `max_tokens` 等保守量兜底。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    unknown: bool = False

    def add(self, usage) -> "Usage":
        if usage is None:
            self.unknown = True
            return self
        self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self


@dataclass
class _Attempt:
    """一次调用的中间结果：用于分类，不暴露给调用方。"""

    message: Any = None
    usage: Any = None
    finish_reason: str | None = None
    error: Any = None
    status: int | None = None
    empty_body: bool = False
    payload: Any = None


def _truncated_tool_args(message: Any, finish_reason: str | None) -> bool:
    """工具参数是不是被截断的 JSON。

    **只在 `finish_reason == "length"` 时才算截断**：模型自己回了个非法 JSON（finish=stop）
    是另一回事，翻倍 token 也救不回来，应当按成功返回让上层处理。
    空串与非法 JSON 同等处理——`arguments: ""` 是缺参数，不是「无需参数」。
    """
    if finish_reason != "length":
        return False
    for call in getattr(message, "tool_calls", None) or []:
        args = getattr(getattr(call, "function", None), "arguments", None)
        if args is None:
            continue
        try:
            json.loads(args)
        except (ValueError, TypeError):
            return True
    return False


def _status_verdict(status: int, retry_on=(), fatal_status=()) -> str:
    """状态码 → `"retry"` / `"fatal"`。**两个配置键都必须真的生效**。

    判定顺序（顺序本身是语义）：
      0. **2xx/3xx 一律"不是最终判决"** —— `_Attempt.status` 正常响应也是 200，
         它的成败要看正文与 `finish_reason`，不能在这里提前定性。
         漏了这一步会让所有正常响应被判成 fatal（实测：翻倍重试直接不触发）。
      1. `never_retry` 里的一律 fatal —— 显式放弃优先
      2. `retry_on` 里的一律 retry —— 显式重试
      3. 其余按默认：5xx 与 0（连接失败）重试，其余 4xx 放弃

    以前这里是写死的 `_FATAL_STATUS`，`retry_on` **没有任何读取点**、
    `never_retry` 也没接线——两个键都写在 YAML 里，改了什么都不会发生。
    """
    if 200 <= status < 400:
        return "ok"                     # 交给上层看正文 / finish_reason
    if status in tuple(fatal_status or ()):
        return "fatal"
    if status in tuple(retry_on or ()):
        return "retry"
    return "retry" if (status == 0 or status >= 500) else "fatal"


def classify(src: _Attempt | Exception, fatal_status=None, retry_on=None) -> Verdict:
    """§7.5.6 的判空顺序：**先看 tool_calls**，再看 `finish_reason` 与正文。

    `retry_on` / `fatal_status` 由配置 `retry.retry_on` / `retry.never_retry`
    传入，见 `_status_verdict()`。
    """
    if isinstance(src, Exception):
        if isinstance(src, NonRetryable):
            return "fatal"
        # **鸭子类型，不 isinstance openai 的异常类**：SDK 若未导入，那些类就不存在；
        # 而 `status_code` 是 openai 各类 API 异常的公共属性（连接/超时错误没有，
        # 落到下面的 retry 分支——与原先 isinstance 判定的结论一致）。
        status = getattr(src, "status_code", None)
        if status is not None:
            return _status_verdict(int(status), retry_on, fatal_status)
        return "retry"

    if _status_verdict(int(src.status or 0), retry_on, fatal_status) == "fatal":
        return "fatal"
    # 2xx/3xx 落到下面按正文与 finish_reason 判断（`_status_verdict` 返回 "ok"）
    if src.empty_body:
        return "retry"
    message = src.message
    if getattr(message, "tool_calls", None):
        # 有工具调用就是成功，哪怕 content 是空的；只有被 max_tokens 截断才谈翻倍
        return "double_tokens" if _truncated_tool_args(message, src.finish_reason) else "ok"
    if src.finish_reason == "length":
        return "double_tokens"          # 正文被 max_tokens 截断：加预算再来
    if not (getattr(message, "content", None) or "").strip():
        return "retry"
    return "ok"


class LlmClient:
    def __init__(self, cfg, http_client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        # **空串必须当成"没设"**（2026-11-27）：原来写
        # `os.environ.get("LLM_BASE_URL", cfg.llm.base_url)`——
        # 只要变量**存在**（哪怕是空串）就返回它，于是 `.env` 里留一行
        # `LLM_BASE_URL=` 会把 base_url 变成空串，所有请求打到相对地址上。
        # `or` 才算"没给值就用 yaml"。
        base_url = (os.environ.get("LLM_BASE_URL") or "").strip() \
            or cfg.llm.base_url
        api_key = os.environ.get("LLM_API_KEY") or os.environ.get(
            str(cfg.llm.api_key_env), "") or "not-needed"
        # **`LLM_MODEL` 原来完全没被读**（2026-11-27 发现）：站点 `.env` 里一直
        # 写着 `LLM_MODEL=`，改它却毫无效果，只有 `config/llm.yaml` 生效——
        # 排查时会以为"已经换了模型"而实际还在用旧的。三个变量（key/base_url/
        # model）现在都按同一套语义解析：空串等于没设，回退到 yaml。
        self.model = (os.environ.get("LLM_MODEL") or "").strip() or cfg.llm.model
        self._owns_http = http_client is None
        self._base_url = base_url
        self._api_key = api_key
        self._http_client = http_client
        self._client = None                   # 惰性：首次调用才付 openai 的导入成本

    @property
    def client(self):
        """惰性构造 `AsyncOpenAI`（首次访问时才 `import openai`，见文件顶部说明）。"""
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
                max_retries=0,                  # 我们的重试次数已经算过账，SDK 别再乘一次
                # 单次调用的超时由 `default_timeout()` 每次决定（配置 0 时用
                # `SAFETY_TIMEOUT_S` 兜底）。这里给 `None` 是为了**不让 client
                # 级默认值盖掉每次请求传的值**；真正生效的是请求上的 timeout。
                http_client=self._http_client or httpx.AsyncClient(trust_env=not _direct_http(), timeout=None),
            )
        return self._client

    async def aclose(self) -> None:
        """自己建的连接自己关；调用方传进来的 httpx 实例归调用方管。"""
        # 没建过 client 就没有连接要关（不能走 property，否则会白导入 openai）
        if self._owns_http and self._client is not None:
            await self._client.close()

    async def __aenter__(self) -> "LlmClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def _thinking_type(self) -> str:
        """`llm.thinking` 的取值（`disabled` / `enabled`）。

        **v0.2.105 之前这个键全仓零读取**，`extra_body` 注入的是字面量
        `"disabled"`——改 YAML 没有任何变化，而这里的注释还教人"要开时
        max_tokens 须 ≥2000"，等于指着一个改不动的开关。

        兼容两种配置写法：字符串 `disabled`，或带 `.value` 的枚举对象。
        """
        val = getattr(getattr(self.cfg, "llm", None), "thinking", None)
        val = getattr(val, "value", val)
        return str(val) if val else "disabled"

    def default_max_tokens(self, tools: list[dict] | None) -> int:
        if tools:
            return max(TOOL_MIN_MAX_TOKENS, int(self.cfg.llm.max_tokens.tools))
        return int(self.cfg.llm.max_tokens.plain)

    def default_timeout(self, timeout_s: float | None, tools: list[dict] | None) -> float:
        """单次调用的超时（秒）。配置写 `0` 表示**不设业务上限**。

        默认不设业务上限是一次生产故障换来的：上游网关排队时单次调用要 23–31
        秒，任何固定值迟早会被正常调用超过去，然后每一次 attempt 都超时、
        整个交易日报废（实盘：一天 21/21 个回合 timeout）。

        但**绝不返回 `None`**：`None` 一路传到 SDK 就是"永远等"，上游不回包时
        回合永久卡死、心跳跟着停摆。所以放开业务上限后仍套一层 `SAFETY_TIMEOUT_S`
        兜底——只有上游彻底挂住才会撞上，正常回合几十秒就回来了。
        """
        raw = timeout_s if timeout_s is not None else float(
            self.cfg.llm.timeouts.chat_s if not tools else self.cfg.llm.timeouts.agent_call_s)
        return SAFETY_TIMEOUT_S if raw <= 0 else float(raw)

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   max_tokens: int | None = None, timeout_s: float | None = None,
                   deduct: Any = None, est_in: int = 0,
                   on_attempt: Any = None) -> LlmResult:
        """一次完整调用（含截断翻倍与重试）。

        **每次先扣额度、扣了不退**：`deduct(est_in, max_tokens)` 在每条网络请求
        发出前调用，成功才发。调用结束后**没有任何退款路径**——这是硬上限的全部
        实现。以前那套「预留 + 对账」要求结束时把估计换成实际，于是出现三类补不
        干净的漏洞（第九轮实测）：部分 attempt 未知与已知相抵、在途取消按零 usage
        退款、跨池共享故障标记被无关成功清除。保守扣减让"花超"从机制上不可能。

        代价：估算误差、失败响应、未知 usage 都会保守消耗额度，可能提前耗尽。

        `on_attempt(attempt_seq, usage, unknown, est_in, est_out)` 在每次 attempt
        结束后回调，用来写真实用量审计（不参与预算）。
        """
        attempts = 0
        last_error: Any = None
        budget = int(max_tokens or self.default_max_tokens(tools))
        timeout = self.default_timeout(timeout_s, tools)
        total = Usage()
        doubled = 0                          # 截断只翻倍一次：第二次仍截断就降级
        started = time.monotonic()
        limit = int(self.cfg.retry.max_attempts)
        est = int(est_in or _rough_in(messages))

        while attempts < limit:
            attempts += 1
            # **每条网络请求前都扣一次**（含翻倍重试）：只在进入 chat 前查一次的话，
            # 截断→max_tokens 翻倍→重试 这条路径完全绕过预算。
            if deduct is not None and not deduct(est, budget):
                raise BudgetExceeded("预算不足以再发一次请求")
            try:
                attempt = await self._once(messages, tools, budget, timeout)
            except NonRetryable:
                raise
            except GeneratorExit:
                raise                        # 额度已扣，无需携出任何东西
            except asyncio.CancelledError:
                raise                        # 同上
            except Exception as exc:            # noqa: BLE001 —— 分类后再决定
                attempt = _Attempt(error=exc, status=getattr(exc, "status_code", None))

            unknown_attempt = attempt.usage is None
            total.add(attempt.usage)            # 累计仅供日志/审计
            if on_attempt is not None:
                try:
                    on_attempt(attempts, attempt.usage, unknown_attempt, est, budget)
                except Exception:               # noqa: BLE001 —— 审计失败不影响调用
                    pass

            verdict = classify(attempt, fatal_status=self.cfg.retry.never_retry,
                               retry_on=self.cfg.retry.retry_on)
            if verdict == "ok":
                return LlmResult(message=attempt.message, usage=total, attempts=attempts,
                                 latency_ms=int((time.monotonic() - started) * 1000),
                                 finish_reason=attempt.finish_reason)
            if verdict == "fatal":
                raise NonRetryable(int(attempt.status or 0), attempt.payload)

            last_error = attempt.error or f"finish_reason={attempt.finish_reason}"
            if verdict == "double_tokens":
                if doubled >= 1:
                    # 已经翻倍过一次还截断：降级（上层按模板兜底），别再翻成 8000
                    raise RetriesExhausted(last_error, attempts, usage=total)
                doubled += 1
                budget *= 2
            if attempts >= limit:
                break
            await self._sleep(attempts)         # 退避期取消/GeneratorExit 自然上抛

        raise RetriesExhausted(last_error, attempts, usage=total)


    async def _once(self, messages, tools, max_tokens, timeout_s) -> _Attempt:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            # 唯一注入点：漏了它就会先烧推理 token，还会让 max_tokens 小的请求返回空正文
            # **`llm.thinking` 配置真的生效**（v0.2.105）：审计发现这个键全仓零读取，
            # 注入的是**字面量** "disabled"，改 YAML 不会有任何变化。
            "extra_body": {"thinking": {"type": self._thinking_type()}},
        }
        if tools:
            body["tools"] = tools
        # **`None` 才是"不设超时"**：传 `timeout=None` 时 SDK 会保留
        # `client.timeout`（我们建 client 时也给了 None），于是整条链路无上限。
        resp = await self.client.chat.completions.create(
            **body, timeout=float(timeout_s))
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return _Attempt(error="空 choices", status=200, empty_body=True,
                            usage=getattr(resp, "usage", None))
        choice = choices[0]
        return _Attempt(message=choice.message, usage=getattr(resp, "usage", None),
                        finish_reason=getattr(choice, "finish_reason", None))

    def _backoff_base(self, attempt: int) -> float:
        table = list(self.cfg.retry.backoff) or [0.0]
        return float(table[min(attempt - 1, len(table) - 1)])

    def backoff_delay(self, attempt: int, rng: random.Random | None = None) -> float:
        """第 `attempt` 次重试前等多久：基值 ±30% 抖动（0.5 → [0.35, 0.65]）。"""
        rng = rng or random
        base = self._backoff_base(attempt)
        return base * rng.uniform(0.7, 1.3)

    async def _sleep(self, attempt: int) -> None:
        import asyncio

        await asyncio.sleep(self.backoff_delay(attempt))


def _rough_in(messages: list[dict]) -> int:
    """输入 token 粗估（正文 + 历史 tool_calls；中文约 1 字 ≈ 1 token，留 1.2 倍）。"""
    chars = 0
    for m in messages:
        chars += len(str(m.get("content") or ""))
        for c in m.get("tool_calls") or []:
            chars += len(str(c))
    return int(chars * 1.2) + 200


class BudgetExceeded(Exception):
    """预算不足以再发一次网络请求（由 `reserve` 回调判定）。"""

    def __init__(self, msg: str = "预算已用完"):
        super().__init__(msg)
        self.usage = None
        self.reserved_in = 0
        self.reserved_out = 0
