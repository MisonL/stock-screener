import { useEffect, useMemo, useState } from "react";
import { getJson, usePoll } from "../lib/api";
import { aggregate, type KBar, type Period } from "../lib/aggregate";
import { changeColor } from "../lib/colors";
import { boxOverlay, posInBox, type PoolRow } from "../lib/boxOverlay";

type Quote = { code: string; price: number; prev_close: number; volume: number; up?: number; down?: number };

/** 每个周期一次拉多少根**日** K：周/月由客户端聚合，多拉一些才够画。 */
const PERIOD_DAYS: Record<Period, number> = { day: 240, week: 480, month: 1700 };

const W = 720;
const H = 340;
const PRICE_H = 250;      // 主图（蜡烛）高度
const VOL_TOP = 268;      // 成交量副图起点
const VOL_H = 60;
const PAD = 40;

/**
 * K 线图：蜡烛（开高低收 + 影线）+ 成交量柱 + 箱体水平线。
 *
 * 几十~上千根 bar 用 SVG 画，不引图表库——体积与离线可用性都更好。
 * 颜色沿用全局红涨绿跌（§8.1），实体按 `close >= open` 上色。
 */
export function KLineChart({ bars, overlays }: { bars: KBar[]; overlays: { value: number; label: string }[] }) {
  if (bars.length < 2) return <p className="muted">没有足够的 K 线</p>;

  const top = Math.max(...bars.map((b) => b.high), ...overlays.map((o) => o.value));
  const bottom = Math.min(...bars.map((b) => b.low), ...overlays.map((o) => o.value));
  const span = top - bottom || 1;
  const step = (W - PAD) / bars.length;
  const bodyW = Math.max(1.5, step * 0.6);
  const x = (i: number) => PAD + step * (i + 0.5);
  const y = (v: number) => ((top - v) / span) * (PRICE_H - 20) + 10;

  const maxVol = Math.max(...bars.map((b) => b.volume || 0), 1);
  const volY = (v: number) => VOL_TOP + VOL_H - ((v || 0) / maxVol) * VOL_H;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="K 线">
      {overlays.map((o) => (
        <g key={o.label}>
          <line x1={PAD} x2={W} y1={y(o.value)} y2={y(o.value)} stroke="#999" strokeDasharray="4 4" />
          <text x={W - 60} y={y(o.value) - 4} fontSize="11" fill="#666">{o.label} {o.value.toFixed(2)}</text>
        </g>
      ))}

      {bars.map((b, i) => {
        const color = `var(${changeColor(b.close >= b.open ? 1 : -1)})`;
        const bodyTop = Math.min(y(b.open), y(b.close));
        return (
          <g key={b.date} data-candle={b.date}>
            {/* 影线：最高 → 最低 */}
            <line x1={x(i)} x2={x(i)} y1={y(b.high)} y2={y(b.low)} stroke={color} strokeWidth="1" />
            {/* 实体：开 → 收 */}
            <rect x={x(i) - bodyW / 2} y={bodyTop} width={bodyW}
                  height={Math.max(1, Math.abs(y(b.open) - y(b.close)))} fill={color} />
          </g>
        );
      })}

      {/* 成交量副图：柱高按全区间最大量归一 */}
      {bars.map((b, i) => (
        <rect key={`v${b.date}`} data-volume={b.date} x={x(i) - bodyW / 2}
              y={volY(b.volume)} width={bodyW} height={VOL_TOP + VOL_H - volY(b.volume)}
              fill={`var(${changeColor(b.close >= b.open ? 1 : -1)})`} opacity="0.6" />
      ))}
      <line x1={PAD} x2={W} y1={VOL_TOP + VOL_H} y2={VOL_TOP + VOL_H} stroke="#ccc" />
    </svg>
  );
}

export function Stock({ code, market }: { code: string; market: string }) {
  const { data: quote } = usePoll<Quote>(`/api/quote/${code}`, 3_000);
  const [period, setPeriod] = useState<Period>("day");
  const [adjust, setAdjust] = useState<"raw" | "adj">("raw");
  const [daily, setDaily] = useState<KBar[]>([]);
  const [pool, setPool] = useState<PoolRow | null>(null);

  useEffect(() => {
    getJson<{ bars: KBar[] }>(`/api/kline/${code}?days=${PERIOD_DAYS[period]}&adjust=${adjust}`)
      .then((r) => setDaily(r.bars ?? []))
      .catch(() => setDaily([]));
    getJson<{ rows: PoolRow[] }>("/api/pool").then((r) => {
      setPool(r.rows.find((x) => x.code === code) ?? null);
    }).catch(() => setPool(null));
  }, [code, period, adjust]);

  // 周/月是真聚合（ISO 周、自然月），不是「把天数改小」——同一份日线派生三个周期
  const bars = useMemo(() => aggregate(daily, period), [daily, period]);

  const chg = quote && quote.prev_close ? ((quote.price / quote.prev_close - 1) * 100) : 0;
  const isCn = market.startsWith("cn_");

  return (
    <section>
      <h2>
        {code} <span style={{ color: `var(${changeColor(chg)})` }}>
          {quote?.price?.toFixed(2)} ({chg.toFixed(2)}%)
        </span>
      </h2>

      <div className="statusbar">
        {(["day", "week", "month"] as Period[]).map((p) => (
          <button key={p} onClick={() => setPeriod(p)} className={period === p ? "active" : ""}>
            {p === "day" ? "日" : p === "week" ? "周" : "月"}
          </button>
        ))}
        <button onClick={() => setAdjust(adjust === "raw" ? "adj" : "raw")}>
          {adjust === "raw" ? "不复权" : "前复权"}
        </button>
      </div>

      <KLineChart bars={bars} overlays={pool ? boxOverlay(pool, bars) : []} />

      {pool && (
        <p className="muted">
          箱体 {pool.box_days} 日 ｜ 位置 {((posInBox(pool, quote?.price ?? pool.close_raw) ?? 0) * 100).toFixed(0)}%
          {/* 折半标记在 pool.factors 里，不是顶层字段——读顶层永远是 undefined，
              角标永远不显示（API 返回 factors 已是对象，见 /api/pool） */}
          　{(pool.factors?.a_group_discounted ?? false) ? "A 组已折半" : ""}
        </p>
      )}

      {/* 港美股不显示涨跌停价：那边没有这个概念，显示出来只是错误信息 */}
      {isCn && quote?.up != null && (
        <p>涨停 {quote.up.toFixed(2)} ｜ 跌停 {quote.down?.toFixed(2)}</p>
      )}
    </section>
  );
}
