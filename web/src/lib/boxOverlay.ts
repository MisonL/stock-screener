/**
 * 把观察池的箱体画到 K 线上。
 *
 * **必须用 `box_high_raw / box_low_raw`**：详情页默认 `adjust=raw`（§7.5.2），
 * 复权价与 raw 价差着几倍因子，用 adj 画出来的箱体会浮在图表外。
 */
export type PoolRow = {
  trade_date: number;
  market: string;
  code: string;
  rank: number;
  score: number;
  factors: Record<string, number>;
  box_days: number;
  box_high_adj: number;
  box_low_adj: number;
  box_high_raw: number;
  box_low_raw: number;
  close_raw: number;
  box_vol_mean: number;
  pos_in_box: number;
  a_group_discounted?: number | boolean;
  b4_degenerate?: number | boolean;
};

export type KBar = { date: number; open: number; high: number; low: number; close: number; volume: number };
export type Overlay = { kind: "hline"; value: number; label: string };

export function boxOverlay(pool: PoolRow, bars: KBar[]): Overlay[] {
  if (!pool || bars.length === 0) return [];
  void bars;
  return [
    { kind: "hline", value: pool.box_high_raw, label: "箱顶" },
    { kind: "hline", value: pool.box_low_raw, label: "箱底" },
  ];
}

/** 现价在箱体里的位置（0 = 箱底，1 = 箱顶）；箱体退化时返回 null。 */
export function posInBox(pool: Pick<PoolRow, "box_high_raw" | "box_low_raw">, price: number): number | null {
  const span = pool.box_high_raw - pool.box_low_raw;
  if (!(span > 0)) return null;
  return (price - pool.box_low_raw) / span;
}
