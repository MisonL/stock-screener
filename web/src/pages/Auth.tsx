import { useEffect, useState } from "react";

export function Auth() {
  // 每 1.2 秒轮询登录状态（D25）
  const [state, setState] = useState("waiting");
  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("token") ?? "";
    const timer = setInterval(async () => {
      const resp = await fetch(`/api/auth/status?token=${encodeURIComponent(token)}`);
      const body = await resp.json();
      setState(body.ok ? "confirmed" : body.state ?? "waiting");
    }, 1200);
    return () => clearInterval(timer);
  }, []);
  return (
    <section>
      <h2>登录</h2>
      <p>{state === "confirmed" ? "已登录" : "等待确认…"}</p>
    </section>
  );
}
