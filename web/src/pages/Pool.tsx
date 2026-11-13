import { Fragment, useState } from "react";
import { usePoll } from "../lib/api";

type Row = {
  code: string; rank: number; score: number; box_days: number; close: number;
  box_high: number; box_low: number; pos_in_box: number; factors: string | Record<string, number>;
};

export function Pool({ market }: { market: string }) {
  const { data } = usePoll<{ trade_date: number | null; rows: Row[] }>(
    `/api/pool?market=${market}`, 3_000);
  const [open, setOpen] = useState<string | null>(null);

  const factorsOf = (raw: Row["factors"]) =>
    typeof raw === "string" ? (JSON.parse(raw || "{}") as Record<string, number>) : raw;

  return (
    <section>
      <h2>观察池 {data?.trade_date ?? ""}</h2>
      <table>
        <thead>
          <tr><th>#</th><th>代码</th><th>总分</th><th>箱体</th><th>位置</th></tr>
        </thead>
        <tbody>
          {(data?.rows ?? []).map((r) => {
            const f = factorsOf(r.factors);
            const discounted = Boolean(f.a_group_discounted);
            return (
              <Fragment key={r.code}>
                <tr onClick={() => setOpen(open === r.code ? null : r.code)}
                    style={discounted ? { background: "#f1f3f4" } : undefined}
                    title={discounted ? "A 组已折半" : undefined}>
                  <td>{r.rank}</td>
                  <td><a href={`/stock/${r.code}`}>{r.code}</a></td>
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
    </section>
  );
}
