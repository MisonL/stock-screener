#!/usr/bin/env bash
# 在 81.4 上跑测试，**本机不启动任何容器**。
#
# 为什么有这个脚本（2026-11-21 事故）：
#   本机的长期测试容器 `ss-dev-run`（`sleep infinity`，从不删除）可写层涨到
#   **230 GB**，撑爆 Docker Desktop 的虚拟磁盘，连带 PostgreSQL 起不来
#   （`could not write lock file "postmaster.pid": No space left on device`）。
#   它的 `/tmp` 下有 33 万个变更路径，主要来自测试里从不清理的 `tempfile.mkdtemp()`。
#
# 所以测试一律在远端跑，并且必须同时满足：
#   1. `--rm`：容器跑完即删，不残留；
#   2. `--tmpfs /tmp:size=2g`：临时文件进内存，**再怎么写也撑不爆宿主机磁盘**，
#      并随容器销毁全部消失；
#   3. 代码打成 tar 传过去解到独立目录，不碰远端工作区、也不依赖它是否干净。
#
# 用法：
#   scripts/test_remote.sh                      # 全量
#   scripts/test_remote.sh tests/agent/ -k foo  # 只跑一部分
set -euo pipefail

: "${SS_HOST:?请先 export SS_HOST=<测试机地址>（见 deploy/RUNBOOK.md §12）}"
HOST="$SS_HOST"
IMAGE="${SS_TEST_IMAGE:-ss-dev:latest}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TARBALL="$(mktemp -t ss-src-XXXXXX.tar)"
RUNNER="$(mktemp -t ss-run-XXXXXX.sh)"
trap 'rm -f "$TARBALL" "$RUNNER"' EXIT

# 只打包工作区（含未提交改动），排除 .git / node_modules / 各种缓存。
# **不用 `git archive`**：它会漏掉未提交的改动，而"测的就是当前代码"是硬要求。
# **必须带上 `.git`**：`tests/test_version.py`、`test_release_script.py`、
# `test_scan_secrets.py` 需要 git 历史与 tag，离线快照会让它们假红（实测 7 条）。
tar -cf "$TARBALL" \
    --exclude='node_modules' --exclude='__pycache__' \
    --exclude='.pytest_cache' --exclude='web/dist' --exclude='data' \
    --exclude='.venv' --exclude='*.pyc' \
    -C "$ROOT" .

ARGS="$*"
cat > "$RUNNER" <<EOF
set -euo pipefail
# **每次跑用独立目录**：容器以 root 写入挂载点，复用同一个目录时
# 下一次宿主机侧的 rm 会因为属主不对而失败（实测踩过）。
WORK=\$(mktemp -d /tmp/ss-src-XXXXXX)
trap 'docker run --rm -v /tmp:/x alpine sh -c "rm -rf /x/\$(basename \$WORK)" >/dev/null 2>&1 || true' EXIT
tar -xf /tmp/ss-src.tar -C "\$WORK"
cd "\$WORK"
# **关键**：新开一次性容器，而不是 docker exec 进生产容器——
# 后者会把测试文件写进生产容器的可写层，正是这次事故的形状。
docker run --rm \\
    --tmpfs /tmp:rw,exec,size=2g \\
    -v "\$WORK":/app -w /app \\
    $IMAGE bash scripts/_remote_pytest.sh $ARGS
EOF

scp -q -o BatchMode=yes "$TARBALL" "$HOST:/tmp/ss-src.tar"
scp -q -o BatchMode=yes "$RUNNER" "$HOST:/tmp/ss-runner.sh"
ssh -o BatchMode=yes "$HOST" 'bash /tmp/ss-runner.sh'
