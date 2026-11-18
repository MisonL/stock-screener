import { useMemo, useState } from "react";
import { usePoll } from "../lib/api";

/** 后端契约（Task 13 的 `GET /api/agent-log?date=YYYYMMDD`，见 §7.6.1）。 */
type ToolCall = { name: string; args?: Record<string, unknown> };

type Turn = {
  ts: string;
  wake_kind: string | null;
  decision: string | null;
  why: string | null;
  level: string | null;
  tool_calls: ToolCall[] | null;
  rounds: number | null;
  push_seq: string | null;
  schedule_next: number | null;
  /** 预渲染上下文的一句话摘要（Task 13 后端补；原始渲染文本未落库） */
  context_digest?: string | null;
  context_chars?: number | null;
  context_truncated?: boolean | null;
};

type Summary = {
  detected: number;
  escalated: number;
  spoke: number;
  silent: number;
  cost_cny: number;
  pricing_is_estimate: boolean;
  /** 部分版本可能带预算标志：读不到按 false 处理（不臆造字段名）。 */
  budget_exhausted?: boolean;
};

type AgentLogBody = { summary: Summary; turns: Turn[] };

/** 后端契约（`GET /api/push-log?date=YYYYMMDD`）：推送历史明细。
 *
 *  **这个接口原来不存在**，于是"今天那条为什么没收到"只能 SSH 进库查——
 *  失败、被抑制、审计行全都不可见，页面只能看到一枚通道级徽标。 */
type PushRow = {
  ts: string;
  code: string | null;
  kind: string;
  level: string | null;
  seq_id: string | null;
  segment: number | null;
  status: string;
  body: string | null;
  error: string | null;
  ntfy_id: string | null;
};

type PushLogBody = { date: number; counts: Record<string, number>; rows: PushRow[] };

/** 日期选择器用 YYYY-MM-DD，接口收 YYYYMMDD。 */
function toApiDate(isoDay: string): string {
  return isoDay.replace(/-/g, "");
}

