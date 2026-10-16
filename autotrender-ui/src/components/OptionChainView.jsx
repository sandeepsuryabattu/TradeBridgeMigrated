import { useState } from "react";
import OITable from "./OITable.jsx";

const FILTERS = ["All", "ITM", "OTM"];

function formatNum(n) {
  return Number(n || 0).toLocaleString("en-IN");
}

export default function OptionChainView({ rows = [] }) {
  const [filter, setFilter] = useState("All");

  const atmIndex = rows.findIndex((r) => r.atm);
  const midIndex = atmIndex >= 0 ? atmIndex : Math.floor(rows.length / 2);
  const midStrike = rows[midIndex]?.strike ?? 0;

  const visibleRows =
    filter === "ITM"
      ? rows.filter((r) => r.strike < midStrike)
      : filter === "OTM"
        ? rows.filter((r) => r.strike > midStrike)
        : rows;

  const totalCallOI = visibleRows.reduce((sum, r) => sum + Number(r.callOI || 0), 0);
  const totalPutOI = visibleRows.reduce((sum, r) => sum + Number(r.putOI || 0), 0);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1 rounded-xl border border-white/10 bg-white/5 p-1">
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors ${
                filter === f
                  ? "bg-emerald-500/20 text-emerald-200 border border-emerald-500/30"
                  : "text-neutral-400 border border-transparent hover:text-neutral-100 hover:bg-white/5"
              }`}
            >
              {f}
            </button>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-2 text-[12px]">
          <span className="px-3 py-1.5 rounded-xl border border-white/10 bg-white/5 text-neutral-300">
            Call OI: <b className="tabular-nums text-neutral-100">{formatNum(totalCallOI)}</b>
          </span>
          <span className="px-3 py-1.5 rounded-xl border border-white/10 bg-white/5 text-neutral-300">
            Put OI: <b className="tabular-nums text-neutral-100">{formatNum(totalPutOI)}</b>
          </span>
          <span className="px-3 py-1.5 rounded-xl border border-white/10 bg-white/5 text-neutral-500">
            {visibleRows.length} / {rows.length} strikes
          </span>
        </div>
      </div>

      <OITable rows={visibleRows} />
    </div>
  );
}
