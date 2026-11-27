/**
 * 站内跳转的**唯一入口**。
 *
 * 为什么需要它：`main.tsx` 用 `pushState` 做客户端路由，而列表里的
 * `<a href="/stock/xxx">` 是**浏览器原生导航** —— 点一下整个应用重载：
 * 重新下载 bundle、重跑 `App()`、让每个页面的 `usePoll` 从头再拉一遍，
 * 滚动位置也丢。手机上就是「点了 → 白屏 → 等」。
 *
 * 这里把点击和浏览器前进/后退并成**同一条通道**：`main.tsx` 只监听
 * `popstate`，点击时补发一个同名事件。两条路径共用一份处理逻辑，
 * 否则「离开个股页要恢复全局市场」这类规则只会在其中一条上生效。
 */
export function navigate(to: string): void {
  const here = window.location.pathname + window.location.search;
  if (to === here) return;              // 点当前页：不产生无意义的历史记录
  window.history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
