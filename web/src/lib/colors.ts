/**
 * 全局配色：**红涨绿跌**（§8.1）。
 *
 * `market` 参数保留给将来做「按市场配色」的开关，当前所有市场口径一致——
 * 不要按欧美习惯把港美股反过来：同一屏上红绿含义不一致是最难用的一种「特性」。
 */
export function changeColor(pct: number, market?: string): "--up" | "--down" | "--flat" {
  void market;
  if (pct > 0) return "--up";
  if (pct < 0) return "--down";
  return "--flat";
}

/** CSS 变量名 → 可直接用于 style 的 `var(--x)`。 */
export function colorVar(pct: number, market?: string): string {
  return `var(${changeColor(pct, market)})`;
}
