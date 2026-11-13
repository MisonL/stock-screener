/** 只读 API 的极小封装 + 轮询 hook（行情 30s、池子/自选 3s、状态 10s）。 */
import { useEffect, useRef, useState } from "react";

export async function getJson<T>(path: string): Promise<T> {
  const resp = await fetch(path, { headers: { Accept: "application/json" } });
  if (!resp.ok) throw new Error(`${path} → ${resp.status}`);
  return (await resp.json()) as T;
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`${path} → ${resp.status}`);
  return (await resp.json()) as T;
}

export function usePoll<T>(path: string, intervalMs: number): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    const tick = async () => {
      try {
        const next = await getJson<T>(path);
        if (alive.current) {
          setData(next);
          setError(null);
        }
      } catch (err) {
        if (alive.current) setError(String(err));
      }
    };
    void tick();
    const timer = setInterval(tick, intervalMs);
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
  }, [path, intervalMs]);

  return { data, error };
}
