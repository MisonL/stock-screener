// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { KLineChart, Stock } from "./Stock";
import type { KBar } from "../lib/aggregate";

const bars: KBar[] = [
  { date: 20261026, open: 10, high: 12, low: 9, close: 11, volume: 100 },
  { date: 20261027, open: 11, high: 13, low: 10, close: 10, volume: 200 },
  { date: 20261028, open: 10, high: 11, low: 8, close: 9, volume: 300 },
  { date: 20261029, open: 9, high: 10, low: 7, close: 9.5, volume: 150 },
];

afterEach(cleanup);

test("K 线画蜡烛（实体 + 影线）与成交量柱，而不是一条收盘折线", () => {
  const { container } = render(<KLineChart bars={bars} overlays={[]} />);
  const svg = container.querySelector('svg[aria-label="K 线"]');
  expect(svg).not.toBeNull();

  // 每根 bar：一个实体矩形 + 一条影线
  expect(svg!.querySelectorAll("g[data-candle]")).toHaveLength(4);
  expect(svg!.querySelectorAll("g[data-candle] rect")).toHaveLength(4);
  expect(svg!.querySelectorAll("g[data-candle] line")).toHaveLength(4);

  // 成交量副图：每根 bar 一根柱
  expect(svg!.querySelectorAll("rect[data-volume]")).toHaveLength(4);
});

test("少于两根 bar 时明确提示，而不是画空图", () => {
  const { container } = render(<KLineChart bars={bars.slice(0, 1)} overlays={[]} />);
  expect(container.querySelector("svg")).toBeNull();
  expect(container.textContent).toContain("没有足够的 K 线");
});

test("折半标记读的是 pool.factors.a_group_discounted，不是顶层字段", async () => {
  // 顶层放一个同名假值 + factors 里放真值：只有正确路径才会显示
  const rows = [{
    trade_date: 20261030, market: "cn_main", code: "sh600519", rank: 1, score: 82.9,
    box_days: 40, box_high: 10.14, box_low: 9.86, close: 10.04,
    box_high_raw: 10.14, box_low_raw: 9.86, close_raw: 10.04, prev_close: 10.0,
    box_vol_mean: 6e6, pos_in_box: 0.5,
    a_group_discounted: false,                       // 顶层：必须被忽略
    factors: { a_group_discounted: true },
  }];
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ trade_date: 20261030, rows }) })));
  render(<Stock code="sh600519" market="cn_main" />);
  expect(await screen.findByText(/A 组已折半/)).toBeTruthy();
});
