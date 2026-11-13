import { changeColor } from "../lib/colors";
import { usePoll } from "../lib/api";

type Quote = { code: string; name?: string; price: number; prev_close: number; volume: number };

export function Quotes() {
  // 行情列表 30 秒轮询（全市场快照本来就 30 秒一刷）
  const { data } = usePoll<{ as_of: string | null; rows: Quote[] }>("/api/quotes", 30_000);
  const rows = data?.rows ?? [];
  return (
    <section>
      <h2>行情 {data?.as_of && <small className="muted">{data.as_of}</small>}</h2>
      <table>
        <thead>
          <tr><th>代码</th><th>名称</th><th>现价</th><th>涨跌</th></tr>
        </thead>
        <tbody>
          {rows.map((q) => {
            const chg = q.prev_close ? ((q.price / q.prev_close - 1) * 100) : 0;
            return (
              <tr key={q.code}>
                <td><a href={`/stock/${q.code}`}>{q.code}</a></td>
                <td>{q.name ?? ""}</td>
                <td style={{ color: `var(${changeColor(chg)})` }}>{q.price?.toFixed(2)}</td>
                <td style={{ color: `var(${changeColor(chg)})` }}>{chg.toFixed(2)}%</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 && <p className="muted">快照还没到</p>}
    </section>
  );
}
