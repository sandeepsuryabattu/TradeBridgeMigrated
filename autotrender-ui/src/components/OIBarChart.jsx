export default function OIBarChart({ rows = [] }) {
  const maxOI = Math.max(
    1,
    ...rows.map((r) => Math.max(Number(r.callOI) || 0, Number(r.putOI) || 0))
  );

  const fmt = (n) => Number(n || 0).toLocaleString("en-IN");

  return (
    <div className="w-full">
      {/* header */}
      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2 px-1 pb-1 text-[11px] font-medium uppercase tracking-wide text-neutral-400">
        <span className="text-right">Call OI</span>
        <span className="w-16 text-center">Strike</span>
        <span className="text-left">Put OI</span>
      </div>

      <div className="flex flex-col gap-1">
        {rows.map((row) => {
          const callPct = ((Number(row.callOI) || 0) / maxOI) * 100;
          const putPct = ((Number(row.putOI) || 0) / maxOI) * 100;
          return (
            <div
              key={row.strike}
              className={`grid grid-cols-[1fr_auto_1fr] items-center gap-2 rounded-md px-1 py-0.5 text-[11px] tabular-nums ${
                row.atm ? "bg-emerald-500/10" : ""
              }`}
            >
              {/* call bar (left, right-aligned) */}
              <div className="flex items-center justify-end gap-1.5">
                <span className="text-neutral-300">{fmt(row.callOI)}</span>
                <div className="h-4 w-full max-w-[220px] overflow-hidden rounded bg-white/5">
                  <div className="flex h-full w-full justify-end">
                    <div
                      className="h-full rounded-l bg-red-400/70"
                      style={{ width: `${callPct}%` }}
                      title={`Call OI ${fmt(row.callOI)}`}
                    />
                  </div>
                </div>
              </div>

              {/* strike label center */}
              <span
                className={`w-16 text-center ${
                  row.atm ? "font-bold text-emerald-300" : "font-medium text-neutral-100"
                }`}
              >
                {fmt(row.strike)}
              </span>

              {/* put bar (right, left-aligned) */}
              <div className="flex items-center gap-1.5">
                <div className="h-4 w-full max-w-[220px] overflow-hidden rounded bg-white/5">
                  <div
                    className="h-full rounded-r bg-green-500/70"
                    style={{ width: `${putPct}%` }}
                    title={`Put OI ${fmt(row.putOI)}`}
                  />
                </div>
                <span className="text-neutral-300">{fmt(row.putOI)}</span>
              </div>
            </div>
          );
        })}
      </div>

      {rows.length === 0 && (
        <p className="py-4 text-center text-[11px] text-neutral-500">No OI data</p>
      )}
    </div>
  );
}
