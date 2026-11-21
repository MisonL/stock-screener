"""静态门禁：禁止未定义的名字（pyflakes）。

为什么要有这条：v0.2.114部署后 jobs容器进入重启循环——
`main()`里写了 `data_dir=data_dir,`，而 `data_dir`在该作用域不存在，
抛 `NameError`。**1349条测试全绿**，因为没有任何测试调用 `main()`
（它是进程入口，测试只覆盖它下面的函数）。

这类「入口函数里名字写错」的问题，静态检查一眼就能抓到，
比「祈祷有人跑一次进程」可靠得多。
"""
from __future__ import annotations

from pathlib import Path

import pyflakes.api
import pyflakes.reporter

ROOT = Path(__file__).resolve().parent.parent

#:只守「未定义名字」这一类（F821）。全量 pyflakes还会报未使用变量/导入，
#:那些不是这道门禁的目标。
UNDEFINED = "undefined name"

SOURCE_DIRS = ("jobs", "api", "agent", "core", "notify", "chat", "scripts")


def test_no_undefined_names_in_source():
    """源码里不允许出现未定义的名字——那会在部署后变成 NameError。"""

    class _Reporter(pyflakes.reporter.Reporter):
        def __init__(self):
            self.hits = []
            super().__init__(open("/dev/null", "w"), open("/dev/null", "w"))

        def flake(self, message):
            if UNDEFINED in str(message):
                self.hits.append(str(message))

    rep = _Reporter()
    targets = [p for d in SOURCE_DIRS for p in (ROOT / d).rglob("*.py")
        if "__pycache__" not in p.parts]
    assert targets, "没扫到任何源文件，路径写错了"
    for p in targets:
        pyflakes.api.checkPath(str(p), rep)

    assert not rep.hits, (
        "源码里有未定义的名字（部署后会 NameError）：\n"
        + "\n".join(rep.hits[:20]))
