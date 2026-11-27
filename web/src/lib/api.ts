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

/**
 * 取数处于哪一步。
 *
 * **`loading` 和「确实没有数据」必须分开**：以前两者都表现为 `data === null`
 * + 空数组，于是首屏还没拉到东西时，页面照常渲染空态，写出「今天还没有池子」
 * ——用户看到的就是「系统说今天没选股」，而真相只是请求还在路上。
 * 这类「没取到 ≠ 没有」的错报比报错更糟：它不响，也没人去查。
 */
export type PollState = "loading" | "ok" | "error";

export function usePoll<T>(
  path: string,
  intervalMs: number,
): { data: T | null; error: string | null; state: PollState } {
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

  // 已有一份数据后又失败：仍算 `ok`（旧数据继续显示），error 另行提示，
  // 不让一次抖动把整页内容擦成空白。
  const state: PollState = data !== null ? "ok" : error !== null ? "error" : "loading";
  return { data, error, state };
}
