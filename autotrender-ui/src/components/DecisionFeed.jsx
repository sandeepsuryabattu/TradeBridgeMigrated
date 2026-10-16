// Live decision feed — which indicator said what on the latest tick,
// plus a rolling history of every tick's decision. Real data only.
function voteTone(vote) {
  const v = String(vote ?? "");
  if (/FIRED|TREND|EXPAND|BULL/.test(v)) return "emerald";
  if (/BLOCKED|CLIMAX|SPENT|CHOP|CRUSH|BEAR|panic/.test(v)) return "red";
  return "dim";
}

function toneClasses(tone, picked) {
  if (!picked) return "border-white/5 text-neutral-600 opacity-70";
  if (tone === "emerald") return "border-emerald-400/60 bg-emerald-400/15 font-bold text-emerald-100 shadow-[0_0_8px_rgba(52,211,153,0.25)]";
  if (tone === "red") return "border-red-400/60 bg-red-400/15 font-bold text-red-100 shadow-[0_0_8px_rgba(248,113,113,0.25)]";
  return "border-white/30 bg-white/10 font-bold text-neutral-50";
}

function OptionChips({ options, vote }) {
  if (!options || options.length === 0) {
    return <span className="font-semibold whitespace-nowrap">{vote}</span>;
  }
  return (
    <span className="flex flex-wrap gap-1 py-0.5 sm:grid sm:grid-flow-col sm:auto-cols-fr">
      {options.map((opt) => (
        <span
          key={opt}
          className={`rounded-full border px-2 py-px text-[11px] whitespace-nowrap tracking-wide text-center sm:min-w-0 ${toneClasses(voteTone(vote), opt === vote)}`}
        >
          {opt}
        </span>
      ))}
    </span>
  );
}

// Display-only tidy for backend detail strings: Python reprs
// ("{'ema_stack': 0}") and raw floats ("value=23256.404003862073")
// become compact human text. Raw value kept in the title tooltip.
function cleanDetail(detail) {
  let s = String(detail ?? "").trim();
  if (!s || s === "None") return "—";
  s = s.replace(/^\{/, "").replace(/\}$/, "");
  s = s.replace(/',\s*'/g, " · ").replace(/':\s*/g, "=");
  s = s.replace(/^'/, "").replace(/'$/, "");
  s = s.replace(/_/g, " ");
  s = s.replace(/=\s*None\b/g, "= —");
  s = s.replace(/\d+\.\d{3,}/g, (m) => Number(m).toFixed(2));
  s = s.replace(/\s*=\s*/g, " = ").replace(/\s*·\s*/g, " · ");
  return s || "—";
}

function DecisionBanner({ entry }) {
  if (!entry) return null;
  const fired = !!entry.fire;
  return (
    <div className={`rounded-lg border px-3 py-2 flex flex-wrap items-center gap-2 ${
      fired ? "border-emerald-400/30 bg-emerald-400/10" : "border-white/10 bg-black/30"
    }`}>
      <span className="text-[11px] text-neutral-500">{entry.at?.slice(11, 19) ?? ""}</span>
      <span className={`text-[13px] font-bold ${fired ? "text-emerald-300" : "text-neutral-300"}`}>
        {fired ? `FIRED ${entry.direction ?? ""}` : "no fire"}
      </span>
      <span className="text-[11px] tabular-nums text-neutral-400">
        {entry.outcome ?? ""}{entry.score != null ? ` · score ${entry.score}` : ""}
      </span>
      {(entry.vetoes?.length > 0) && (
        <span className="text-[11px] text-red-300/90">vetoes: {entry.vetoes.join(" · ")}</span>
      )}
    </div>
  );
}

export default function DecisionFeed({ engine, decisions }) {
  const ballot = engine?.ballot ?? [];
  const hist = (decisions ?? []).slice(-25).reverse();
  if (!engine && hist.length === 0) return null;
  return (
    <div className="rounded-xl border border-white/10 bg-white/5 p-3 space-y-2">
      <p className="text-[12px] font-semibold tracking-wide text-neutral-300">
        SIGNAL BALLOT — who voted what <span className="font-normal text-neutral-500">· live every tick</span>
      </p>
      {hist.length > 0 && <DecisionBanner entry={hist[0]} />}
      {ballot.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-left text-neutral-500">
                <th className="py-1.5 pr-3 text-[10px] font-semibold uppercase tracking-widest">Indicator</th>
                <th className="py-1.5 pr-3 text-[10px] font-semibold uppercase tracking-widest">Vote</th>
                <th className="py-1.5 text-[10px] font-semibold uppercase tracking-widest">Detail</th>
              </tr>
            </thead>
            <tbody>
              {ballot.map((row) => (
                <tr key={row.model} className={`border-t border-white/5 transition-colors hover:bg-white/[0.03] ${row.model === "DECISION" ? "bg-emerald-400/[0.04] font-semibold" : ""}`}>
                  <td className="py-1.5 pr-3 text-neutral-300 whitespace-nowrap font-medium">{row.model}</td>
                  <td className="py-1.5 pr-3"><OptionChips options={row.options} vote={row.vote} /></td>
                  <td className="py-1.5 text-neutral-400 tabular-nums break-words" title={String(row.detail ?? "")}>{cleanDetail(row.detail)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {hist.length > 1 && (
        <details className="pt-1">
          <summary className="text-[11px] text-neutral-500 cursor-pointer hover:text-neutral-300">
            Earlier ticks ({hist.length - 1})
          </summary>
          <div className="max-h-40 overflow-y-auto space-y-1 mt-1">
            {hist.slice(1).map((d, i) => (
              <DecisionBanner key={`${d.at}-${i}`} entry={d} />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
