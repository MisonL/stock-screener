/** 路由表：底部导航与 `/auth` 都由它驱动（测试直接断言这份清单）。 */
export const routes = ["/", "/quotes", "/pool", "/watch", "/sector", "/signals", "/agent-log",
  "/chat", "/settings", "/auth"] as const;

export type Route = (typeof routes)[number];

/** 当前 URL → 前端路由。`popstate` 与首屏共用这一份，避免两套判断漂移。 */
export function currentRoute(): string {
  const path = window.location.pathname;
  if (path.startsWith("/stock/")) return path;
  if (path === "/auth") return "/auth";
  if (path.startsWith("/sector")) return "/sector";
  if (path.startsWith("/settings")) return "/settings";
  if (path.startsWith("/quotes")) return "/quotes";
  if (path.startsWith("/pool")) return "/pool";
  if (path.startsWith("/signals")) return "/signals";
  if (path.startsWith("/agent-log")) return "/agent-log";
  if (path.startsWith("/chat")) return "/chat";
  if (path.startsWith("/watch")) return "/watch";
  return "/";
}

export const NAV: { path: Route; label: string }[] = [
  { path: "/", label: "首页" },
  { path: "/quotes", label: "行情" },
  { path: "/pool", label: "观察池" },
  { path: "/watch", label: "自选" },
  { path: "/sector", label: "板块" },
  { path: "/signals", label: "信号" },
  { path: "/agent-log", label: "日志" },
  { path: "/chat", label: "对话" },
  { path: "/settings", label: "设置" },
];
