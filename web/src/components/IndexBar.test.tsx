// @vitest-environment jsdom
import { afterEach, describe, expect, test } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IndexBar } from "./IndexBar";

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
});

const ROWS = [
  { code: "sh000001", name: "上证指数", price: 3412.55, prev_close: 3398.1 },
  { code: "sz399001", name: "深证成指", price: 11233.4, prev_close: 11290.0 },
  { code: "sh000300", name: "沪深300", price: 4011.2, prev_close: 4011.2 },
  { code: "sz399006", name: "创业板指", price: 2211.0, prev_close: 2180.0 },
  { code: "600519", name: "贵州茅台", price: 1688, prev_close: 1670 },
];

describe("指数栏", () => {
  test("按当前市场选指数：切到港股就不该再显示上证", () => {
    // 背景：原来只有一个写死的 `sh000001` 和一个硬编码的「上证」标签，
    // 切到港股照样显示「上证 x.xx」——那个数既不是港股的，也不是真的。
    const cn = mount(<IndexBar rows={ROWS} market="cn_main" />);
    expect(cn.textContent).toContain("上证指数");
    act(() => root!.unmount());

    const hk = mount(<IndexBar rows={ROWS} market="hk" />);
    expect(hk.textContent).not.toContain("上证");
    expect(hk.textContent).not.toContain("深证");
    expect(hk.querySelector(".indexbar")).toBeNull();   // 港股没有指数就整条不画
  });

  test("个股绝不能混进指数栏", () => {
    const host = mount(<IndexBar rows={ROWS} market="cn_main" />);
    expect(host.textContent).not.toContain("贵州茅台");
  });

  test("取不到价格显示「—」，不是 0.00", () => {
    // `price ?? 0` 会把「没取到」显示成「跌到 0」——最坏的一种错报。
    const host = mount(
      <IndexBar rows={[{ code: "sh000001", name: "上证指数", price: null, prev_close: null }]}
                market="cn_main" />,
    );
    expect(host.textContent).toContain("—");
    expect(host.textContent).not.toContain("0.00");
  });

  test("没有 prev_close 就不印涨跌幅，而不是印 0.00%", () => {
    const host = mount(
      <IndexBar rows={[{ code: "sh000001", name: "上证指数", price: 3400, prev_close: null }]}
                market="cn_main" />,
    );
    expect(host.textContent).toContain("3400.00");
    expect(host.textContent).not.toContain("%");
  });

  test("涨跌幅带符号且按红涨绿跌上色", () => {
    const host = mount(<IndexBar rows={ROWS} market="cn_main" />);
    const pcts = Array.from(host.querySelectorAll(".indexpct")).map((e) => e.textContent);
    expect(pcts.some((t) => t?.startsWith("+"))).toBe(true);
    expect(pcts.some((t) => t?.startsWith("-"))).toBe(true);
    const flat = Array.from(host.querySelectorAll(".indexpct"))
      .find((e) => e.textContent === "+0.00%") as HTMLElement;
    expect(flat.style.color).toContain("--flat");
  });

  test("创业板市场只看创业板指", () => {
    const host = mount(<IndexBar rows={ROWS} market="cn_gem" />);
    expect(host.textContent).toContain("创业板指");
    expect(host.textContent).not.toContain("上证");
  });
});
