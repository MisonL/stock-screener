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
