// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { App } from "./main";
import { AgentActivity } from "./pages/Home";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement;

/** 后端契约（Task 13）：沉默的回合也在 turns 里，summary 带 pricing_is_estimate。 */
const agentLogBody = () => ({
  summary: {
    detected: 12,
    escalated: 4,
    spoke: 2,
    silent: 2,
    cost_cny: 0.31,
    pricing_is_estimate: true,
  },
  turns: [
    {
      ts: "2026-10-30T10:30:00+08:00",
      wake_kind: "event",
      context_digest: "唤醒=event 事件=3 上下文=1840字符",
      context_chars: 1840,
      context_truncated: false,
      decision: "speak",
      why: "板块整体 -2.8%，个股破位，值得说",
      level: "P1",
      tool_calls: [{ name: "get_quote", args: {} }],
      rounds: 1,
      push_seq: "sig-600519-20261030-1",
      schedule_next: 30,      // 分钟（生产口径）
    },
    {
      ts: "2026-10-30T10:15:00+08:00",
      wake_kind: "heartbeat",
      context_digest: "唤醒=heartbeat 池子=5 上下文=5200字符",
      context_chars: 5200,
      context_truncated: true,
      decision: "silent",
      why: "量能都只有 1.2–1.3×，未达 1.5× 门槛，且价格未动",
      level: null,
      tool_calls: [],
      rounds: 1,
      push_seq: null,
      schedule_next: null,
    },
    {
      ts: "2026-10-30T10:00:00+08:00",
      wake_kind: "alarm",
      context_digest: "唤醒=alarm 码=600519 上下文=900字符",
      context_chars: 900,
      context_truncated: false,
      decision: "silent",
      why: "30 分钟前那笔放量没有延续，量能已回落到 0.9×",
      level: null,
      tool_calls: [],
      rounds: 1,
      push_seq: null,
      schedule_next: null,
    },
  ],
});

function statusBody(
  overrides: { turn_stale_min?: number | null; last_turn_at?: string | null;
               budget_exhausted?: boolean; in_session?: boolean } = {},
) {
  const staleMin = "turn_stale_min" in overrides ? overrides.turn_stale_min : 2.0;
  const lastTurnAt =
    "last_turn_at" in overrides
      ? overrides.last_turn_at
      : staleMin == null ? null : new Date(Date.now() - staleMin * 60_000).toISOString();
  return {
    services: {
      api: { last_beat_at: null, stale_s: null },
      jobs: { last_beat_at: null, stale_s: 30 },
      // 心跳口径：故意一直"健康"，证明首页看的是回合口径而不是心跳
      agent: { last_beat_at: null, stale_s: 5 },
    },
    counts: { event_log: 0, signals: 0, push_log: 0 },
    pools: {},
    upstream_1h: {},
    notify_ok: true,
    notify_last_error: null,
    disk_used_pct: 0,
    maintenance: false,
    budget_exhausted: overrides.budget_exhausted ?? false,
    agent_last_turn_at: lastTurnAt,
    agent_turn_stale_min: staleMin,
    // 默认按「交易时段内」——老用例都是这个前提；收盘后的用例显式传 false
    in_session: overrides.in_session ?? true,
  };
}

/** 推送历史：**必须带失败行**——"今天那条为什么没收到"就靠它。
 *  这个接口原来不存在，失败/被抑制/审计行全都不可见。 */
function pushLogBody() {
  return {
    date: 20261119,
    counts: { accepted: 2, suppressed: 1, failed: 1 },
    rows: [
      { ts: "2026-11-19T10:30:00+08:00", code: null, kind: "llm_down", level: "P0",
        seq_id: "s1", segment: 1, status: "accepted", body: "LLM 连续失败", error: null,
        ntfy_id: "n1" },
      { ts: "2026-11-19T10:20:00+08:00", code: "sh600519", kind: "breakout", level: "P2",
        seq_id: "s2", segment: 1, status: "failed", body: "放量突破", error: "通道 503",
        ntfy_id: null },
      { ts: "2026-11-19T10:10:00+08:00", code: "sh600000", kind: "breakdown", level: "P2",
        seq_id: "s3", segment: 1, status: "suppressed", body: "跌破", error: null,
        ntfy_id: null },
    ],
  };
}

function stubFetch(
  overrides: { turn_stale_min?: number | null; last_turn_at?: string | null;
               budget_exhausted?: boolean; in_session?: boolean } = {},
) {
  const body = (path: string) => {
    if (path.startsWith("/api/status")) return statusBody(overrides);
    if (path.startsWith("/api/agent-log")) return agentLogBody();
    if (path.startsWith("/api/push-log")) return pushLogBody();
    if (path.startsWith("/api/settings")) return { strategy: {}, markets: {} };
    return { as_of: null, trade_date: null, rows: [] };
  };
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
    new Response(JSON.stringify(body(String(input))), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

async function mount(path: string) {
  window.history.replaceState(null, "", path);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<App />);
  });
  // 首轮 fetch 是异步的：再让微任务跑完，页面才有数据
  await act(async () => {
    await Promise.resolve();
  });
}

