// @vitest-environment jsdom
import { afterEach, describe, expect, test, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { App } from "../main";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement;

/**
 * 每个页面的"缺字段"响应都必须不崩。
 *
 * 背景（2026-11-27 实机）：`/api/sector` 返回 `pct`，组件读 `chg`，
 * 于是 `undefined.toFixed(2)` 抛错 → React 卸载整棵树 →
 * **点"板块"整个应用白屏**，连导航都没了。
 *
 * 这类错误的共同点：字段名变了、列是 NULL、或者整条记录的键根本不存在。
 * 判空只写 `x === null` 挡不住 `undefined`，所以要逐个页面验证"喂缺字段
 * 也不崩"，而不是只在出错的那一处补一行。
 */
const PAGES: { path: string; api: string; bad: unknown }[] = [
  { path: "/", api: "/api/pool", bad: { trade_date: 20261127, rows: [{ code: "sh600519" }] } },
  { path: "/quotes", api: "/api/quotes", bad: { as_of: null, rows: [{ code: "sh600519" }] } },
  { path: "/pool", api: "/api/pool", bad: { trade_date: 20261127, rows: [{ code: "sh600519" }] } },
  { path: "/watch", api: "/api/watch", bad: { rows: [{ code: "sh600519" }] } },
  // chg 整条缺失（原 bug 就发生在这里：返回的是 pct，没有 chg）
  { path: "/sector", api: "/api/sector", bad: { rows: [{ code: "BK0475", name: "银行" }] } },
  { path: "/signals", api: "/api/signals", bad: { rows: [{ id: 1, code: "sh600519", kind: "breakout" }] } },
  { path: "/stock/sh600519", api: "/api/pool", bad: { trade_date: 20261127, rows: [{ code: "sh600519" }] } },
];

function stubAll(badFor: string, bad: unknown) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    if (path.startsWith(badFor)) {
      return new Response(JSON.stringify(bad), { status: 200,
        headers: { "Content-Type": "application/json" } });
    }
    // 其余接口给一个"几乎什么都没有"的最小响应，避免别的页面抢戏
    return new Response(JSON.stringify({ rows: [], as_of: null, trade_date: null, bars: [] }),
      { status: 200, headers: { "Content-Type": "application/json" } });
  });
}

async function mount(path: string) {
  window.history.replaceState(null, "", path);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => { root!.render(<App />); });
  return host;
}

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  host?.remove();
  vi.restoreAllMocks();
});

describe("任何一页吃到缺字段的响应，都不许白屏", () => {
  for (const { path, api, bad } of PAGES) {
    test(`${path}（${api} 缺字段）`, async () => {
      stubAll(api, bad);
      const el = await mount(path);
      // 关键：**导航必须还在**。整树被卸载时 nav 会一起消失，
      // 用户只能刷新——这正是实机看到的整站白屏。
      expect(el.querySelector("nav")).not.toBeNull();
      expect(el.querySelector("header")).not.toBeNull();
      expect(el.textContent!.length).toBeGreaterThan(20);
    });
  }

  test("真要抛错时，只毁这一页并在页内说清楚，其他页面照常切", async () => {
    // 造一个必然抛错的页面数据：numbers 里塞字符串，让 .toFixed 之外的
    // 计算炸掉。这里直接用 sector 的极端形态。
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path.startsWith("/api/sector")) {
        // name 是对象：React 渲染对象会抛 "Objects are not valid as a React child"
        return new Response(JSON.stringify({ rows: [{ code: "BK1", name: { x: 1 }, chg: 1 }] }),
          { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify({ rows: [], as_of: null, trade_date: null, bars: [] }),
        { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const el = await mount("/sector");
    // 导航还在（边界兜住了）
    expect(el.querySelector("nav")).not.toBeNull();
    // 并且**在页面里说清楚**了，不是一片空白
    expect(el.textContent).toContain("这一页渲染失败了");
    // 切到别的页面就恢复
    await act(async () => {
      window.history.pushState(null, "", "/quotes");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(el.textContent).not.toContain("这一页渲染失败了");
    errSpy.mockRestore();
  });
});
