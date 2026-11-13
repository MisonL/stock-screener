#!/usr/bin/env bash
# 3 天挂机验收：一次性投递 71 条服务端延迟推送（X-Delay: 1h … 71h）。
# ntfy 的服务端延迟上限是 3 天，不需要宿主机 cron/at —— 投递完这个脚本就可以退出。
#
# 用法（在 VM 上，env 里已有 token）：
#   NTFY_URL=https://ntfy.<域名> NTFY_TOKEN=tk_xxx TOPIC=stock-alerts ./scripts/accept/soak.sh
#
# 验收：3 天后手机 App 里应有 71/71 条，且你随手记下的「手机响了」时刻与消息时间相差 < 1 分钟。

set -euo pipefail

: "${NTFY_URL:?需要 NTFY_URL，例如 https://ntfy.example.com}"
: "${NTFY_TOKEN:?需要 NTFY_TOKEN（tk_ 开头）}"
: "${TOPIC:?需要 TOPIC，与 config/notify.yaml 的 topic 一致}"

for i in $(seq 1 71); do
  curl -fsS \
    -H "Authorization: Bearer ${NTFY_TOKEN}" \
    -H "X-Delay: ${i}h" \
    -H "X-Title: soak ${i}/71" \
    -d "挂机验收 ${i}/71：预计 ${i} 小时后到达" \
    "${NTFY_URL}/${TOPIC}" > /dev/null
  echo "queued X-Delay: ${i}h"
done

echo "71 条已入队；3 天后核对 App 内 71/71。"
