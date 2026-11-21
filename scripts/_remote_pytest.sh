#!/usr/bin/env bash
# 容器内的 pytest 入口（由 `scripts/test_remote.sh` 调用）。
#
# 为什么不直接在 `docker run ... pytest` 里写命令：那需要多层嵌套引号，
# 实测会把 `'*'` 和路径拆坏（`safe.directory: command not found`）。
# 放进文件里，引号只有一层。
#
# `safe.directory`：代码目录是从宿主机拷进来的，属主不是容器里的 root，
# git 会以 "dubious ownership" 拒绝，导致 release / deploy / version 那组
# 测试假红（实测 `git status ... exited 128`）。只在测试容器里放行。
set -euo pipefail

git config --global --add safe.directory '*' >/dev/null 2>&1 || true

exec pytest -q "$@"