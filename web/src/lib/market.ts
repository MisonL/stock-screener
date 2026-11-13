/** 全局市场切换（§8.1）：`?m=` > `localStorage['market']` > `cn_main`。 */

export type Market = "cn_main" | "cn_gem" | "cn_star" | "cn_bse" | "hk" | "us";

export const MARKETS: Market[] = ["cn_main", "cn_gem", "cn_star", "cn_bse", "hk", "us"];

export const MARKET_LABEL: Record<Market, string> = {
  cn_main: "沪主板",
  cn_gem: "创业板",
  cn_star: "科创板",
  cn_bse: "北交所",
  hk: "港股",
  us: "美股",
};

const STORAGE_KEY = "market";

function asMarket(v: string | null | undefined): Market | null {
  return v && (MARKETS as string[]).includes(v) ? (v as Market) : null;
}

/**
 * 决定当前市场。
 *
 * 个股详情页返回**该股自己的市场**，不跟切换器——否则切到港股后翻一只 A 股，
 * 页面会用港股的规则去显示涨跌停和配色。`stockMarket` 由路由层从代码推断。
 */
export function resolveMarket(
  url: URL,
  storage: Storage,
  route: string,
  stockMarket?: string,
): Market {
  if (route.startsWith("/stock/") && asMarket(stockMarket)) return stockMarket as Market;
  return asMarket(url.searchParams.get("m")) ?? asMarket(storage.getItem(STORAGE_KEY)) ?? "cn_main";
}

/**
 * 从带交易所前缀的代码推断市场（`sh600519`/`sz300750`/`sh688981`/`bj...`/`hk...`）。
 *
 * 六位裸代码不猜（和 `core.market.classify_market` 同一口径）；推断不出返回 null，
 * 调用方继续用切换器的当前市场。
 */
export function marketFromCode(code: string | null | undefined): Market | null {
  const c = (code ?? "").trim().toLowerCase();
  if (c.startsWith("sh")) {
    const rest = c.slice(2);
    if (rest.startsWith("688") || rest.startsWith("689")) return "cn_star";
    if (rest.startsWith("60")) return "cn_main";
    return null;
  }
  if (c.startsWith("sz")) {
    const rest = c.slice(2);
    if (rest.startsWith("30")) return "cn_gem";
    if (rest.startsWith("00")) return "cn_main";
    return null;
  }
  if (c.startsWith("bj")) return "cn_bse";
  if (c.startsWith("hk")) return "hk";
  if (c.startsWith("us")) return "us";
  return null;
}

/** 路由层入口：个股页用代码推断市场，其余页面用 `?m=` > storage > 默认。 */
export function marketForRoute(route: string, url: URL, storage: Storage): Market {
  const code = route.startsWith("/stock/") ? route.slice("/stock/".length) : null;
  return resolveMarket(url, storage, route, marketFromCode(code) ?? undefined);
}

/** 同时写 `localStorage` 与 `?m=`：刷新页面后仍然是这个市场，分享出去的链接也是。 */
export function setMarket(m: Market, url: URL = new URL(window.location.href), storage?: Storage): URL {
  (storage ?? window.localStorage).setItem(STORAGE_KEY, m);
  url.searchParams.set("m", m);
  return url;
}
