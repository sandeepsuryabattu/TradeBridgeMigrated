// Engine internals panel — the WHY behind the latest scheduler tick.
// Reads bot.engine from /api/autotrender/live. Real data only; renders
// nothing when the bot hasn't published yet.
function kv(label, value, tone = "text-neutral-200") {
  return (
    <div className="rounded-lg bg-black/30 p-2">
      <p className="text-[11px] text-neutral-500">{label}</p>
      <p className={`text-[13px] font-semibold tabular-nums mt-0.5 ${tone}`}>{value}</p>
    </div>
  );
}

function fmtScore(v) {
  const n = Number(v);
  return Number.isFinite(n) ? (Number.isInteger(n) ? String(n) : n.toFixed(1)) : "—";
}

export default function EnginePanel({ engine }) {
  if (!engine) {
    return (
      <div className="rounded-xl border border-white/10 bg-white/5 p-4 text-center">
        <p className="text-[12px] text-neutral-500">No engine tick published yet — waiting for the first in-hours evaluation.</p>
      </div>
    );
  }
  const fired = String(engine.outcome ?? "").startsWith("generated");
  const fusion = engine.fusion ?? null;
  return (
    <div className="rounded-xl border border-white/10 bg-white/5 p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[12px] font-semibold tracking-wide text-neutral-300">ENGINE</p>
        <span className={`rounded-full border px-2 py-0.5 text-[11px] font-bold ${
          fired
            ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-300"
            : "border-white/10 bg-white/5 text-neutral-400"
        }`}>
          {engine.outcome ?? "—"}
        </span>
        <span className="ml-auto text-[11px] tabular-nums text-neutral-500">{engine.at ?? ""}</span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {kv("Bars", engine.bars ?? "—")}
        {kv("RSI", engine.rsi == null ? "—" : Number(engine.rsi).toFixed(1))}
        {kv("Trend score", fmtScore(engine.trend?.score))}
        {kv("Trigger", engine.trigger?.fire ? `FIRED ${engine.trigger?.direction ?? ""}` : "no",
          engine.trigger?.fire ? "text-emerald-300" : "text-neutral-400")}
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {kv("Volatility", engine.volatility?.ok ? "OK" : "VETO",
          engine.volatility?.ok === false ? "text-red-300" : "text-neutral-200")}
        {kv("Positioning", fusion ? fmtScore(fusion.detail?.positioning) : (engine.positioning?.score ?? "—"))}
        {kv("Fusion v2", fusion ? `${fmtScore(fusion.score)}${fusion.fire ? " FIRED" : ""}` : "—",
          fusion?.fire ? "text-emerald-300" : "text-neutral-200")}
        {kv("Cooldown", engine.cooldown?.last_bias
          ? `${engine.cooldown.last_bias} · ${engine.cooldown.remaining_secs}s`
          : "—")}
      </div>
      {engine.v2 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {kv("ADX", engine.v2.strength?.trending
            ? `${fmtScore(engine.v2.strength.adx)} TREND`
            : `${fmtScore(engine.v2.strength?.adx)} range`,
            engine.v2.strength?.trending ? "text-emerald-300" : "text-neutral-400")}
          {kv("HTF 15m", `${engine.v2.htf?.bias ?? "—"}${engine.v2.htf_exhaustion?.rsi != null ? ` · RSI ${engine.v2.htf_exhaustion.rsi}` : ""}`)}
          {kv("VWAP stretch", engine.v2.exhaustion?.stretched
            ? `${engine.v2.exhaustion.z}σ CLIMAX`
            : `${fmtScore(engine.v2.exhaustion?.z)}σ`,
            engine.v2.exhaustion?.stretched ? "text-red-300" : "text-neutral-200")}
          {kv("Extension", engine.v2.extension?.vetoed
            ? `${fmtScore(engine.v2.extension.ratio)} SPENT`
            : fmtScore(engine.v2.extension?.ratio),
            engine.v2.extension?.vetoed ? "text-red-300" : "text-neutral-200")}
        </div>
      )}
      {engine.v2 && (engine.v2.iv_skew || engine.v2.straddle || engine.v2.vix) && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {kv("OI flow", engine.v2.oi_flow?.score ?? "—")}
          {kv("IV skew", engine.v2.iv_skew?.skew != null ? `${engine.v2.iv_skew.skew}` : "—")}
          {kv("Straddle", engine.v2.straddle?.chg_pct != null ? `${engine.v2.straddle.chg_pct}%` : "—")}
          {kv("VIX", engine.v2.vix?.vix ?? engine.v2.vix?.state ?? "—")}
        </div>
      )}
      {fusion?.vetoes?.length > 0 && (
        <p className="text-[11px] font-semibold text-red-300/90">
          vetoes: {fusion.vetoes.join(" · ")}
        </p>
      )}
      {(engine.trigger?.reason || engine.volatility?.reason || engine.positioning?.reason) && (
        <p className="text-[11px] text-neutral-500">
          {[engine.trigger?.reason && `trigger: ${engine.trigger.reason}`,
            engine.volatility?.reason && `vol: ${engine.volatility.reason}`,
            engine.positioning?.reason && `pos: ${engine.positioning.reason}`]
            .filter(Boolean).join(" • ")}
        </p>
      )}
    </div>
  );
}
