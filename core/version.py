"""版本号的**唯一来源**。

为什么要有这个文件：

1. 版本文本原本散在 `pyproject.toml`、`package.json`、`web/package.json` 三处，
   谁都不读它——写多少都不影响运行中的程序，等于没有版本号。
2. 生产是 Docker 镜像，`importlib.metadata` 在镜像里不一定能拿到本项目元数据
   （项目没有以包形式安装进 site-packages），所以不能靠读 distribution 取版本。
3. 排障时需要能回答「线上跑的是哪个版本」。`/healthz` 与 `/api/status` 会带出
   `VERSION`，把「容器里到底是哪版代码」变成可查的事实。

`VERSION` 的格式是语义化版本 `MAJOR.MINOR.PATCH`（可选 `-预发布` 后缀），
与 git tag `v{VERSION}` 一一对应。改动它必须同时：
  - 同步三处包清单（由 `tests/test_version.py` 守卫，不一致直接红）
  - 在 `CHANGELOG.md` 里补对应小节
  - 打 tag（`make release` 会一并做）
"""

from __future__ import annotations

VERSION = "0.2.79"

__all__ = ["VERSION"]
