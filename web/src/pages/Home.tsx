import { colorVar } from "../lib/colors";
import { usePoll } from "../lib/api";
import type { Market } from "../lib/market";
import { Link } from "../components/Link";
import { ErrorNote, Loading } from "../components/Status";

type Status = {
  services: Record<
    string,
    {
      last_beat_at: string | null;
      stale_s: number | null;
    }
  >;
  counts: Record<string, number>;
  pools: Record<string, number>;
  notify_ok: boolean;
  notify_last_error: string | null;
  maintenance: boolean;
  /** Task 13 后端新增：日预算耗尽（预期停止，不是卡死） */
  budget_exhausted?: boolean;
  /** Task 13 后端新增：`agent_turn` 最后一行的 ISO 时间；null = 今天还没有任何回合 */
  agent_last_turn_at?: string | null;
  /** Task 13 后端新增：距最后一回合多少分钟（浮点）；无回合时后端给初始值 15.0 */
  agent_turn_stale_min?: number | null;
  /** 当前是否在交易时段（09:15–15:05 且开市）。收盘后无回合是预期，不标红。 */
  in_session?: boolean;
};

type PoolRow = { code: string; score: number; close: number; prev_close?: number };

/** 日志 summary 兜底：/api/status 顶层没有 budget_exhausted 时再从日志接口读一次 */
type AgentLogBody = { summary?: { budget_exhausted?: boolean }; turns?: unknown[] };

const STALE_LIMIT_MIN = 15;   // §7.6.3 / §8.1：超过 15 分钟标红

export function StaleBadge({ name, staleS }: { name: string; staleS: number | null }) {
  // > 15 分钟标红（§8.1 状态栏）
  const stale = staleS === null || staleS > 900;
  return (
    <span className={`badge ${stale ? "bad" : "ok"}`}>
      {name} {staleS === null ? "无心跳" : `${staleS}s`}
    </span>
  );
}

/**
 * 「agent 最后活动 N 分钟前」（§8.1 / §7.6.3）。
 *
 * 两条规则的**口径必须一致**：> 15 分钟标红表示「可能已停止」；但 `budget_exhausted`
 * 为真时是**预期停止**（日预算耗尽后 agent 主动不再唤醒，watchdog 规则 3 同样抑制
 * P1），所以不标红，改显示「今日额度已用完」。
 *
 * 分钟数用 `/api/status` 的 `agent_turn_stale_min`（**agent_turn 口径**，不是心跳）：
 * 心跳正常但回合不出来，正是最隐蔽的故障，心跳秒数看不出来。
 */
export function AgentActivity({
  staleMin,
  hasTurn = true,
  budgetExhausted,
  inSession = true,
  pending = false,
}: {
  staleMin: number | null;
  hasTurn?: boolean;
  budgetExhausted: boolean;
  /** 当前是否在交易时段。收盘后没有新回合是**预期**，不标红。 */
  inSession?: boolean;
  pending?: boolean;
}) {
  if (pending) {
    // 首屏还没拿到 /api/status：不下「已停止」的结论，也不标红
    return <span className="badge">agent 最后活动 …</span>;
  }

  // 只在**交易时段内**判断「可能已停止」：agent 只在 09:15–15:05 唤醒，
  // 收盘后一直到次日开盘都不会有新回合，只按分钟数标红会让首页每天
  // 收盘后一路红到天亮（实机验证：152 分钟前标红）。
  const stale =
    inSession && !budgetExhausted && (!hasTurn || staleMin === null || staleMin > STALE_LIMIT_MIN);

  let text: string;
  if (budgetExhausted) {
    text = "agent 已停止唤醒：今日额度已用完";
  } else if (!hasTurn || staleMin === null) {
    text = "agent 最后活动 尚无回合";
  } else {
    text = `agent 最后活动 ${Math.floor(staleMin)} 分钟前`;
  }

  return (
    <span
      className={`badge ${budgetExhausted ? "" : stale ? "bad" : "ok"}`}
      title={budgetExhausted ? "日预算耗尽，预期停止，不是卡死" : "超过 15 分钟标红（§7.6.3）"}
    >
      {text}
    </span>
  );
}

