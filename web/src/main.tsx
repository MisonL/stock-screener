import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { MARKETS, MARKET_LABEL, marketForRoute, marketFromCode, resolveMarket, setMarket, type Market } from "./lib/market";
import { NAV, currentRoute } from "./routes";
import { Home } from "./pages/Home";
import { Quotes } from "./pages/Quotes";
import { Sector } from "./pages/Sector";
import { Settings } from "./pages/Settings";
import { Auth } from "./pages/Auth";
import { Stock } from "./pages/Stock";
import { Pool } from "./pages/Pool";
import { Watch } from "./pages/Watch";
import { Signals } from "./pages/Signals";
import { AgentLog } from "./pages/AgentLog";
import { Chat } from "./pages/Chat";
import { usePoll } from "./lib/api";
import { IndexBar, type QuoteRow } from "./components/IndexBar";
import { ErrorBoundary } from "./components/ErrorBoundary";

const stockCodeOf = (route: string) => route.startsWith("/stock/") ? route.slice("/stock/".length) : null;

export function App() {
  const [route, setRoute] = useState<string>(() => currentRoute());
  const [market, setCurrent] = useState<Market>(() =>
    // 首屏就走同一份「?m= > storage > 默认」；个股页额外用代码推断市场（§8.1）
    marketForRoute(currentRoute(), new URL(window.location.href), window.localStorage),
  );

  // 路由切换的两条入口（点击导航 pushState、浏览器前进后退 popstate）共用这一份，
  // 否则从个股页点导航离开时，个股按代码推断出来的市场会被带到别的页面（P2-E）
  const applyRoute = (next: string) => {
    setRoute(next);
    if (!next.startsWith("/stock/")) {
      // 离开个股页时回到用户自己的全局市场，不把个股市场带走
      setCurrent(resolveMarket(new URL(window.location.href), window.localStorage, next));
    }
  };

  useEffect(() => {
    // 浏览器前进/后退只改 URL，不会触发 React 重渲染——必须自己听 popstate
    const onPop = () => applyRoute(currentRoute());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // 个股页跟随个股自己的市场，不跟切换器：切到港股后翻 A 股仍按 A 股规则显示
  const shownMarket = marketFromCode(stockCodeOf(route)) ?? market;

  // 指数栏按**当前市场**选指数（`IndexBar` 内部决定）；这里不请求 `?market=`
  // —— 指数在快照里的市场分类是「指数」，带上市场参数反而会被筛掉。
  const { data: quotes } = usePoll<{ rows: QuoteRow[] }>("/api/quotes", 30_000);

  const choose = (m: Market) => {
    setCurrent(m);
    const url = setMarket(m, new URL(window.location.href));
    window.history.replaceState(null, "", url.pathname + url.search);
  };

  const go = (path: string) => {
    window.history.pushState(null, "", path);
    applyRoute(path);                 // 与 popstate 同一份逻辑：市场必须一起重新解析
  };

  return (
    <>
      <header>
        <h1>选股盯盘</h1>
        <label>
          市场{" "}
          <select value={shownMarket} onChange={(e) => choose(e.target.value as Market)}>
            {MARKETS.map((m) => <option key={m} value={m}>{MARKET_LABEL[m]}</option>)}
          </select>
        </label>
      </header>

      <IndexBar rows={quotes?.rows ?? []} market={shownMarket} />

      <main>
        {/* 用当前路由做 key：切页时边界自动重置，上一页的错误不会留在下一页 */}
        <ErrorBoundary key={route}>
        {route === "/" && <Home market={shownMarket} />}
        {route === "/quotes" && <Quotes />}
        {route === "/pool" && <Pool market={shownMarket} />}
        {route === "/watch" && <Watch />}
        {route === "/sector" && <Sector />}
        {route === "/signals" && <Signals market={shownMarket} />}
        {route === "/agent-log" && <AgentLog />}
        {route === "/chat" && <Chat market={shownMarket} />}
        {route === "/settings" && <Settings />}
        {route === "/auth" && <Auth />}
        {route.startsWith("/stock/") && (
          <Stock code={route.slice("/stock/".length)} market={shownMarket} />
        )}
        </ErrorBoundary>
      </main>

      <nav>
        {NAV.map((item) => (
          <button key={item.path} onClick={() => go(item.path)}
                  className={route === item.path ? "active" : ""}>
            {item.label}
          </button>
        ))}
      </nav>
    </>
  );
}

// 测试会 import { App }：没有 #root 时不要 createRoot(null) 炸掉
const rootEl = document.getElementById("root");
if (rootEl) {
  createRoot(rootEl).render(
    <StrictMode><App /></StrictMode>,
  );
}
