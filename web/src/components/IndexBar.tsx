import { colorVar } from "../lib/colors";
import type { Market } from "../lib/market";

/** 快照里的一行（`/api/quotes` 与 `/api/quote/{code}` 同一形状的子集）。 */
export type QuoteRow = {
  code: string;
  name?: string | null;
  price?: number | null;
  prev_close?: number | null;
};

/**
 * 每个市场该看哪几个指数。
 *
 * 空数组 = 该市场在快照里没有对应指数，**整条指数栏不渲染**。
 * 以前这里只有一个写死的 `sh000001`，不管切到哪个市场都显示「上证」，
 * 而且标签是硬编码的字符串、价格直接 `?? 0` —— 切到港股以后会出现
 * 「上证 0.00」，既不是港股的数，也不是真的 0。
 */
const INDEX_BY_MARKET: Record<Market, string[]> = {
  cn_main: ["sh000001", "sz399001", "sh000300"],
  cn_gem: ["sz399006"],
  cn_star: ["sh000688"],
  cn_bse: [],
  hk: [],
  us: [],
};

/** 快照里没有 `name` 时的兜底简称（比直接印代码好认）。 */
const SHORT_LABEL: Record<string, string> = {
  sh000001: "上证",
  sz399001: "深证",
  sz399006: "创业板指",
  sh000688: "科创50",
  sh000300: "沪深300",
};

/** 价格显示：**取不到就显示「—」，绝不当成 0**。 */
export function fmtPrice(price: number | null | undefined): string {
  return typeof price === "number" && Number.isFinite(price) ? price.toFixed(2) : "—";
}

/** 涨跌幅：`prev_close` 缺失或为 0 时返回 `null`（无从谈起涨跌），不返回 0。 */
export function pctOf(row: QuoteRow): number | null {
  const p = row.price;
  const prev = row.prev_close;
  if (typeof p !== "number" || typeof prev !== "number" || prev === 0) return null;
  return (p / prev - 1) * 100;
}

export function IndexBar({ rows, market }: { rows: QuoteRow[]; market: Market }) {
  const wanted = INDEX_BY_MARKET[market] ?? [];
  if (wanted.length === 0) return null;

  const byCode = new Map(rows.map((r) => [r.code, r]));
  const shown = wanted.map((code) => byCode.get(code)).filter((r): r is QuoteRow => !!r);
  // 一个都没取到就不渲染空壳（首屏还没拉到数据时，别闪一排「—」）
  if (shown.length === 0) return null;

  return (
    <div className="indexbar" aria-label="指数">
      {shown.map((r) => {
        const pct = pctOf(r);
        const label = r.name || SHORT_LABEL[r.code] || r.code;
        return (
          <span className="indexitem" key={r.code} data-code={r.code}>
            <span className="indexname">{label}</span>
            <span className="indexprice" style={pct === null ? undefined : { color: colorVar(pct) }}>
              {fmtPrice(r.price)}
            </span>
            {pct !== null && (
              <span className="indexpct" style={{ color: colorVar(pct) }}>
                {pct >= 0 ? "+" : ""}
                {pct.toFixed(2)}%
              </span>
            )}
          </span>
        );
      })}
    </div>
  );
}
