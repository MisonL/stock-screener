#!/usr/bin/env bash
# 宿主机磁盘护栏：**容器里看不到 Docker 自己的可写层**，所以这一层必须在宿主机跑。
#
# 2026-11-21 事故复盘：
#   本机的长期测试容器 `ss-dev-run`（`sleep infinity`，从不删除）可写层涨到
#   230 GB，撑爆 Docker 虚拟磁盘（294.65/294.68 GB），PostgreSQL 起不来：
#   `could not write lock file "postmaster.pid": No space left on device`。
#   `docker system df` 当时显示容器占用 230.4 GB、其中 230.2 GB 可回收。
#
#   容器里的 `df /` 只能看到挂载进来的文件系统，**看不到 `/var/lib/docker`
#   的可写层明细**（该目录在容器内是空的）。所以"应用内每 10 分钟查磁盘"
#   （`jobs/cleanup.py::check_disk`）能发现**总量**危机，但发现不了
#   "某个容器在偷偷长"——这个脚本补的就是后者。
#
# 用法：加到宿主机 crontab（每 30 分钟）
#   */30 * * * * NTFY_URL=https://ntfy.example NTFY_TOPIC=ss \
#     /home/qdm/stock-screener/scripts/host_disk_guard.sh >> /var/log/ss-disk-guard.log 2>&1
#
# 阈值与应用侧对齐（`jobs/cleanup.py` 的 75/85/95）。
# **只报告，不自动删除**：删什么属于运维决策。
set -euo pipefail

HOST_WARN_PCT="${HOST_WARN_PCT:-75}"
CONTAINER_WARN_GB="${CONTAINER_WARN_GB:-5}"
RECLAIM_WARN_GB="${RECLAIM_WARN_GB:-20}"
NTFY_URL="${NTFY_URL:-}"
NTFY_TOPIC="${NTFY_TOPIC:-}"

notify() {
    local level="$1" msg="$2"
    echo "[$(date -Is)] [$level] $msg"
    if [ -n "$NTFY_URL" ] && [ -n "$NTFY_TOPIC" ]; then
        curl -s -m 10 -o /dev/null \
             -H "Title: 磁盘护栏" -H "Priority: ${level}" \
             -d "$msg" "$NTFY_URL/$NTFY_TOPIC" || true
    fi
}

# 把 "1.234GB" / "512MB" / "49.2kB" 统一成以 GB 为单位的数字
to_gb() {
    awk -v s="$1" 'BEGIN{
        if (match(s, /^[0-9.]+/)) {
            v = substr(s, 1, RLENGTH) + 0
            rest = substr(s, RLENGTH + 1)
            if (rest ~ /^[Gg]B/)      print v
            else if (rest ~ /^[Mm]B/) printf "%.4f\n", v / 1024
            else if (rest ~ /^[Kk]B/) printf "%.6f\n", v / 1048576
            else                      printf "%.4f\n", v / 1073741824
        } else print 0
    }'
}

# ① 宿主机根分区
USED_PCT=$(df --output=pcent / | tail -1 | tr -dc '0-9')
USED_PCT=${USED_PCT:-0}
if [ "$USED_PCT" -ge 95 ]; then
    notify urgent "宿主机磁盘 ${USED_PCT}%（>=95%）：随时可能写不进去"
elif [ "$USED_PCT" -ge 85 ]; then
    notify high "宿主机磁盘 ${USED_PCT}%（>=85%）"
elif [ "$USED_PCT" -ge "$HOST_WARN_PCT" ]; then
    notify default "宿主机磁盘 ${USED_PCT}%（>=${HOST_WARN_PCT}%，建议清理）"
fi

# ② 单个容器的可写层：这是 230 GB 那种"偷偷长"的唯一可靠信号
while read -r name size; do
    first=${size%% *}
    gb=$(to_gb "$first")
    if awk -v a="$gb" -v b="$CONTAINER_WARN_GB" 'BEGIN{exit !(a>=b)}'; then
        notify high "容器 ${name} 可写层 ${gb} GB（>=${CONTAINER_WARN_GB} GB）"
    fi
done < <(docker ps -s --format '{{.Names}} {{.Size}}')

# ③ 可回收空间：BuildKit 缓存与悬空镜像长期不管会堆到几十 GB
RECLAIM=$(docker system df --format '{{.Reclaimable}}' 2>/dev/null \
    | sed 's/ (.*//' | while read -r v; do to_gb "$v"; done \
    | awk '{s+=$1} END{printf "%.1f", s}')
RECLAIM=${RECLAIM:-0}
if awk -v a="$RECLAIM" -v b="$RECLAIM_WARN_GB" 'BEGIN{exit !(a>=b)}'; then
    notify default "Docker 可回收空间约 ${RECLAIM} GB（>=${RECLAIM_WARN_GB} GB），建议人工确认后执行 docker builder prune"
fi

exit 0