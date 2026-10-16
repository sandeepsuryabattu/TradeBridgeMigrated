function signalTone(value) {
  const s = String(value ?? "").toUpperCase();
  if (s.includes("BUY")) return "text-emerald-400";
  if (s.includes("SELL")) return "text-red-400";
  return "text-neutral-200";
}

function formatPrice(n) {
  const num = Number(n);
  if (Number.isNaN(num)) return "—";
  return num.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function DashboardView({ spot, rows, pcr, signal, support, resistance, maxPain, straddle, pcrChg, snapshotAt, chainSource }) {
  const spotPrice = typeof spot === "object" && spot !== null
    ? spot.price ?? spot.ltp ?? spot.value
    : spot;

  const spotChg = typeof spot === "object" && spot !== null
    ? spot.changePct ?? spot.chgPct ?? spot.change ?? null
    : null;

  const chgNum = spotChg === null || spotChg === undefined ? null : Number(spotChg);
  const chgUp = chgNum !== null && !Number.isNaN(chgNum) && chgNum >= 0;

  const strikeCount = Array.isArray(rows) ? rows.length : 0;

  const pcrChgNum =
    pcrChg === null || pcrChg === undefined || pcrChg === "" ? null : Number(pcrChg);
  const pcrChgUp = pcrChgNum !== null && !Number.isNaN(pcrChgNum) && pcrChgNum >= 0;

  return (
    <section className="space-y-3">
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3">
        <div className="rounded-xl border border-white/10 bg-white/5 p-3">
          <p className="text-[12px] text-neutral-400">Spot</p>
          <p className="text-[13px] font-semibold tabular-nums mt-1">
            {formatPrice(spotPrice)}{" "}
            {chgNum !== null && !Number.isNaN(chgNum) && (
              <span className={`font-medium ${chgUp ? "text-emerald-400" : "text-red-400"}`}>
                {chgUp ? "+" : ""}{chgNum.toFixed(2)}%
              </span>
            )}
          </p>
        </div>

        <div className="rounded-xl border border-white/10 bg-white/5 p-3">
          <p className="text-[12px] text-neutral-400">PCR</p>
          <p className="text-[13px] font-semibold tabular-nums mt-1">
            {Number.isNaN(Number(pcr)) ? "—" : Number(pcr).toFixed(2)}{" "}
            {pcrChgNum !== null && !Number.isNaN(pcrChgNum) && (
              <span className={`font-medium ${pcrChgUp ? "text-emerald-400" : "text-red-400"}`}>
                {pcrChgUp ? "+" : ""}{pcrChgNum.toFixed(2)}
              </span>
            )}
          </p>
        </div>

        <div className="rounded-xl border border-white/10 bg-white/5 p-3">
          <p className="text-[12px] text-neutral-400">Signal</p>
          <p className={`text-[13px] font-semibold mt-1 ${signalTone(signal)}`}>
            {signal ?? "—"}
          </p>
        </div>

        <div className="rounded-xl border border-white/10 bg-white/5 p-3">
          <p className="text-[12px] text-neutral-400">Support / Resistance</p>
          <p className="text-[13px] font-semibold tabular-nums mt-1">
            S: {support ?? "—"} / R: {resistance ?? "—"}
          </p>
          {strikeCount > 0 && (
            <p className="text-[12px] text-neutral-500 mt-1">{strikeCount} strikes</p>
          )}
        </div>

        <div className="rounded-xl border border-white/10 bg-white/5 p-3">
          <p className="text-[12px] text-neutral-400">Max Pain</p>
          <p className="text-[13px] font-semibold tabular-nums mt-1">
            {maxPain ?? "—"}
          </p>
        </div>

        <div className="rounded-xl border border-white/10 bg-white/5 p-3">
          <p className="text-[12px] text-neutral-400">Straddle</p>
          <p className="text-[13px] font-semibold tabular-nums mt-1">
            {straddle ?? "—"}
          </p>
        </div>
      </div>

      <div className="rounded-xl border border-white/10 bg-white/5 p-3">
        <p className="text-[12px] text-neutral-400 mb-2">Live snapshot</p>
        {snapshotAt ? (
          <p className="text-[12px] tabular-nums text-neutral-200">
            Chain snapshot <span className="text-emerald-300 font-semibold">{snapshotAt}</span>
            {chainSource && <span className="text-neutral-500"> • {chainSource} • ChgOI live</span>}
            {strikeCount > 0 && <span className="text-neutral-500"> • {strikeCount} strikes • no intraday history feed wired</span>}
          </p>
        ) : (
          <p className="text-[12px] text-neutral-500">No snapshot yet.</p>
        )}
      </div>
    </section>
  );
}
