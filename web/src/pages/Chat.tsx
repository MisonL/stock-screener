import { useState } from "react";
import { postJson } from "../lib/api";

type Answer = {
  session_id: number;
  text: string;
  as_of: string | null;
  market: string;
  fallback: boolean;
  grounded: { ungrounded: boolean; failed: boolean };
};

type Line = { role: "user" | "assistant"; text: string; answer?: Answer };

/**
 * AI 对话（§7.4 / §7.5.3）。
 *
 * 三条界面规则都是有原因的：
 * - **未溯源段落灰显**：每个数字都必须能追到本轮工具返回，追不到的那句要肉眼可辨；
 * - **降级要显式**：`fallback=true` 时首行是「AI 暂时不可用」，不能和正常回答长得一样；
 * - **`as_of` 与市场要露出来**：用户得知道看的是哪一刻、哪个市场的数据。
 */
export function Chat({ market }: { market: string }) {
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send() {
    const question = text.trim();
    if (!question || busy) return;
    setBusy(true);
    setError(null);
    setLines((prev) => [...prev, { role: "user", text: question }]);
    setText("");
    try {
      const answer = await postJson<Answer>("/api/chat", {
        session_id: sessionId,
        text: question,
        market,
      });
      setSessionId(answer.session_id);
      setLines((prev) => [...prev, { role: "assistant", text: answer.text, answer }]);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h2>对话</h2>
      <ul className="list">
        {lines.map((l, i) => (
          <li key={i} className={l.role === "assistant" ? "chat-a" : "chat-q"}>
            <div className={l.answer?.grounded.failed ? "chat-ungrounded" : undefined}>
              {l.text}
            </div>
            {l.answer && (
              <div className="muted">
                {l.answer.fallback ? "已降级为直接查询 · " : ""}
                {l.answer.market}
                {l.answer.as_of ? ` · 数据 ${l.answer.as_of.slice(11, 19)}` : ""}
                {l.answer.grounded.failed ? " · 部分数字未通过校验（已灰显）" : ""}
              </div>
            )}
          </li>
        ))}
        {lines.length === 0 && <li className="muted">问点什么，比如「600519 现在什么情况」。</li>}
      </ul>
      {error && <p style={{ color: "var(--bad)" }}>{error}</p>}
      <div style={{ display: "flex", gap: 8 }}>
        <input
          aria-label="问题"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="600519 现在什么情况"
          style={{ flex: 1 }}
        />
        <button onClick={send} disabled={busy}>
          {busy ? "思考中…" : "发送"}
        </button>
      </div>
    </section>
  );
}
