import type { AnchorHTMLAttributes, MouseEvent, ReactNode } from "react";
import { navigate } from "../lib/nav";

type Props = { to: string; children: ReactNode } &
  Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href">;

/**
 * 站内链接：语义仍是 `<a href>`，但**普通左键点击走客户端路由**。
 *
 * 保留 `href` 而不是换成 `<div onClick>`，因为这四件事只有真链接能给：
 * 中键/⌘ 点击开新标签、右键「复制链接地址」、屏幕阅读器播报「链接」、
 * 以及 JS 还没加载完时也能跳转。
 */
export function Link({ to, children, onClick, ...rest }: Props) {
  const handle = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    // 已被上游 `preventDefault`、或这次点击带着「开新窗口/下载」的意图时，
    // 交还给浏览器 —— 抢过来反而会破坏用户预期。
    if (e.defaultPrevented) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigate(to);
  };
  return (
    <a href={to} onClick={handle} {...rest}>
      {children}
    </a>
  );
}
