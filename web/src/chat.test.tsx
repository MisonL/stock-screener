// @vitest-environment jsdom
import { afterEach, describe, expect, test, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { App } from "./main";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement;

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  host?.remove();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

/** `/api/chat` 由 `reply` 决定，其余只读接口一律给空壳。 */
function stubFetch(reply: unknown) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path.startsWith("/api/chat")) {
      bodies.push(JSON.parse(String(init?.body ?? "{}")));
      return new Response(JSON.stringify(reply), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (path.startsWith("/api/status")) {
      return new Response(JSON.stringify({ services: {}, counts: {}, pools: {}, upstream_1h: {} }),
        { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify({ as_of: null, rows: [] }),
      { status: 200, headers: { "Content-Type": "application/json" } });
  });
}

const bodies: { session_id: number | null; text: string }[] = [];

async function mount(path: string) {
  window.history.replaceState(null, "", path);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<App />);
  });
  await act(async () => {
    await Promise.resolve();
  });
}

function typeAndSend(text: string) {
  const input = host.querySelector("input[aria-label='问题']") as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  act(() => {
    (host.querySelector("button") as HTMLButtonElement).click();
  });
}

const ANSWER = {
  session_id: 7,
  text: "现价 1680.00（get_quote.data.price）",
  as_of: "2026-10-30T10:30:05+08:00",
  market: "cn_main",
  fallback: false,
  grounded: { ungrounded: false, failed: false },
};

describe("对话页", () => {
  test("发送后把问题与回答都渲染出来，并显示市场与数据时刻", async () => {
    bodies.length = 0;
    stubFetch(ANSWER);
    await mount("/chat");
    typeAndSend("600519 现在什么情况");
    await act(async () => {
      await Promise.resolve();
    });
    expect(host.textContent).toContain("600519 现在什么情况");
    expect(host.textContent).toContain("1680.00");
    expect(host.textContent).toContain("cn_main");
    expect(host.textContent).toContain("10:30:05");
    expect(bodies[0].text).toBe("600519 现在什么情况");
  });

  test("首轮 session_id 传 null，第二轮带上服务端返回的 id", async () => {
    bodies.length = 0;
    stubFetch(ANSWER);
    await mount("/chat");
    typeAndSend("第一问");
    await act(async () => {
      await Promise.resolve();
    });
    typeAndSend("第二问");
    await act(async () => {
      await Promise.resolve();
    });
    expect(bodies.length).toBe(2);
    expect(bodies[0].session_id).toBeNull();
    expect(bodies[1].session_id).toBe(7);
  });

  test("未溯源段落灰显，并提示部分数字未通过校验", async () => {
    stubFetch({ ...ANSWER, text: "现价 9999.00（get_quote.data.price）",
      grounded: { ungrounded: false, failed: true } });
    await mount("/chat");
    typeAndSend("报个价");
    await act(async () => {
      await Promise.resolve();
    });
    expect(host.querySelector(".chat-ungrounded")).toBeTruthy();
    expect(host.textContent).toContain("未通过校验");
  });

  test("降级回答必须显式标出，不能和正常回答长得一样", async () => {
    stubFetch({ ...ANSWER, text: "AI 暂时不可用，以下是直接查询结果", fallback: true });
    await mount("/chat");
    typeAndSend("600519");
    await act(async () => {
      await Promise.resolve();
    });
    expect(host.textContent).toContain("AI 暂时不可用");
    expect(host.textContent).toContain("已降级为直接查询");
  });

  test("路由 /chat 指向对话页，底部导航有入口", async () => {
    stubFetch(ANSWER);
    await mount("/chat");
    expect(host.textContent).toContain("对话");
    const nav = Array.from(host.querySelectorAll("a,button"))
      .map((el) => el.textContent || "")
      .join("|");
    expect(nav).toContain("对话");
  });
});
