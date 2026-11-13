/**
 * 日/周/月 K 线聚合（§8.1 个股详情）。
 *
 * 周线按 **ISO 周**（周一到周日）分组，不是「每 5 根 bar」：长假、停牌会让
 * 「5 根一组」跨周错位。月线按自然月分组。输出保持升序，`date` 取组内最后一根。
 */
export type KBar = {
  date: number; // YYYYMMDD
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type Period = "day" | "week" | "month";

const DAY_MS = 86_400_000;

function toUtc(date: number): Date {
  return new Date(Date.UTC(Math.floor(date / 10_000), Math.floor(date / 100) % 100 - 1, date % 100));
}

/** ISO 8601 周键：`2026-W44`。周四决定周所属年份，跨年周不会裂成两周。 */
function isoWeekKey(date: number): string {
  const d = toUtc(date);
  const dayNum = d.getUTCDay() || 7; // 周日 = 7
  d.setUTCDate(d.getUTCDate() + 4 - dayNum); // 移到本周周四
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((d.getTime() - yearStart.getTime()) / DAY_MS + 1) / 7);
  return `${d.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

/** 分组键：日线按自身日期；月线直接 `YYYYMM`。 */
export function bucketOf(date: number, period: Period): string | number {
  if (period === "month") return Math.floor(date / 100);
  if (period === "week") return isoWeekKey(date);
  return date;
}

/**
 * 聚合日 K。输入顺序无要求（先按日期升序排），不修改入参。
 * 开=组内第一根 open，收=最后一根 close，高/低=组内极值，量=求和。
 */
export function aggregate(bars: KBar[], period: Period): KBar[] {
  if (period === "day") return bars.map((b) => ({ ...b }));
  const sorted = [...bars].sort((a, b) => a.date - b.date);
  const out: KBar[] = [];
  let key: string | number | null = null;
  let cur: KBar | null = null;

  for (const b of sorted) {
    const k = bucketOf(b.date, period);
    if (cur === null || k !== key) {
      cur = { ...b };
      out.push(cur);
      key = k;
      continue;
    }
    cur.high = Math.max(cur.high, b.high);
    cur.low = Math.min(cur.low, b.low);
    cur.close = b.close;
    cur.volume += b.volume;
    cur.date = b.date; // 组内最后一根（周线=周五，月末=最后一个交易日）
  }
  return out;
}
