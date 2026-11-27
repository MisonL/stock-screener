// @vitest-environment jsdom
import { afterEach, describe, expect, test, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Signals } from "./Signals";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement;

async function mount() {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => { root!.render(<Signals market="cn_main" />); });
  return host;
}

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  host?.remove();
  vi.restoreAllMocks();
});

describe("信号页的两栏", () => {
  test("加载中两栏都不许写「无」——那会被读成「今天没有信号」", async () => {
    // 突破/跌破两栏各自有「无」占位。若不受取数状态约束，
    // 首屏还没回来时两栏都是「无」，看上去就是一条信号都没有。
    vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise(() => {}));
    const el = await mount();
    expect(el.textContent).toContain("正在拉取信号");
    const items = Array.from(el.querySelectorAll(".list li"));
    expect(items.filter((li) => li.textContent === "无")).toHaveLength(0);
  });

  test("确实拉到了、两栏都空，才显示「无」", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify({ rows: [] }),
        { status: 200, headers: { "Content-Type": "application/json" } }));
    const el = await mount();
    const empties = Array.from(el.querySelectorAll(".list li"))
      .filter((li) => li.textContent === "无");
    expect(empties).toHaveLength(2);       // 突破一栏 + 跌破一栏
  });

  test("突破与跌破分栏，且代码是站内链接", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify({ rows: [
        { id: 1, ts: "2026-11-27T09:21:00+08:00", code: "sh600519", kind: "breakout",
          level: "强", read_at: null },
        { id: 2, ts: "2026-11-27T09:22:00+08:00", code: "sz300750", kind: "breakdown",
          level: "弱", read_at: null },
      ] }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const el = await mount();
    const links = Array.from(el.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(links).toContain("/stock/sh600519");
    expect(links).toContain("/stock/sz300750");
    expect(el.textContent).toContain("突破（1）");
    expect(el.textContent).toContain("跌破（1）");
  });
});
