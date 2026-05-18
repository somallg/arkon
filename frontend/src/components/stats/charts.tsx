"use client";

import React from "react";

/* ──────────────────────────────────────────────────────────────────────── */
/* Small SVG charts used by the Statistics dashboard.                        */
/* Intentionally dependency-free: keeps Next 16 / React 19 compat boring.    */
/* ──────────────────────────────────────────────────────────────────────── */

type SeriesPoint = { date: string; value: number | null };

export function LineChart({
  data,
  height = 120,
  color = "#2a7ec2",
  label,
}: {
  data: SeriesPoint[];
  height?: number;
  color?: string;
  label?: string;
}) {
  if (!data.length) {
    return (
      <div className="h-[120px] flex items-center justify-center text-xs text-muted-foreground/60">
        No data
      </div>
    );
  }
  const width = 600;
  const pad = { left: 28, right: 8, top: 8, bottom: 18 };
  const values = data.map((d) => (d.value ?? 0));
  const max = Math.max(1, ...values);
  const min = Math.min(0, ...values);
  const span = max - min || 1;
  const xStep = (width - pad.left - pad.right) / Math.max(1, data.length - 1);

  const points = data.map((d, i) => {
    const x = pad.left + i * xStep;
    const y = pad.top + (1 - ((d.value ?? 0) - min) / span) * (height - pad.top - pad.bottom);
    return [x, y, d] as const;
  });

  const path = points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const areaPath =
    `${path} L${pad.left + (data.length - 1) * xStep},${height - pad.bottom} L${pad.left},${height - pad.bottom} Z`;

  return (
    <div>
      {label && <div className="text-xs text-muted-foreground mb-1">{label}</div>}
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto block">
        {/* horizontal grid */}
        {[0.25, 0.5, 0.75].map((t) => (
          <line
            key={t}
            x1={pad.left}
            x2={width - pad.right}
            y1={pad.top + t * (height - pad.top - pad.bottom)}
            y2={pad.top + t * (height - pad.top - pad.bottom)}
            stroke="currentColor"
            strokeOpacity={0.07}
          />
        ))}
        <path d={areaPath} fill={color} fillOpacity={0.08} />
        <path d={path} fill="none" stroke={color} strokeWidth={1.5} />
        {points.map(([x, y, d], i) => (
          <circle key={i} cx={x} cy={y} r={2} fill={color}>
            <title>{`${d.date}: ${d.value ?? 0}`}</title>
          </circle>
        ))}
        {/* y-axis ticks */}
        <text x={4} y={pad.top + 4} fontSize={9} fill="currentColor" fillOpacity={0.45}>{max.toFixed(0)}</text>
        <text x={4} y={height - pad.bottom + 2} fontSize={9} fill="currentColor" fillOpacity={0.45}>{min.toFixed(0)}</text>
        {/* x labels: first + last */}
        {data.length > 0 && (
          <>
            <text x={pad.left} y={height - 4} fontSize={9} fill="currentColor" fillOpacity={0.45}>{data[0].date}</text>
            <text x={width - pad.right} y={height - 4} fontSize={9} textAnchor="end" fill="currentColor" fillOpacity={0.45}>
              {data[data.length - 1].date}
            </text>
          </>
        )}
      </svg>
    </div>
  );
}

export function BarList({
  items,
  valueKey = "count",
  labelKey = "name",
  emptyText = "No data",
  color = "#2a7ec2",
  maxItems = 10,
}: {
  items: Array<Record<string, unknown>>;
  valueKey?: string;
  labelKey?: string;
  emptyText?: string;
  color?: string;
  maxItems?: number;
}) {
  if (!items?.length) {
    return <div className="text-xs text-muted-foreground/60 px-1 py-3">{emptyText}</div>;
  }
  const sliced = items.slice(0, maxItems);
  const max = Math.max(1, ...sliced.map((i) => Number(i[valueKey] ?? 0)));
  return (
    <div className="flex flex-col gap-[6px]">
      {sliced.map((it, idx) => {
        const v = Number(it[valueKey] ?? 0);
        const label = String(it[labelKey] ?? "—");
        const pct = (v / max) * 100;
        return (
          <div key={idx} className="grid grid-cols-[1fr_auto] items-center gap-2">
            <div className="relative h-6 rounded bg-black/[0.03] overflow-hidden">
              <div
                className="absolute inset-y-0 left-0 rounded transition-all"
                style={{ width: `${pct}%`, backgroundColor: color, opacity: 0.18 }}
              />
              <div className="relative px-2 py-1 text-[12px] truncate text-foreground">{label}</div>
            </div>
            <div className="text-[12px] tabular-nums text-muted-foreground min-w-[3ch] text-right">{v}</div>
          </div>
        );
      })}
    </div>
  );
}
