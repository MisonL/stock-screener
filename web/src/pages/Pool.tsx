import { Fragment, useState } from "react";
import { usePoll } from "../lib/api";
import { Link } from "../components/Link";
import { ErrorNote, Loading } from "../components/Status";

type Row = {
  code: string; rank: number; score: number; box_days: number; close: number;
  box_high: number; box_low: number; pos_in_box: number; factors: string | Record<string, number>;
};

export function Pool({ market }: { market: string }) {
  const { data, error, state } = usePoll<{ trade_date: number | null; rows: Row[] }>(
    `/api/pool?market=${market}`, 3_000);
  const [open, setOpen] = useState<string | null>(null);

  const factorsOf = (raw: Row["factors"]) =>
    typeof raw === "string" ? (JSON.parse(raw || "{}") as Record<string, number>) : raw;

  const rows = data?.rows ?? [];

  return (
    <section>
      <h2>观察池 {data?.trade_date ?? ""}</h2>

      {state === "loading" && <Loading label="正在拉取观察池…" />}
      {state === "error" && error && <ErrorNote error={error} what="观察池" />}
      {/* 「今天没有」是结论，只能在一份**成功拿到**的数据上说得出口 */}
      {state === "ok" && rows.length === 0 && (
        <p className="muted empty">今天还没有池子（选股任务可能还没跑）</p>
      )}

      {rows.length > 0 && (
        <table>
          <thead>
            <tr><th>#</th><th>代码</th><th>总分</th><th>箱体</th><th>位置</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const f = factorsOf(r.factors);
              const discounted = Boolean(f.a_group_discounted);
              return (
                <Fragment key={r.code}>
                  <tr onClick={() => setOpen(open === r.code ? null : r.code)}
                      className={discounted ? "discounted" : undefined}
                      style={discounted ? { background: "#f1f3f4" } : undefined}
                      title={discounted ? "A 组已折半" : "点击查看因子明细"}>
                    <td>{r.rank}</td>
                    <td><Link to={`/stock/${r.code}`}>{r.code}</Link></td>
                    <td>{r.score?.toFixed(1)}</td>
                    <td>{r.box_days} 日</td>
                    <td>{(r.pos_in_box * 100).toFixed(0)}%</td>
                  </tr>
                  {open === r.code && (
                    <tr>
                      <td colSpan={5}>
                        <pre>{JSON.stringify(f, null, 2)}</pre>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