function fetchedPaths(): string[] {
  return (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
    .map((c) => String(c[0]));
}

/** 首页状态栏里的「agent 最后活动」徽章 */
function agentBadge(): HTMLElement | null {
  return (
    Array.from(document.querySelectorAll<HTMLElement>(".statusbar .badge"))
      .find((el) => (el.textContent ?? "").includes("agent")) ?? null
  );
}

beforeEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    /* jsdom 在无 origin 时可能不给 localStorage，忽略 */
  }
});

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  host?.remove();
  vi.restoreAllMocks();
});

describe("工作日志页", () => {
  test("路由 /agent-log 指向日志页，并按今天请求 /api/agent-log", async () => {
    stubFetch();
    await mount("/agent-log");
    expect(host.textContent).toContain("工作日志");
    const logCall = fetchedPaths().find((p) => p.startsWith("/api/agent-log"));
    expect(logCall).toBeDefined();
    // 日期参数是 YYYYMMDD（后端契约），默认今天
    expect(logCall).toMatch(/[?&]date=\d{8}$/);
    expect(host.querySelector("input[type=date]")).not.toBeNull();
  });

  test("推送历史显示失败原因（这条故障原来完全不可见）", async () => {
    stubFetch();
    await mount("/agent-log");
    expect(host.textContent).toContain("推送历史");
    const pushRows = Array.from(host.querySelectorAll(".pushlist li.logturn"));
    expect(pushRows.length).toBeGreaterThan(0);
    // 失败的那条必须让人看见原因
    expect(host.textContent).toContain("发送失败");
    expect(host.textContent).toContain("通道 503");
    expect(host.textContent).toContain("被抑制");
    expect(host.textContent).toContain("已送达");
  });

  test("沉默的回合也在列表里（这是这张表的灵魂）", async () => {
    stubFetch();
    await mount("/agent-log");
    const rows = Array.from(host.querySelectorAll(".list:not(.pushlist) li.logturn"));
    expect(rows).toHaveLength(3);
    const silent = rows.filter((r) => (r.textContent ?? "").includes("沉默"));
    // 两条 silent 回合都必须渲染出来，且带回自己的 why
    expect(silent).toHaveLength(2);
    expect(host.textContent).toContain("量能都只有 1.2–1.3×，未达 1.5× 门槛");
    expect(host.textContent).toContain("30 分钟前那笔放量没有延续");
  });

  test("每条回合显示三件套：看了 / 说了或没说 / 排了闹钟", async () => {
    stubFetch();
    await mount("/agent-log");
    expect(host.textContent).toContain("看了：");
    expect(host.textContent).toContain("说了：");
    expect(host.textContent).toContain("没说：");
    expect(host.textContent).toContain("排了闹钟：");

    const rows = Array.from(host.querySelectorAll(".list li.logturn"));
    const speaking = rows.find((r) => (r.textContent ?? "").includes("开口"))!;
    const quiet = rows.find((r) => (r.textContent ?? "").includes("沉默"))!;
    expect(speaking.textContent).toContain("说了：");
    expect(speaking.textContent).not.toContain("没说：");
    expect(quiet.textContent).toContain("没说：");
    expect(quiet.textContent).not.toContain("说了：");

    // 「看了」用后端补的 context_digest 作为主文案；被裁过的上下文加「已截断」
    expect(speaking.textContent).toContain("看了：唤醒=event 事件=3 上下文=1840字符");
    // 两条 silent 里 10:15 那条上下文被 6000 字符预算裁过，需带「已截断」标记
    const truncated = rows.find((r) => (r.textContent ?? "").includes("上下文=5200字符"))!;
    expect(truncated.textContent).toContain("已截断");
    // `schedule_next` 就是分钟（agent/decision.py），**不能再除 60**：
    // 真实的 30 分钟曾被渲染成「0 分钟」；没排的也明确写「无」
    expect(speaking.textContent).toContain("排了闹钟：30 分钟后再看一次");
    expect(quiet.textContent).toContain("排了闹钟：无");
  });

  test("pricing_is_estimate=true 时出现「估算，以中转站账单为准」，summary 显示四个计数与花费", async () => {
    stubFetch();
    await mount("/agent-log");
    expect(host.textContent).toContain("估算，以中转站账单为准");
    expect(host.textContent).toContain("检测 12 个异动");
    expect(host.textContent).toContain("升级 4 个");
    expect(host.textContent).toContain("开口 2 次");
    expect(host.textContent).toContain("沉默 2 次");
    expect(host.textContent).toContain("¥0.31");
  });
});

