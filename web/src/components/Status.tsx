import type { PollState } from "../lib/api";

/**
 * 首屏加载中的占位。
 *
 * `role="status"` + `aria-live="polite"`：屏幕阅读器会播报「加载中」，
 * 而不是让视障用户对着一片空白猜页面是不是坏了。
 */
export function Loading({ label = "加载中…" }: { label?: string }) {
  return (
    <p className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}
    </p>
  );
}

/** 错误提示。给出**具体是哪个接口、什么错**，不要只说「出错了」。 */
export function ErrorNote({ error, what }: { error: string; what?: string }) {
  return (
    <div className="errbox" role="alert">
      <strong>{what ? `${what}取数失败` : "取数失败"}</strong>
      <code>{error}</code>
      <span className="muted">（页面会自动重试；持续失败请查看「设置」里的服务状态）</span>
    </div>
  );
}

/**
 * 「列表空着」的两种情况必须分开渲染。
 *
 * 返回 `null` 表示**现在不该渲染空态**（还在加载、或已经报错）；
 * 调用方拿到 `null` 就什么都别画，把位置留给 `<Loading />` / `<ErrorNote />`。
 */
export function emptyHint(state: PollState, text: string) {
  if (state !== "ok") return null;
  return <li className="muted empty">{text}</li>;
}
