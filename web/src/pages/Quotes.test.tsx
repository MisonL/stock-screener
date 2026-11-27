// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Quotes } from "./Quotes";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement;

async function mount() {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => { root!.render(<Quotes />); });
  return host;
}

beforeEach(() => { host = document.createElement("div"); });
afterEach(() => {
  act(() => root?.unmount());
  root = null;
  host?.remove();
  vi.restoreAllMocks();
});

describe("行情页的三种状态", () => {
  test("还在加载时不写「快照还没到」——没取到 ≠ 没有", async () => {
    // 背景：以前只有「空数组 → 印一句『快照还没到』」，而首屏请求还没回来时
    // 数组同样是空的。于是页面一进来就说「没数据」，用户以为系统坏了。
    vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise(() => {}));
    const el = await mount();
    expect(el.textContent).toContain("正在拉取行情");
    expect(el.textContent).not.toContain("快照还没到");
    expect(el.querySelector("[role=status]")).not.toBeNull();   // 读屏会播报
  });

  test("取数失败要说清是哪一步失败，而不是一片空白", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response("boom", { status: 500 }));
    const el = await mount();
    expect(el.textContent).toContain("取数失败");
    expect(el.textContent).toContain("行情");
    expect(el.querySelector("[role=alert]")).not.toBeNull();
    expect(el.textContent).not.toContain("快照还没到");
  });

  test("真的拿到了、只是没有行，才允许说「快照还没到」", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify({ as_of: null, rows: [] }),
        { status: 200, headers: { "Content-Type": "application/json" } }));
    const el = await mount();
    expect(el.textContent).toContain("快照还没到");
    expect(el.textContent).not.toContain("加载中");
  });

  test("prev_close 缺失时涨跌印「—」，不印 0.00%", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify({
        as_of: "2026-11-27 10:00",
        rows: [{ code: "sh600519", name: "贵州茅台", price: 1688, prev_close: 0, volume: 1 }],
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const el = await mount();
    expect(el.textContent).toContain("1688.00");
    expect(el.textContent).not.toContain("0.00%");
    expect(el.textContent).toContain("—");
  });

  test("股票代码是真链接（可中键新开），但左键点击不整页刷新", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify({
        as_of: null,
        rows: [{ code: "sh600519", name: "贵州茅台", price: 1688, prev_close: 1670, volume: 1 }],
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const el = await mount();
    const a = el.querySelector("a")!;
    expect(a.getAttribute("href")).toBe("/stock/sh600519");

    window.history.replaceState(null, "", "/quotes");
    const ev = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    await act(async () => { a.dispatchEvent(ev); });
    expect(ev.defaultPrevented).toBe(true);
    expect(window.location.pathname).toBe("/stock/sh600519");
  });
});