describe("首页 agent 最后活动", () => {
  test("最后活动 16 分钟（>15）标红——即使心跳只有 5 秒", async () => {
    stubFetch({ turn_stale_min: 16.0 });
    await mount("/");
    const badge = agentBadge();
    expect(badge).not.toBeNull();
    expect(badge!.textContent).toContain("agent 最后活动 16 分钟前");
    expect(badge!.classList.contains("bad")).toBe(true);
  });

  test("最后活动 5 分钟不标红", async () => {
    stubFetch({ turn_stale_min: 5.0 });
    await mount("/");
    const badge = agentBadge();
    expect(badge!.textContent).toContain("agent 最后活动 5 分钟前");
    expect(badge!.classList.contains("bad")).toBe(false);
  });

  test("收盘后（非交易时段）长期没有回合**不标红**", async () => {
    // agent 只在 09:15–15:05 唤醒，收盘后没有新回合是**预期**行为。
    // 只按分钟数标红会让首页每天收盘后一路红到天亮（实机验证：152 分钟前标红）。
    stubFetch({ turn_stale_min: 152.0, in_session: false });
    await mount("/");
    const badge = agentBadge();
    expect(badge!.classList.contains("bad")).toBe(false);
    expect(badge!.textContent).toContain("152 分钟前");
  });

  test("交易时段内超过 15 分钟仍然标红", async () => {
    stubFetch({ turn_stale_min: 16.0, in_session: true });
    await mount("/");
    const badge = agentBadge();
    expect(badge!.classList.contains("bad")).toBe(true);
  });

  test("budget_exhausted=true 时显示「今日额度已用完」且不标红（即使超过 15 分钟）", async () => {
    stubFetch({ turn_stale_min: 100.0, budget_exhausted: true });
    await mount("/");
    const badge = agentBadge();
    expect(badge).not.toBeNull();
    expect(badge!.textContent).toContain("今日额度已用完");
    expect(badge!.classList.contains("bad")).toBe(false);
  });

  test("AgentActivity：15 分钟边界不红、>15 标红、无回合标红、预算耗尽优先于标红", () => {
    const render1 = (staleMin: number | null, budget: boolean, hasTurn = true) => {
      const el = document.createElement("div");
      const r = createRoot(el);
      act(() =>
        r.render(
          <AgentActivity staleMin={staleMin} hasTurn={hasTurn} budgetExhausted={budget} />,
        ),
      );
      const span = el.querySelector(".badge") as HTMLElement;
      const out = { text: span.textContent ?? "", bad: span.classList.contains("bad") };
      act(() => r.unmount());
      return out;
    };
    // 恰好 15 分钟是**不**标红的边界（规则是「> 15 分钟」）
    expect(render1(15, false)).toEqual({ text: "agent 最后活动 15 分钟前", bad: false });
    expect(render1(15.1, false).bad).toBe(true);
    // agent_last_turn_at == null：今天还没有任何回合（后端 stale_min 会给 15.0，不能用它判断）
    expect(render1(15, false, false)).toEqual({ text: "agent 最后活动 尚无回合", bad: true });
    const budget = render1(60, true);
    expect(budget.text).toContain("今日额度已用完");
    expect(budget.bad).toBe(false);
  });

  test("agent_last_turn_at=null 时显示「尚无回合」而不是用初始值 15.0 说成「15 分钟前」", async () => {
    stubFetch({ turn_stale_min: 15.0, last_turn_at: null });
    await mount("/");
    const badge = agentBadge();
    expect(badge!.textContent).toContain("尚无回合");
    expect(badge!.classList.contains("bad")).toBe(true);
  });

  test("budget_exhausted 只在 /api/agent-log 的 summary 里时也能识别（兜底链）", async () => {
    const spy = stubFetch({ turn_stale_min: 100.0 });
    // 覆盖 /api/status：去掉预算标志，迫使前端去日志 summary 里读
    const inner = spy.getMockImplementation()!;
    spy.mockImplementation(async (input) => {
      const path = String(input);
      if (path.startsWith("/api/status")) {
        const body = statusBody({ turn_stale_min: 100.0 }) as Record<string, unknown>;
        delete body.budget_exhausted;
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (path.startsWith("/api/agent-log")) {
        const body = agentLogBody();
        (body.summary as Record<string, unknown>).budget_exhausted = true;
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return inner(input);
    });
    await mount("/");
    const badge = agentBadge();
    expect(badge!.textContent).toContain("今日额度已用完");
    expect(badge!.classList.contains("bad")).toBe(false);
  });
});

describe("agentActivityOf 的新旧接口判断", () => {
  test("新接口明确返回 stale_min=null（一次回合都没有）时不能当成旧接口回退到心跳", async () => {
    const { agentActivityOf } = await import("./pages/Home");
    const out = agentActivityOf({
      // 新接口：字段存在但为 null
      agent_turn_stale_min: null,
      agent_last_turn_at: null,
      services: { agent: { stale_s: 3 } },   // 心跳很新
    } as never);
    expect(out.hasTurn).toBe(false);
    expect(out.staleMin).toBeNull();
  });

  test("旧接口（完全没有该字段）才回退到心跳", async () => {
    const { agentActivityOf } = await import("./pages/Home");
    const out = agentActivityOf({ services: { agent: { stale_s: 120 } } } as never);
    expect(out.hasTurn).toBe(true);
    expect(out.staleMin).toBe(2);
  });
});
