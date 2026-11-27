import { expect, test } from "vitest";
import { boxOverlay, posInBox, type PoolRow } from "./boxOverlay";

const bars = [{ date: 20261030, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 }];
const row = (over: Partial<PoolRow> = {}): PoolRow => ({
  trade_date: 20261029, market: "cn_main", code: "sh600519", rank: 1, score: 86,
  factors: { b4: 0.41 }, box_days: 40,
  box_high_adj: 30, box_low_adj: 27, box_high_raw: 10, box_low_raw: 9,
  close_raw: 9.5, box_vol_mean: 1e6, pos_in_box: 0.5, ...over,
});

test("overlay uses raw box", () => {
  expect(boxOverlay(row(), bars)[0].value).toBe(10);
  expect(boxOverlay(row(), bars)[1].value).toBe(9);
});

test("empty bars gives no overlay", () => {
  expect(boxOverlay(row(), [])).toEqual([]);
});

test("pos in box", () => {
  expect(posInBox(row(), 9.5)).toBe(0.5);
  expect(posInBox(row({ box_high_raw: 10, box_low_raw: 10 }), 10)).toBeNull();
});

// ---- 缺字段必须不产出 overlay（2026-11-27 实机白屏）--------------------
test("箱体字段缺失 → 不画箱体线，而不是把 undefined 传下去", () => {
  // 实机复现：`/api/pool` 的行缺 `box_high_raw` 时，旧实现返回
  // `[{value: undefined}, ...]`，下游 `o.value.toFixed(2)` 抛异常，
  // **整个个股页面白屏**；同时 `Math.max(..., undefined)` = NaN
  // 会把整张 K 线的坐标轴也毁掉（图不崩，但一片空白）。
  const missing = row({ box_high_raw: undefined as unknown as number });
  expect(boxOverlay(missing, bars)).toEqual([]);

  const nulled = row({ box_low_raw: null as unknown as number });
  expect(boxOverlay(nulled, bars)).toEqual([]);

  const nan = row({ box_high_raw: NaN });
  expect(boxOverlay(nan, bars)).toEqual([]);
});

test("两条箱体线要么都画、要么都不画", () => {
  // 只画一条会让人以为另一条在图外，属于"看起来对但说错了"
  expect(boxOverlay(row({ box_low_raw: undefined as unknown as number }), bars)).toHaveLength(0);
  expect(boxOverlay(row(), bars)).toHaveLength(2);
});

test("每个 overlay 的 value 都是有限数字（下游可直接算坐标）", () => {
  for (const o of boxOverlay(row(), bars)) {
    expect(Number.isFinite(o.value)).toBe(true);
  }
});
