import { usePoll } from "../lib/api";
import { colorVar } from "../lib/colors";
import { Link } from "../components/Link";
import { ErrorNote, Loading } from "../components/Status";

type Signal = { id: number; ts: string; code: string; kind: string; level: string; read_at: string | null };

/** 突破与跌破分两栏（§8.3）：一栏混排时，风险信号会被机会信号淹掉。 */
export function Signals({ market }: { market: string }) {
  const { data, error, state } = usePoll<{ rows: Signal[] }>(
    `/api/signals?market=${market}`, 10_000);
  const rows = data?.rows ?? [];
  const breakout = rows.filter((r) => r.kind === "breakout");
  const breakdown = rows.filter((r) => r.kind === "breakdown");

  const Column = ({ title, items }: { title: string; items: Signal[] }) => (
    <div className="col" style={{ flex: 1, minWidth: 280 }}>
      <h3>{title}（{items.length}）</h3>
      <ul className="list">
        {items.map((s) => (
          <li key={s.id}>
            <Link to={`/stock/${s.code}`}>{s.code}</Link>
            <span style={{ color: colorVar(title === "突破" ? 1 : -1) }}>{s.level}</span>
            <span className="muted">
              {s.ts.slice(11, 19)}
              {s.read_at ? " · 已读" : ""}
            </span>
          </li>
        ))}
        {/* 加载中不写「无」——那会被读成「今天没有信号」 */}
        {items.length === 0 && state === "ok" && <li className="muted empty">无</li>}
      </ul>
    </div>
  );

  return (
    <section>
      <h2>信号</h2>
      {state === "loading" && <Loading label="正在拉取信号…" />}
      {state === "error" && error && <ErrorNote error={error} what="信号" />}
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <Column title="突破" items={breakout} />
        <Column title="跌破" items={breakdown} />
      </div>
    </section>
  );
}
