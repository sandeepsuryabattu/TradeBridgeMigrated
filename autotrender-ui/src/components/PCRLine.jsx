export default function PCRLine({ points = [] }) {
  const W = 300;
  const H = 80;
  const PAD_X = 4;
  const PAD_Y = 6;

  if (!points.length) {
    return (
      <svg width={W} height={H} className="block" role="img" aria-label="No PCR data">
        <text x={W / 2} y={H / 2} textAnchor="middle" fontSize={11} fill="#737373">
          No PCR data
        </text>
      </svg>
    );
  }

  const values = points.map((p) => Number(p.pcr));
  const lo = Math.min(...values, 0.95);
  const hi = Math.max(...values, 1.05);
  const span = hi - lo || 1;

  const yFor = (v) => PAD_Y + (1 - (v - lo) / span) * (H - PAD_Y * 2);
  const xFor = (i) =>
    points.length === 1
      ? W / 2
      : PAD_X + (i / (points.length - 1)) * (W - PAD_X * 2);

  const coords = points.map((p, i) => `${xFor(i).toFixed(1)},${yFor(Number(p.pcr)).toFixed(1)}`);
  const line = coords.join(" ");

  // Neutral band 0.95–1.05 shaded, clamped to domain
  const bandTop = yFor(Math.min(1.05, hi));
  const bandBottom = yFor(Math.max(0.95, lo));

  const last = values[values.length - 1];
  const lastX = xFor(points.length - 1);
  const lastY = yFor(last);

  return (
    <div className="text-[11px]">
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block" role="img" aria-label="PCR trend">
        {/* neutral band */}
        <rect
          x={PAD_X}
          y={bandTop}
          width={W - PAD_X * 2}
          height={Math.max(0, bandBottom - bandTop)}
          fill="currentColor"
          className="text-white/10"
        />
        {/* band edges */}
        <line x1={PAD_X} x2={W - PAD_X} y1={yFor(1.05)} y2={yFor(1.05)} stroke="#525252" strokeDasharray="3 3" strokeWidth="1" />
        <line x1={PAD_X} x2={W - PAD_X} y1={yFor(0.95)} y2={yFor(0.95)} stroke="#525252" strokeDasharray="3 3" strokeWidth="1" />
        {/* trend polyline */}
        <polyline
          points={line}
          fill="none"
          stroke="#34d399"
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {/* last point dot */}
        <circle cx={lastX} cy={lastY} r="2.5" fill="#34d399" />
      </svg>
      <div className="flex justify-between tabular-nums text-neutral-400">
        <span>{points[0]?.time ?? ""}</span>
        <span className="text-neutral-200">{Number(last).toFixed(2)}</span>
        <span>{points[points.length - 1]?.time ?? ""}</span>
      </div>
    </div>
  );
}
