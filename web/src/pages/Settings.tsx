import { usePoll } from "../lib/api";

type SettingsBody = { strategy: Record<string, unknown>; markets: Record<string, unknown> };

export function Settings() {
  const { data } = usePoll<SettingsBody>("/api/settings", 60_000);        // D23：只读展示
  if (!data) return <p className="muted">加载中…</p>;
  return (
    <section>
      <h2>设置（只读）</h2>
      <h3>市场开关</h3>
      <pre>{JSON.stringify(data.markets, null, 2)}</pre>
      <h3>策略阈值</h3>
      <pre>{JSON.stringify(data.strategy, null, 2)}</pre>
      <p className="muted">阈值改 config/*.yaml 后重启 jobs 生效；页面不提供写入。</p>
    </section>
  );
}
