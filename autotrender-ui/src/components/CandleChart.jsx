// Minimal SVG candlestick chart — no dependencies. Renders engine bars
// oldest→newest; the forming bar is drawn dimmed with a LIVE tag.
export default function CandleChart({ bars = [], height = 220 }) {
  const W = 560;
  const H = height;
  const PAD = 8;
  if (!Array.isArray(bars) || bars.length === 0) {
    return (
      <p className="text-[12px] text-neutral-500">
        No candles yet — the tape starts with the first live ticks.
      </p>
    );
  }
  const rows = bars.slice(-60);
  let hi = -Infinity;
  let lo = Infinity;
  for (const b of rows) {
    hi = Math.max(hi, Number(b.h));
    lo = Math.min(lo, Number(b.l));
  }
  if (!Number.isFinite(hi) || !Number.isFinite(lo) || hi <= lo) {
    return <p className="text-[12px] text-neutral-500">Waiting for valid bars…</p>;
  }
  const span = hi - lo || 1;
  const y = (v) => PAD + (1 - (Number(v) - lo) / span) * (H - PAD * 2);
  const slot = W / rows.length;
  const bw = Math.max(2, slot * 0.6);
  const last = rows[rows.length - 1];
  const lastUp = Number(last.c) >= Number(last.o);

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: H }}>
        {rows.map((b, i) => {
          const up = Number(b.c) >= Number(b.o);
          const color = up ? "#34d399" : "#f87171";
          const x = i * slot + (slot - bw) / 2;
          const forming = i === rows.length - 1;
          return (
            <g key={`${b.t}-${i}`} opacity={forming ? 0.55 : 1}>
              <line
                x1={x + bw / 2} x2={x + bw / 2}
                y1={y(b.h)} y2={y(b.l)}
                stroke={color} strokeWidth={1}
              />
              <rect
                x={x}
                y={y(Math.max(b.o, b.c))}
                width={bw}
                height={Math.max(1, Math.abs(y(b.o) - y(b.c)))}
                fill={color}
              />
            </g>
          );
        })}
        <text x={W - 4} y={y(last.c) - 4} textAnchor="end" fontSize="10" fill={lastUp ? "#34d399" : "#f87171"}>
          {Number(last.c).toFixed(1)}
        </text>
      </svg>
      <p className="text-[11px] text-neutral-500 mt-1 tabular-nums">
        {rows.length} × 1-min bars • {lo.toFixed(1)} – {hi.toFixed(1)}
        {" • "}last bar forming (LIVE)
      </p>
    </div>
  );
}
