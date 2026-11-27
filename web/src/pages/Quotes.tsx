import { colorVar } from "../lib/colors";
import { usePoll } from "../lib/api";
import { Link } from "../components/Link";
import { ErrorNote, Loading } from "../components/Status";

type Quote = { code: string; name?: string; price: number; prev_close: number; volume: number };

export function Quotes() {
  // 行情列表 30 秒轮询（全市场快照本来就 30 秒一刷）
  const { data, error, state } = usePoll<{ as_of: string | null; rows: Quote[] }>(
    "/api/quotes", 30_000);
  const rows = data?.rows ?? [];

  return (
    <section>
      <h2>
        行情{" "}
        {data?.as_of && <small className="muted">{data.as_of}</small>}
      </h2>

      {state === "loading" && <Loading label="正在拉取行情…" />}
      {state === "error" && error && <ErrorNote error={error} what="行情" />}

      {state === "ok" && rows.length === 0 && (
        <p className="muted empty">快照还没到（数据源本轮可能没返回）</p>
      )}

      {rows.length > 0 && (
        <table>
          <thead>
            <tr><th>代码</th><th>名称</th><th>现价</th><th>涨跌</th></tr>
          </thead>
          <tbody>
            {rows.map((q) => {
              // `prev_close` 缺失时**不说「0.00%」**：那是「没算过」，不是「没涨没跌」。
              const chg = q.prev_close ? (q.price / q.prev_close - 1) * 100 : null;
              const color = chg === null ? undefined : { color: colorVar(chg) };
              return (
                <tr key={q.code}>
                  <td><Link to={`/stock/${q.code}`}>{q.code}</Link></td>
                  <td>{q.name ?? ""}</td>
                  <td style={color}>{typeof q.price === "number" ? q.price.toFixed(2) : "—"}</td>
                  <td style={color}>{chg === null ? "—" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