function todayLocal(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

const WAKE: Record<string, string> = {
  event: "事件",
  heartbeat: "心跳",
  alarm: "闹钟回访",
  auction: "集合竞价",
  open: "开盘",
  close: "收盘",
  chat: "对话",
};

/** 决策徽章：沉默的回合也要一眼看见（§7.6.2「沉默的那几条也要在」）。 */
const DECISION: Record<string, { label: string; kind: string }> = {
  speak: { label: "开口", kind: "ok" },
  silent: { label: "沉默", kind: "quiet" },
  timeout: { label: "超时", kind: "bad" },
  error: { label: "出错", kind: "bad" },
  invalid: { label: "判定失败", kind: "bad" },
  gave_up: { label: "放弃", kind: "bad" },
  deduped: { label: "已去重", kind: "quiet" },
  shadow: { label: "观测期", kind: "quiet" },
};

function decisionInfo(decision: string | null): { label: string; kind: string } {
  return DECISION[decision ?? ""] ?? { label: decision ?? "未知", kind: "quiet" };
}

/** 「看了什么」：优先用 `context_digest`（后端补的预渲染上下文摘要，§7.6.1）；
 *  没有时用唤醒类型 + 工具调用还原。原始渲染文本未落库，摘要版式如
 *  `唤醒=event 事件=3 上下文=1840字符`。 */
function sawWhat(turn: Turn): string {
  if (turn.context_digest) {
    const truncated = turn.context_truncated ? "（已截断）" : "";
    return `${turn.context_digest}${truncated}`;
  }
  const wake = WAKE[turn.wake_kind ?? ""] ?? turn.wake_kind ?? "未知唤醒";
  const tools = (turn.tool_calls ?? []).map((t) => t.name).filter(Boolean);
  if (tools.length === 0) return `${wake}：预渲染上下文，未调用工具`;
  return `${wake}：${tools.join("、")}`;
}

function formatCost(cny: number): string {
  return `¥${cny.toFixed(2)}`;
}

export function TurnRow({ turn }: { turn: Turn }) {
  const info = decisionInfo(turn.decision);
  const spoke = turn.decision === "speak";
  const tools = (turn.tool_calls ?? []).map((t) => t.name).filter(Boolean);
  // `agent_turn.schedule_next` 存的就是**分钟**（agent/decision.py 的
  // "下次回访距今分钟数"）；再除 60 会把真实的 30 分钟显示成 0 分钟。
  const scheduleMin =
    typeof turn.schedule_next === "number" ? Math.round(turn.schedule_next) : null;

  return (
    <li className="logturn">
      <div className="loghead">
        <span className="muted">{turn.ts.slice(5, 16).replace("T", " ")}</span>
        <span className={`badge logbadge-${info.kind}`}>{info.label}</span>
        {turn.level && <span className="muted">{turn.level}</span>}
        {turn.rounds != null && <span className="muted">{turn.rounds} 轮</span>}
      </div>
      <div className="logbody">
        <div>看了：{sawWhat(turn)}</div>
        {spoke ? (
          <div>
            说了：{turn.why ?? "（未记录理由）"}
            {turn.push_seq && <span className="muted"> → 已推送 {turn.push_seq}</span>}
          </div>
        ) : (
          <div>
            没说：{turn.why ?? "（未记录理由）"}
          </div>
        )}
        {scheduleMin != null ? (
          <div>排了闹钟：{scheduleMin} 分钟后再看一次</div>
        ) : (
          <div className="muted">排了闹钟：无</div>
        )}
        {!spoke && tools.length > 0 && (
          <div className="muted">调用了 {tools.join("、")}</div>
        )}
      </div>
    </li>
  );
}

/** 推送状态：`accepted` 才算真的到了手机。 */
const PUSH_STATUS: Record<string, { label: string; kind: string }> = {
  accepted: { label: "已送达", kind: "ok" },
  suppressed: { label: "被抑制", kind: "quiet" },
  failed: { label: "发送失败", kind: "bad" },
  pending: { label: "发送中", kind: "quiet" },
};

const PUSH_KIND: Record<string, string> = {
  breakout: "突破",
  breakdown: "跌破",
  agent_note: "agent 便签",
  open_brief: "开盘简报",
  close_digest: "收盘摘要",
  confirm_card: "确认卡",
  confirm_result: "确认结果",
  push_channel_down: "通道故障",
  llm_down: "LLM 故障",
};

function PushRowItem({ row }: { row: PushRow }) {
  const info = PUSH_STATUS[row.status] ?? { label: row.status, kind: "quiet" };
  return (
    <li className="logturn">
      <div className="loghead">
        <span className="muted">{row.ts.slice(5, 16).replace("T", " ")}</span>
        <span className={`badge logbadge-${info.kind}`}>{info.label}</span>
        <span className="muted">{PUSH_KIND[row.kind] ?? row.kind}</span>
        {row.code && <span className="muted">{row.code}</span>}
        {row.level && <span className="muted">{row.level}</span>}
        {row.segment === 2 && <span className="muted">第二段</span>}
      </div>
      <div className="logbody">
        {row.body && <div>{row.body}</div>}
        {row.error && <div className="bad">原因：{row.error}</div>}
      </div>
    </li>
  );
}

export function AgentLog() {
  // 日期选择器默认今天；页面上「按时间倒序，一天一屏」（§7.6.2）
  const [date, setDate] = useState<string>(() => todayLocal());
  const { data, error } = usePoll<AgentLogBody>(`/api/agent-log?date=${toApiDate(date)}`, 30_000);
  const { data: pushData } = usePoll<PushLogBody>(`/api/push-log?date=${toApiDate(date)}`, 30_000);
  const summary = data?.summary;
  const turns = useMemo(() => (data?.turns ?? []).slice().reverse(), [data]);
  const pushes = pushData?.rows ?? [];
  const pushCounts = pushData?.counts ?? {};

  return (
    <section>
      <div className="logfilter">
        <h2>工作日志</h2>
        <label>
          日期{" "}
          <input type="date" value={date} max={todayLocal()} onChange={(e) => setDate(e.target.value)} />
        </label>
      </div>

      {summary && (
        <div className="statusbar">
          <span className="badge">检测 {summary.detected} 个异动</span>
          <span className="badge">升级 {summary.escalated} 个</span>
          <span className="badge">开口 {summary.spoke} 次</span>
          <span className="badge">沉默 {summary.silent} 次</span>
          <span className="badge">花费 {formatCost(summary.cost_cny ?? 0)}</span>
          {summary.pricing_is_estimate && (
            <span className="muted">估算，以中转站账单为准</span>
          )}
          {summary.budget_exhausted && (
            <span className="badge bad">今日额度已用完</span>
          )}
        </div>
      )}

      <ul className="list">
        {turns.map((t, i) => <TurnRow key={`${t.ts}-${i}`} turn={t} />)}
        {turns.length === 0 && (
          <li className="muted">
            {error ? "日志读取失败" : data ? `这一天没有回合（${date}）` : "加载中…"}
          </li>
        )}
      </ul>

      {/* 推送历史：**每一条的最终状态与失败原因**。原来这一块完全不可见，
          "今天那条为什么没收到"只能进库查。 */}
      <div className="logfilter">
        <h2>推送历史</h2>
      </div>
      {pushData && (
        <div className="statusbar">
          <span className="badge">已送达 {pushCounts.accepted ?? 0}</span>
          <span className="badge">被抑制 {pushCounts.suppressed ?? 0}</span>
          <span className={`badge${(pushCounts.failed ?? 0) > 0 ? " bad" : ""}`}>
            失败 {pushCounts.failed ?? 0}
          </span>
        </div>
      )}
      {/* **独立的 class**：回合列表的 `.list li.logturn` 是"沉默也在"那条断言的
          选择器，混进来会把回合数算错（实测 3 条回合被算成 6 条）。 */}
      <ul className="list pushlist">
        {pushes.map((r, i) => <PushRowItem key={`${r.ts}-${i}`} row={r} />)}
        {pushes.length === 0 && (
          <li className="muted">{pushData ? `这一天没有推送记录（${date}）` : "加载中…"}</li>
        )}
      </ul>
    </section>
  );
}
