// @vitest-environment jsdom
import { afterEach, describe, expect, test, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Link } from "./Link";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement;

function mount(node: React.ReactNode) {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  act(() => root!.render(node));
  return host;
}

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  host?.remove();
  window.history.replaceState(null, "", "/");
});

/** 造一次点击。`button: 0` 是左键，其余交给浏览器。 */
function click(el: HTMLElement, init: MouseEventInit = {}) {
  const ev = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, ...init });
  act(() => { el.dispatchEvent(ev); });
  return ev;
}

describe("站内链接", () => {
  test("普通左键点击走客户端路由：拦下原生导航，改 pushState", () => {
    // 背景：列表里原来是裸 `<a href="/stock/xxx">`，点一下整个应用重载
    // —— 重新下 bundle、重跑 App、所有 usePoll 从头拉一遍。
    const host = mount(<Link to="/stock/sh600519">600519</Link>);
    const a = host.querySelector("a")!;

    const ev = click(a);

    // 拦下了：jsdom 里裸链接会尝试原生导航（不实现，pathname 不变）
    expect(ev.defaultPrevented).toBe(true);
    // 并且真的跳了
    expect(window.location.pathname).toBe("/stock/sh600519");
  });

  test("href 必须保留：中键新开、右键复制链接、无 JS 时仍可跳", () => {
    const host = mount(<Link to="/stock/sh600519">600519</Link>);
    expect(host.querySelector("a")!.getAttribute("href")).toBe("/stock/sh600519");
  });

  test("⌘/Ctrl/Shift/中键点击交给浏览器（开新标签页），不许抢", () => {
    const host = mount(<Link to="/stock/sh600519">600519</Link>);
    const a = host.querySelector("a")!;

    for (const init of [{ metaKey: true }, { ctrlKey: true }, { shiftKey: true }, { button: 1 }]) {
      window.history.replaceState(null, "", "/");
      const ev = click(a, init);
      expect(ev.defaultPrevented).toBe(false);
      expect(window.location.pathname).toBe("/");
    }
  });

  test("点当前页不产生多余的历史记录", () => {
    window.history.replaceState(null, "", "/pool");
    const host = mount(<Link to="/pool">观察池</Link>);
    const before = window.history.length;
    click(host.querySelector("a")!);
    expect(window.location.pathname).toBe("/pool");
    expect(window.history.length).toBe(before);
  });

  test("跳转后补发 popstate，让 App 的路由与市场解析只走一份逻辑", () => {
    // `main.tsx` 只监听 popstate。若点击只是 pushState 不发事件，
    // 页面不会重新渲染；若只在点击路径里单独调一套逻辑，
    // 「离开个股页恢复全局市场」这类规则就会漏掉一条路径。
    const seen: PopStateEvent[] = [];
    const onPop = (e: PopStateEvent) => seen.push(e);
    window.addEventListener("popstate", onPop);
    try {
      const host = mount(<Link to="/quotes">行情</Link>);
      click(host.querySelector("a")!);
      expect(seen.length).toBe(1);
    } finally {
      window.removeEventListener("popstate", onPop);
    }
  });
});
