"""**禁止在测试里手设"会由生产路径推导出来的内部状态"**（v0.2.102）。

## 为什么要这条机械检查

今天 7 次假绿里 **5 次**是同一个形状：

    在测试里直接给对象的内部字段赋值，跳过生产路径，
    于是测试验证的是"我设的那个值"，不是"代码算出来的值"。

| 次 | 假绿 | 手设了什么 |
|---|---|---|
| 3 | 事件重试只验了心跳那条防线 | — |
| 4 | S-a 绕过 `deduct()` 的短路 | 直接调 `_try_deduct` |
| 5 | `..._self_heals_through_the_production_entry` | 只覆盖 `Budget` 内部 |
| 6 | `..._follows_the_db_flag` | 第二个 `_once()` 没跑回合 |
| 7 | `..._skips_event_poll_entirely` | `main._quota_exhausted = True` |

第 7 次尤其说明问题：写它的**前一小时我还在批判同一件事**，还是犯了。

这不是"下次注意"能防住的。凡是"生产路径会算出来的状态"，测试就只能从
**生产路径的输入**（数据库、配置、真实调用）出发去影响它，不能直接赋值。

## 这条检查做什么

扫描 `tests/` 下所有 `.py`，出现下面这些**赋值**就报错：

    <obj>.budget_exhausted = ...
    <obj>._quota_exhausted = ...
    <obj>._budget_fail_streak = ...
    <obj>._budget_cancels = ...
    <obj>._rounds_cancels = ...

允许的是**读**（`assert main.budget_exhausted is True`）与**比较**。

## 边界

这条检查只管"已知会由生产路径推导的状态"。新增同类字段时，把它加进
`FORBIDDEN` —— 加进去的成本很低，漏掉的成本是又一个假绿。
"""
from __future__ import annotations

import ast
from pathlib import Path

#: 生产路径会推导出来的状态字段。测试只能从库/配置出发，不能直接赋值。
FORBIDDEN = (
    "budget_exhausted",
    "_quota_exhausted",
    "_budget_fail_streak",
    "_budget_cancels",
    "_rounds_cancels",
)


def _assignments(tree: ast.AST):
    """遍历 AST 找出对 `*.属性` 的真正赋值（不含注释/文档字符串/比较）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr in FORBIDDEN:
                yield node.lineno, t.attr


def test_no_test_assigns_derived_internal_state():
    root = Path(__file__).resolve().parent
    offenders: list[str] = []

    for path in sorted(root.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue                       # 本文件里引用了这些名字作为规则
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        for lineno, attr in _assignments(ast.parse(src)):
            code = lines[lineno - 1].strip() if lineno <= len(lines) else ""
            offenders.append(f"{path.relative_to(root.parent)}:{lineno}: {code}")

    assert not offenders, (
        "测试里直接给「生产路径会推导出来的状态」赋值——这正是今天 5 次假绿的形状。\n"
        "请改成从**生产路径的输入**出发（例如把 `llm_budget` 写到用完，"
        "让 `_budget_exhausted_now()` 自己算出 True）。\n\n"
        + "\n".join(offenders))


def test_mkdtemp_is_redirected_into_pytest_tmp(tmp_path):
    """**`tempfile.mkdtemp()` 必须落在 `tmp_path` 里**（v0.2.108）。

    2026-11-21：本机长期测试容器 `ss-dev-run` 的可写层涨到 230 GB，撑爆
    Docker 虚拟磁盘，PostgreSQL 起不来。`docker diff` 在它的 `/tmp` 下统计到
    约 33 万个变更路径——`tests/chat/test_session.py` 12 处、
    `tests/jobs/test_main_scheduler.py` 1 处直接 `tempfile.mkdtemp()`，
    **从不清理**，每次跑测试都留十几个目录和 SQLite 文件。

    `tests/conftest.py` 的 autouse fixture 把 `mkdtemp` 重定向到 `tmp_path`。
    这条测试钉住那个 fixture：删掉它、或改成不重定向，这里就必须红。
    """
    import tempfile
    from pathlib import Path

    made = Path(tempfile.mkdtemp())
    assert made.exists(), "mkdtemp 没建出目录"
    assert tmp_path in made.parents or made.parent == tmp_path, (
        f"mkdtemp 建到了 {made}，不在本用例的 tmp_path {tmp_path} 下——"
        "conftest 的重定向 fixture 失效了，临时目录会跨用例泄漏")
