import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode };
type State = { error: Error | null };

/**
 * 单个页面出错时，**只毁这一页，不毁整站**。
 *
 * 为什么必须有：React 18 里一个页面组件抛错，`createRoot` 会把**整棵树**
 * 卸载——顶栏、导航、其他页面全没了，用户看到纯白屏，没有任何提示。
 * 2026-11-27 实测过一次：板块页读到一个不存在的字段，`undefined.toFixed()`
 * 抛错，点"板块"之后整个应用白屏（不是这一页空，是连导航都没了），
 * 用户只能刷新，还可能刷新后继续白屏。
 *
 * 这里兜住渲染错误，把错误**显示出来**并给一个恢复入口。
 * 注意：错误边界只能捕获渲染/生命周期里的错误，`onClick` 里的异步异常
 * 捕获不到——那些靠各页面自己的 try/catch（见 Watch/Stock）。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 控制台留一份完整栈，方便对照"用户看到的那一句"
    console.error("[页面渲染失败]", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="errbox" role="alert">
        <strong>这一页渲染失败了</strong>
        <code>{error.message}</code>
        <span className="muted">
          其他页面仍然可用。点下面的按钮重试；如果反复出现，说明这个页面的数据
          和代码对不上（字段缺失或被改过）。
        </span>
        <button onClick={() => this.setState({ error: null })}>重试这一页</button>
      </div>
    );
  }
}
