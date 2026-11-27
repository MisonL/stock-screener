import { useState } from "react";
import { colorVar } from "../lib/colors";
import { usePoll, getJson } from "../lib/api";
import { Link } from "../components/Link";
import { ErrorNote, Loading } from "../components/Status";

type SectorRow = { code: string; name: string; chg: number | null; members: number | null };
type Member = { code: string; name: string | null };

export function Sector() {
  const { data, error, state } = usePoll<{ rows: SectorRow[] }>("/api/sector", 15_000);   // D23
  const [picked, setPicked] = useState<string | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [memberLoading, setMemberLoading] = useState(false);

  const open = async (code: string) => {
    // 再点一次收起：展开后没法关掉是很烦的一件事
    if (picked === code) {
      setPicked(null);
      return;
    }
    setPicked(code);
    setMembers([]);
    setMemberLoading(true);
    try {
      const body = await getJson<{ rows: Member[] }>(`/api/sector/${code}/members`);
      setMembers(body.rows);
    } finally {
      setMemberLoading(false);
    }
  };

  const rows = data?.rows ?? [];

  return (
    <section>
      <h2>板块</h2>

      {state === "loading" && <Loading label="正在拉取板块…" />}
      {state === "error" && error && <ErrorNote error={error} what="板块" />}
      {state === "ok" && rows.length === 0 && <p className="muted empty">今天还没有板块数据</p>}

      <ul className="list">
        {rows.map((s) => {
          // **只能用 `Number.isFinite` 判"有没有数值"**。
          // 接口里的字段可能是 `null`、可能整个键都不存在（`undefined`）、
          // 也可能是 NaN——`x === null` 挡不住后两种，`x.toFixed()` 照样抛。
          // 2026-11-27 实测：`/api/sector` 返回的是 `pct` 而不是 `chg`，
          // 于是这里 `undefined.toFixed(2)` 抛错 → React 卸载整棵树 →
          // **点"板块"整个应用白屏**（不是这一页空，是整站没了）。
          const chg = Number.isFinite(s.chg) ? (s.chg as number) : null;
          return (
            <li key={s.code}
                onClick={() => open(s.code)}
                className={`clickable${picked === s.code ? " selected" : ""}`}
                title="点击查看成分股">
              <span>{s.name}</span>
              <span style={chg === null ? undefined : { color: colorVar(chg) }}>
                {chg === null ? "—" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`}
              </span>
              <span className="muted">{s.members ?? 0} 只</span>
            </li>
          );
        })}
      </ul>

      {picked && (
        <>
          <h3>成分股</h3>
          {memberLoading && <Loading label="正在拉成分股…" />}
          {!memberLoading && members.length === 0 && <p className="muted empty">这个板块没有成分股数据</p>}
          <ul className="list">
            {members.map((m) => (
              <li key={m.code}>
                <Link to={`/stock/${m.code}`}>{m.code} {m.name}</Link>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
