import { useState } from "react";
import { getJson, postJson, usePoll } from "../lib/api";
import { changeColor } from "../lib/colors";

type Watch = { code: string; added_at: string; note: string | null; pct_threshold: number | null };
type Quote = { code: string; price: number; prev_close: number };

export function Watch() {
  const { data } = usePoll<{ rows: Watch[] }>("/api/watch", 3_000);
  const { data: quotes } = usePoll<{ rows: Quote[] }>("/api/quotes", 30_000);
  const [code, setCode] = useState("");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const priceOf = (c: string) => (quotes?.rows ?? []).find((q) => q.code === c);

  const add = async () => {
    try {
      await postJson("/api/watch", { code, note });
      setCode(""); setNote(""); setMsg("已加入");
    } catch (err) {
      setMsg(`失败：${String(err)}`);          // 同源校验失败会走到这里（400）
    }
  };

  const remove = async (c: string) => {
    await fetch(`/api/watch/${c}`, { method: "DELETE" });
    setMsg("已删除");
  };

  return (
    <section>
      <h2>自选股</h2>
      <div className="statusbar">
        <input placeholder="sh600519" value={code} onChange={(e) => setCode(e.target.value)} />
        <input placeholder="备注" value={note} onChange={(e) => setNote(e.target.value)} />
        <button onClick={add} disabled={code.length < 6}>加入</button>
        {msg && <span className="muted">{msg}</span>}
      </div>

      <ul className="list">
        {(data?.rows ?? []).map((w) => {
          const q = priceOf(w.code);
          const chg = q && q.prev_close ? (q.price / q.prev_close - 1) * 100 : 0;
          return (
            <li key={w.code}>
              <a href={`/stock/${w.code}`}>{w.code}</a>
              <span style={{ color: `var(${changeColor(chg)})` }}>{chg.toFixed(2)}%</span>
              <span className="muted">{w.note ?? ""} {w.pct_threshold ? `阈值 ${w.pct_threshold}%` : ""}</span>
              <button onClick={() => remove(w.code)}>删</button>
            </li>
          );
        })}
        {(data?.rows ?? []).length === 0 && <li className="muted">还没有自选</li>}
      </ul>
    </section>
  );
}
