"""出处校验（§7.5.2）：模型说的每个数字都必须能在本轮工具返回里按路径查到。

四条容易做错的：

1. **比较用相对误差，不用展示小数位**：`32.9` 对存储 `32.90` 一致、对 `32.94` 不一致。
   拿「等长截断」当判据会把 `32.99`（两位）与 `33.00` 判成一致。
2. **路径从信封根算起**（含 `data.`）：`get_quote` 的信封是 `{"ok":…,"data":…}`，写
   `get_quote.price` 在根上找不到 `price`，会把所有正确引用判成错。
3. **表达式绝不用 `eval`**：模型输出是不受信输入，走 AST 白名单。
4. **「无出处数字」扫描要先剔除标记/代码/日期**：否则「9:35 的 600519 现价 32.90（…）」
   会报出三个假阳性。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

# 数字 + 出处标记。**必须支持负号与百分号**：模型会引用「-3.2%」。
_MARK = re.compile(r"(-?\d+(?:\.\d+)?)\s*%?\s*[（(]\s*(\w+)\.([\w.\[\]]+)\s*[）)]")
_CALC = re.compile(r"(-?\d+(?:\.\d+)?)\s*%?\s*[（(]\s*计算[:：]\s*([^）)]+)[）)]")

_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])")
# 剔除假阳性：出处标记整体、6 位代码、日期、时刻、百分比本身不单独算数字
_NOISE = (
    _MARK, _CALC,
    re.compile(r"\b\d{6}\b"),                       # 600519
    re.compile(r"\d{4}-\d{2}-\d{2}"),               # 2026-11-06
    re.compile(r"\b\d{8}\b"),                       # 20261030
    re.compile(r"\b\d{1,2}[:：]\d{2}\b"),           # 9:35
)
_ANY_NUM = re.compile(r"-?\d+(?:\.\d+)?")

_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.Constant, ast.Name, ast.Load,
                  ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Attribute, ast.Subscript)


@dataclass
class Claim:
    value: float
    shown_decimals: int
    tool: str
    path: str | None = None          # 直接引用
    expr: str | None = None          # 计算表达式


@dataclass
class Mismatch:
    sentence: str
    claim: Claim
    actual: float | None
    reason: Literal["path_missing", "value_differs"]


@dataclass
class GroundedText:
    text: str
    ungrounded: bool                 # 有数字没出处
    failed: bool                     # 有对不上的（重写一次后仍有）


def _decimals(raw: str) -> int:
    return len(raw.split(".", 1)[1]) if "." in raw else 0


def extract(text: str) -> tuple[list[Claim], list[str]]:
    claims: list[Claim] = []
    for m in _MARK.finditer(text or ""):
        claims.append(Claim(value=float(m.group(1)), shown_decimals=_decimals(m.group(1)),
                            tool=m.group(2), path=m.group(3)))
    for m in _CALC.finditer(text or ""):
        claims.append(Claim(value=float(m.group(1)), shown_decimals=_decimals(m.group(1)),
                            tool="", path=None, expr=m.group(2).strip()))

    ungrounded: list[str] = []
    for raw in _SENT_SPLIT.split(text or ""):
        sent = raw.strip()
        if not sent:
            continue
        stripped = sent
        for pat in _NOISE:
            stripped = pat.sub(" ", stripped)
        if _ANY_NUM.search(stripped):
            ungrounded.append(sent)
    return claims, ungrounded


# ---- 路径取值 -------------------------------------------------------------

def _walk(root: Any, path: str) -> Any:
    cur = root
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                raise KeyError(part)
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            cur = cur[int(part)]
        else:
            raise KeyError(part)
    return cur


def _resolve(claim: Claim, returns: dict[str, list[dict]]) -> Any:
    envs = returns.get(claim.tool) or []
    if not envs:
        raise KeyError("no_return")
    last = envs[-1]
    return _walk(last, claim.path or "")


# ---- 表达式（AST 白名单，绝不 eval） ---------------------------------------

def _eval_expr(expr: str, returns: dict[str, list[dict]]) -> float:
    src = expr.replace("×", "*").replace("÷", "/").replace("＋", "+").replace("－", "-")
    tree = ast.parse(src, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"不允许的表达式节点 {type(node).__name__}")
    def _val(n):
        if isinstance(n, ast.Expression):
            return _val(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.BinOp):
            a, b = _val(n.left), _val(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return a / b
            raise ValueError("不支持的运算符")
        if isinstance(n, ast.Attribute):
            return _walk_env(returns, _path_of(n))
        raise ValueError(f"不允许的表达式成分 {type(n).__name__}")
    return _val(tree)


def _walk_env(returns: dict, path: str) -> Any:
    """表达式里的 `get_quote.data.price`：先按工具名取**本轮最近一次**信封，再走剩余路径。"""
    tool, _, rest = path.partition(".")
    envs = returns.get(tool) or []
    if not envs:
        raise KeyError(tool)
    return _walk(envs[-1], rest) if rest else envs[-1]


def _path_of(node) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        raise ValueError("表达式路径必须以工具名开头")
    parts.append(node.id)
    return ".".join(reversed(parts))


# ---- 校验 -----------------------------------------------------------------

def check(claims: list[Claim], returns: dict[str, list[dict]]) -> list[Mismatch]:
    out: list[Mismatch] = []
    for c in claims:
        try:
            actual = _eval_expr(c.expr, returns) if c.expr else _resolve(c, returns)
        except (KeyError, IndexError, ValueError, TypeError, SyntaxError, ArithmeticError):
            out.append(Mismatch(sentence="", claim=c, actual=None, reason="path_missing"))
            continue
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            out.append(Mismatch(sentence="", claim=c, actual=None, reason="path_missing"))
            continue
        actual = float(actual)
        if abs(c.value - actual) / max(1e-6, abs(actual)) > 1e-6:
            out.append(Mismatch(sentence="", claim=c, actual=actual, reason="value_differs"))
    return out


def _with_sentences(text: str, mismatches: list[Mismatch]) -> list[Mismatch]:
    """给每条 Mismatch 补上原句：重写提示词要指名「哪句的哪个数对不上」。"""
    sentences = [s for s in _SENT_SPLIT.split(text or "") if s.strip()]
    for m in mismatches:
        needle = f"{m.claim.value:g}"
        m.sentence = next((s.strip() for s in sentences if needle in s), "")
    return mismatches


async def enforce(text: str, returns: dict[str, list[dict]],
                  rewrite: Callable[[str, list[Mismatch]], Awaitable[str]]) -> GroundedText:
    claims, ungrounded = extract(text)
    bad = _with_sentences(text, check(claims, returns))
    if not bad:
        return GroundedText(text=text, ungrounded=bool(ungrounded), failed=False)

    retried = await rewrite(text, bad)
    claims2, ungrounded2 = extract(retried)
    still = _with_sentences(retried, check(claims2, returns))
    if still:
        # 重写一次仍然对不上：把出问题的句子换成固定文案，别把错数字发出去
        out = retried
        for m in still:
            if m.sentence:
                out = out.replace(m.sentence, "数据校验失败，请查网页。")
        return GroundedText(text=out, ungrounded=bool(ungrounded2), failed=True)
    return GroundedText(text=retried, ungrounded=bool(ungrounded2), failed=False)
