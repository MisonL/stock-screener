// @vitest-environment jsdom
import { afterEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Sector } from "./Sector";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const stub = (body: unknown) =>
  vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
    new Response(JSON.stringify(body), { status: 200,
      headers: { "Content-Type": "application/json" } }));

describe("板块页对缺字段要稳", () => {
  test("chg 整个键不存在时不许崩（实机白屏的原始触发点）", async () => {
    // 实机：接口给的是 `pct`，组件读 `chg` → undefined → toFixed 抛错。
    // 这里**不套 ErrorBoundary**，直接渲染组件，确认它自己就不崩。
    stub({ rows: [{ code: "BK0475", name: "银行", pct: 1.23 }] });
    expect(() => render(<Sector />)).not.toThrow();
    expect(await screen.findByText("银行")).toBeTruthy();
  });

  test("chg 为 null / NaN / 字符串时都显示「—」，不显示 0.00%", async () => {
    stub({ rows: [
      { code: "A", name: "甲", chg: null },
      { code: "B", name: "乙", chg: NaN },
      { code: "C", name: "丙", chg: "1.2" },
    ] });
    render(<Sector />);
    expect(await screen.findByText("甲")).toBeTruthy();
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(3);
    expect(document.body.textContent).not.toContain("0.00%");
  });

  test("正常的 chg 照常显示带符号百分比", async () => {
    stub({ rows: [{ code: "A", name: "甲", chg: 1.234, members: 12 }] });
    render(<Sector />);
    expect(await screen.findByText("+1.23%")).toBeTruthy();
    expect(screen.getByText("12 只")).toBeTruthy();
  });
});
