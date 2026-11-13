import { useState } from "react";
import { changeColor } from "../lib/colors";
import { usePoll, getJson } from "../lib/api";

type SectorRow = { code: string; name: string; chg: number | null; members: number | null };
type Member = { code: string; name: string | null };

export function Sector() {
  const { data } = usePoll<{ rows: SectorRow[] }>("/api/sector", 15_000);   // D23
  const [picked, setPicked] = useState<string | null>(null);
  const [members, setMembers] = useState<Member[]>([]);

  const open = async (code: string) => {
    setPicked(code);
    const body = await getJson<{ rows: Member[] }>(`/api/sector/${code}/members`);
    setMembers(body.rows);
  };

  return (
    <section>
      <h2>板块</h2>
      <ul className="list">
        {(data?.rows ?? []).map((s) => (
          <li key={s.code} onClick={() => open(s.code)}>
            <span>{s.name}</span>
            <span style={{ color: `var(${changeColor(s.chg ?? 0)})` }}>
              {(s.chg ?? 0).toFixed(2)}%
            </span>
            <span className="muted">{s.members ?? 0} 只</span>
          </li>
        ))}
      </ul>
      {picked && (
        <>
          <h3>成分股</h3>
          <ul className="list">
            {members.map((m) => (
              <li key={m.code}><a href={`/stock/${m.code}`}>{m.code} {m.name}</a></li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
