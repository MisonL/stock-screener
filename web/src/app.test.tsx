// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { App } from "./main";
import { currentRoute } from "./routes";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement;

function stubFetch() {
  const body = (path: string) => {
    if (path.startsWith("/api/status")) {
      return {
        services: {
          api: { last_beat_at: null, stale_s: null },
          jobs: { last_beat_at: null, stale_s: null },
          agent: { last_beat_at: null, stale_s: null },
        },
        counts: { event_log: 0, signals: 0, push_log: 0 },
        pools: {},
        upstream_1h: {},
        notify_ok: true,
        notify_last_error: null,
        disk_used_pct: 0,
        maintenance: false,
        budget_exhausted: false,
      };
    }
    if (path.startsWith("/api/settings")) return { strategy: {}, markets: {} };
    if (path.startsWith("/api/kline/")) return { bars: [] };
    if (path.startsWith("/api/quote/")) return { code: "sz300750", price: 1, prev_close: 1, volume: 1 };
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
}

async function navigate(path: string) {
  await act(async () => {
    window.history.pushState(null, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

beforeEach(() => {
  stubFetch();
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

describe("App 路由", () => {
  test("popstate 让页面跟随 URL", async () => {
    await mount("/");
    expect(currentRoute()).toBe("/");
    await navigate("/pool");
    expect(host.querySelector("header select, header label")).not.toBeNull();
    expect(host.textContent).toContain("观察池");
    expect(host.querySelector("nav button.active")?.textContent).toBe("观察池");
  });

  test("个股页市场跟随个股：/stock/sz300750 → cn_gem", async () => {
    await mount("/stock/sz300750?m=cn_main");
    const select = host.querySelector("header select") as HTMLSelectElement;
    expect(select).not.toBeNull();
    expect(select.value).toBe("cn_gem");
  });

  test("个股页市场跟随个股：/stock/sh600519 → cn_main（切到港股也不跟）", async () => {
    window.localStorage.setItem("market", "hk");
    await mount("/stock/sh600519?m=hk");
    const select = host.querySelector("header select") as HTMLSelectElement;
    expect(select.value).toBe("cn_main");
  });
});

describe("currentRoute", () => {
  test("个股路径原样返回，便于取代码", () => {
    window.history.replaceState(null, "", "/stock/sh600519");
    expect(currentRoute()).toBe("/stock/sh600519");
  });
});

test("从个股页点导航离开时，市场回到用户全局选择（不带走个股市场）", async () => {
  // 先以 hk 市场进入 A 股个股页：shownMarket 应为 cn_main（按代码推断）
  // 用户全局市场 = hk（storage），先从 A 股个股页进入
  window.localStorage.setItem("market", "hk");
  await mount("/stock/sh600519");
  // 个股页按代码推断市场，但**不能写回全局选择**
  expect(window.localStorage.getItem("market")).toBe("hk");

  // 点「观察池」离开个股页：市场必须回到全局的 hk，而不是留在 cn_main
  const btn = Array.from(document.querySelectorAll("nav button"))
    .find((b) => b.textContent === "观察池") as HTMLButtonElement;
  await act(async () => btn.click());
  expect(window.location.pathname).toBe("/pool");
  // 观察池请求必须带上**全局**市场 hk；若把个股页推断出的 cn_main 带过来就会是 market=cn_main
  const urls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
    .map((c) => String(c[0]));
  expect(urls.some((u) => u.includes("/api/pool?market=hk"))).toBe(true);
});
