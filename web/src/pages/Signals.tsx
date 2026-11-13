import { useState } from "react";
import { usePoll } from "../lib/api";
import { changeColor } from "../lib/colors";

type Signal = { id: number; ts: string; code: string; kind: string; level: string; read_at: string | null };

/** 突破与跌破分两栏（§8.3）：一栏混排时，风险信号会被机会信号淹掉。 */
export function Signals({ market }: { market: string }) {
  const { data } = usePoll<{ rows: Signal[] }>(`/api/signals?market=${market}`, 10_000);
  const rows = data?.rows ?? [];
  const breakout = rows.filter((r) => r.kind === "breakout");
  const breakdown = rows.filter((r) => r.kind === "breakdown");

  const Column = ({ title, items }: { title: string; items: Signal[] }) => (
    <div style={{ flex: 1, minWidth: 280 }}>
      <h3>{title}（{items.length}）</h3>
      <ul className="list">
        {items.map((s) => (
          <li key={s.id}>
            <a href={`/stock/${s.code}`}>{s.code}</a>
            <span style={{ color: `var(${changeColor(title === "突破" ? 1 : -1)})` }}>{s.level}</span>
            <span className="muted">{s.ts.slice(11, 19)}{s.read_at ? " · 已读" : ""}</span>
          </li>
        ))}
        {items.length === 0 && <li className="muted">无</li>}
      </ul>
    </div>
  );

  return (
    <section>
      <h2>信号</h2>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <Column title="突破" items={breakout} />
        <Column title="跌破" items={breakdown} />
      </div>
    </section>
  );
}
