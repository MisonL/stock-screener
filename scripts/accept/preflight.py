#!/usr/bin/env python3
"""上线前自检：把「跑起来才发现」的配置/数据面问题一次性查出来。

    docker compose -f deploy/docker-compose.yml --project-directory . --profile tools \
        run --rm agent python scripts/accept/preflight.py [--for-shadow]

为什么需要它：影子观测期**不跑 LLM、不推送**，所以那些问题在那三周里完全看不见，
等切到正式启用才第一次暴露。这个脚本把能静态查的提前查掉：

- `budget.write_failed:{pool}` 这种 fail-closed 标志**按池生效**，切正式前必须确认两个池都是空的；
- `shadow_mode` 与实际 `decision` 分布是否一致（切了 false 但库里没有非 shadow 行 = 没生效）；
- 预算表、心跳、`agent_turn` 的真实数字是否对得上（第 4~7 轮的漏账问题在真实数据上就会现形）；
- 三份 env 是否齐全、代理变量是否为空（有代理连不上中转站）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import load_config  # noqa: E402
from core.db import connect  # noqa: E402

# **密钥变量名由 `llm.yaml` 的 `api_key_env` 决定**，不能写死 `LLM_API_KEY`：
# 生产用的是 `DEEPSEEK_API_KEY`，写死会让自检永久误报，把真正的缺失盖掉。
FIXED_ENV = ("NTFY_TOKEN",)
PROXY_ENV = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")


def check_env(cfg=None) -> list[str]:
    """检查必需的运行期环境变量。

    LLM 密钥的变量名从 `cfg.llm.api_key_env` 读（缺省 `LLM_API_KEY`）；
    `base_url` 在 yaml 里给了就不再要求 `LLM_BASE_URL` 环境变量。
    """
    out = [f"缺少环境变量 {k}" for k in FIXED_ENV if not os.environ.get(k)]
    if cfg is not None:
        key_env = getattr(getattr(cfg, "llm", None), "api_key_env", None) or "LLM_API_KEY"
        if not os.environ.get(key_env):
            out.append(f"缺少环境变量 {key_env}（llm.yaml 的 api_key_env）")
        base_url = getattr(getattr(cfg, "llm", None), "base_url", None)
        if not base_url and not os.environ.get("LLM_BASE_URL"):
            out.append("缺少 LLM_BASE_URL（yaml 与环境变量都没给）")
    return out


def check_proxy() -> list[str]:
    """容器里绝不能有代理：有的话连不上中转站，而且报错是连接超时，很难查。"""
    return [f"存在代理变量 {k}={os.environ[k]}" for k in PROXY_ENV if os.environ.get(k)]


def check_budget_flag(conn) -> list[str]:
    """记账故障标志（**按池隔离**：agent 失败不该锁住 chat）。"""
    out = []
    for pool in ("agent", "chat"):
        name = f"budget.write_failed:{pool}"
        row = conn.execute("SELECT value FROM job_state WHERE name = ?", (name,)).fetchone()
        if row and row[0]:
            out.append(f"[{pool}] 预算写库失败标志仍在（{row[0]}）：该池所有请求会被拒绝；"
                       f"确认无误后手动删除：DELETE FROM job_state WHERE name='{name}'")
    return out


def check_shadow(cfg, conn, for_shadow: bool) -> list[str]:
    att = getattr(cfg.agent, "attention", None)
    shadow = getattr(att, "shadow_mode", True) is not False
    out = []
    rows = conn.execute(
        "SELECT decision, count(*) FROM agent_turn GROUP BY decision").fetchall()
    counts = {r[0]: r[1] for r in rows}
    if for_shadow:
        if not shadow:
            out.append("shadow_mode 已是 false：本次是正式启用，不该按 shadow 验收")
        if counts.get("speak") or counts.get("silent"):
            out.append(f"shadow 期出现了真实回合（speak={counts.get('speak')},"
                       f" silent={counts.get('silent')}）：shadow 不应跑 LLM")
        if not counts.get("shadow"):
            out.append("还没有任何 decision='shadow' 行：shadow 观测尚未产生数据")
    else:
        if shadow:
            out.append("shadow_mode 仍是 true：现在不会跑 LLM、不会推送，不能按正式验收")
        if counts.get("shadow") and not (counts.get("speak") or counts.get("silent")):
            out.append("只有 shadow 行、没有真实回合：改了配置但没重建容器？")
    return out


def check_accounting(conn) -> list[str]:
    """预算表 ↔ `token_audit` 的完整性检查。

    **不要拿预算表跟真实用量比大小**：预算表记的是「发请求前按 输入估算+max_tokens
    不可退还地扣掉的保守值」，真实用量另记 `token_audit`。两者不相等是**设计**，
    不是漏账——按旧口径比会把健康部署判成「漏账」直接挡住上线。

    这里只查两件真会出错的事：
    - 预算表有花费，却**一条审计都没有**（记账出口坏了）；
    - 审计行数远少于成功回合数（部分 attempt 没审计）。
    """
    out = []
    for pool in ("agent", "chat"):
        booked = conn.execute(
            "SELECT coalesce(sum(tokens_in),0), coalesce(sum(tokens_out),0)"
            " FROM llm_budget WHERE pool = ?", (pool,)).fetchone()
        spent = int(booked[0]) + int(booked[1])
        audited = conn.execute(
            "SELECT count(*) FROM token_audit WHERE pool = ?", (pool,)).fetchone()[0]
        if spent > 0 and audited == 0:
            out.append(f"[{pool}] 预算表已扣 {spent}，但 token_audit 一条记录都没有："
                       f"审计出口坏了（查 on_attempt 是否接上）")
            continue
        if pool == "agent" and audited:
            # **排除影子回合**：影子期 `decision='shadow'` 且 `tokens_*` 为 NULL，
            # 它们不产生审计（也不该产生）。把它们算进分母会让自检在影子期
            # 误报「多数 attempt 没被记到」，而实际一条也没漏。
            turns = conn.execute(
                "SELECT count(*) FROM agent_turn WHERE decision <> 'shadow'").fetchone()[0]
            if turns and audited < turns * 0.5:
                out.append(f"[{pool}] {turns} 个回合只有 {audited} 条审计："
                           f"多数 attempt 没被 on_attempt 记到")
    return out


def _has_cols(conn, table: str, cols: tuple[str, ...]) -> bool:
    try:
        names = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:                                      # noqa: BLE001
        return False
    return all(c in names for c in cols)


def check_heartbeat(conn) -> list[str]:
    row = conn.execute("SELECT last_beat_at FROM service_heartbeat WHERE service = 'agent'"
                       ).fetchone()
    if not row or not row[0]:
        return ["没有 agent 心跳：watchdog 会判 agent_down（入口是不是没起 Heartbeat？）"]
    return []


def check_reserve(conn) -> list[str]:
    """**已废弃的预留口径**：保守扣减模型没有 `reserve`/`settle`，没有退款路径。

    旧检查拿「预算表有量」对「`agent_turn.tokens_in` 有没有行」，但新模型下
    预算表记的是**估算**、真实用量只进 `token_audit`，而且 chat 池的花费
    本来就不写 `agent_turn`——纯 chat 消费会被误报成「预留未对账」。
    真正的完整性检查已并入 `check_accounting`（盯预算↔审计的链路是否断），
    这里保留函数只为兼容既有调用点。
    """
    return []


def run(conn, cfg, for_shadow: bool = False) -> list[str]:
    problems: list[str] = []
    problems += check_env(cfg)
    problems += check_proxy()
    problems += check_budget_flag(conn)
    problems += check_shadow(cfg, conn, for_shadow)
    problems += check_heartbeat(conn)
    problems += check_accounting(conn)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="上线前自检")
    ap.add_argument("--for-shadow", action="store_true",
                    help="按「准备开始 shadow 观测」验收（而不是准备正式启用）")
    ap.add_argument("--db", default=os.environ.get("SS_DB_PATH", "data/market.db"))
    args = ap.parse_args()

    cfg = load_config(Path("config"))
    conn = connect(Path(args.db))
    problems = run(conn, cfg, args.for_shadow)
    if problems:
        print("自检未通过：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