/** 从 `/api/status` 解析「最后活动」：优先回合口径，缺字段时退回心跳口径。 */
export function agentActivityOf(status: Status | null): {
  staleMin: number | null;
  hasTurn: boolean;
} {
  if (!status) return { staleMin: null, hasTurn: false };
  // **按「字段是否存在」判断新旧接口，不能按「值是否为 number」**：
  // 新接口在一次回合都没有时会明确返回 `agent_turn_stale_min: null`，
  // 用 typeof 判断会把它当成旧接口，转而拿心跳当依据并标 hasTurn=true，
  // 首页就会在一次回合都没有的情况下显示成绿色（"刚活动过"）。
  if ("agent_turn_stale_min" in status) {
    const v = status.agent_turn_stale_min;
    return {
      staleMin: typeof v === "number" ? v : null,
      hasTurn: status.agent_last_turn_at != null,
    };
  }
  // 兼容旧接口：只有心跳秒数时按心跳估算（口径较弱，仅作兜底）
  const beat = status.services?.agent?.stale_s;
  if (typeof beat === "number") return { staleMin: beat / 60, hasTurn: true };
  return { staleMin: null, hasTurn: false };
}

export function Home({ market }: { market: Market }) {
  const { data: status } = usePoll<Status>("/api/status", 10_000);
  const { data: pool, error: poolError, state: poolState } =
    usePoll<{ rows: PoolRow[] }>(`/api/pool?market=${market}`, 3_000);
  // 顶层没有再从日志 summary 兜底（Task 13：不臆造字段名，读不到按 false）
  const { data: log } = usePoll<AgentLogBody>("/api/agent-log", 60_000);
  const top = (pool?.rows ?? []).slice(0, 10);

  const activity = agentActivityOf(status ?? null);
  const budgetExhausted =
    status?.budget_exhausted ?? log?.summary?.budget_exhausted ?? false;

  return (
    <section>
      <h2>今日活动</h2>
      <div className="statusbar">
        <StaleBadge name="jobs" staleS={status?.services.jobs?.stale_s ?? null} />
        <AgentActivity
          staleMin={activity.staleMin}
          hasTurn={activity.hasTurn}
          budgetExhausted={budgetExhausted}
          inSession={status?.in_session ?? false}
          pending={status === null}
        />
        {status && !status.notify_ok && <span className="badge bad">推送通道异常</span>}
        {status?.maintenance && <span className="badge">维护模式</span>}
        <span className="badge">事件 {status?.counts.event_log ?? 0}</span>
        <span className="badge">信号 {status?.counts.signals ?? 0}</span>
      </div>

      <h2>池子 Top 10</h2>
      {poolState === "loading" && <Loading label="正在拉取观察池…" />}
      {poolState === "error" && poolError && <ErrorNote error={poolError} what="观察池" />}
      <ul className="list">
        {top.map((r) => {
          // 红涨绿跌必须按**涨幅**上色：价格恒大于 0，`close - 1` 会让所有票都显示红色。
          // `prev_close` 缺失时给 null 并显示「—」：那是「没算过」，不是「没涨没跌」。
          const chg = r.prev_close ? (r.close / r.prev_close - 1) * 100 : null;
          return (
            <li key={r.code}>
              <Link to={`/stock/${r.code}`}>{r.code}</Link>
              <span style={chg === null ? undefined : { color: colorVar(chg, market) }}>
                {r.close?.toFixed(2)}
                {chg === null ? "" : `（${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%）`}
              </span>
              <span>{r.score.toFixed(0)} 分</span>
            </li>
          );
        })}
        {/* 加载中/取数失败时都**不能**写「今天还没有池子」——
            那是把"没取到"说成"没有"，比报错更误导（它不响） */}
        {poolState === "ok" && top.length === 0 && (
          <li className="muted empty">今天还没有池子（选股任务可能还没跑）</li>
        )}
      </ul>
    </section>
  );
}
