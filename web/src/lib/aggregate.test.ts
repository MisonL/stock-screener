import { describe, expect, test } from "vitest";
import { aggregate, bucketOf, type KBar } from "./aggregate";

/** 一行日 K；`date` 是 YYYYMMDD 整数。 */
const bar = (date: number, o: number, h: number, l: number, c: number, volume = 100): KBar => ({
  date, open: o, high: h, low: l, close: c, volume,
});

// 2026-10-26 是周一，10-30 是周五；10-31 周六（不属于任何交易周）
const week = [
  bar(20261026, 10, 11, 9.5, 10.5, 1),
  bar(20261027, 10.5, 12, 10, 11, 2),
  bar(20261028, 11, 11.5, 8, 9, 3),
  bar(20261029, 9, 10, 8.5, 9.8, 4),
  bar(20261030, 9.8, 10.2, 9.7, 10, 5),
];

describe("aggregate", () => {
  test("weekly: first open = Monday open, high = max, close = Friday close", () => {
    const [w] = aggregate(week, "week");
    expect(w.open).toBe(10); // 周一 open
    expect(w.high).toBe(12); // 周内最高
    expect(w.low).toBe(8); // 周内最低
    expect(w.close).toBe(10); // 周五 close
    expect(w.volume).toBe(15); // 成交量求和
    expect(w.date).toBe(20261030); // 取本周最后一根
  });

  test("weekend rows fold into the preceding ISO week", () => {
    const out = aggregate([...week, bar(20261031, 20, 20, 20, 20, 1)], "week");
    expect(out).toHaveLength(1);
    expect(out[0].close).toBe(20);
  });

  test("ISO week across year boundary (2025-12-29 Mon + 2026-01-02 Fri is one week)", () => {
    const out = aggregate([bar(20251229, 1, 2, 1, 1.5, 1), bar(20260102, 1.5, 3, 1.4, 2.5, 1)], "week");
    expect(out).toHaveLength(1);
    expect(out[0].open).toBe(1);
    expect(out[0].close).toBe(2.5);
  });

  test("monthly: groups by calendar month", () => {
    const out = aggregate(
      [bar(20260130, 1, 2, 0.5, 1.5, 1), bar(20260202, 1.5, 3, 1, 2, 2), bar(20260227, 2, 2.5, 1.5, 2.2, 3)],
      "month",
    );
    expect(out).toHaveLength(2);
    expect(out[0]).toMatchObject({ open: 1, close: 1.5, date: 20260130 });
    expect(out[1]).toMatchObject({ open: 1.5, high: 3, low: 1, close: 2.2, volume: 5, date: 20260227 });
  });

  test("day keeps bars untouched", () => {
    expect(aggregate(week, "day")).toEqual(week);
  });

  test("bucketOf: ISO week and month keys", () => {
    expect(bucketOf(20261031, "week")).toBe(bucketOf(20261026, "week"));
    expect(bucketOf(20261026, "week")).not.toBe(bucketOf(20261102, "week"));
    expect(bucketOf(20260201, "month")).toBe(202602);
    expect(bucketOf(20260201, "month")).not.toBe(bucketOf(20260301, "month"));
  });
});
