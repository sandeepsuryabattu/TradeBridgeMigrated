import { useEffect, useState } from "react";

function biasTone(bias) {
  const b = String(bias ?? "").toUpperCase();
  if (b.includes("STRONG") && b.includes("BULLISH"))
    return "bg-green-500/20 text-green-300 border-green-500/30";
  if (b.includes("BULLISH"))
    return "bg-emerald-500/15 text-emerald-300 border-emerald-500/30";
  if (b.includes("STRONG") && b.includes("BEARISH"))
    return "bg-red-500/20 text-red-300 border-red-500/30";
  if (b.includes("BEARISH"))
    return "bg-orange-500/15 text-orange-300 border-orange-500/30";
  return "bg-neutral-500/15 text-neutral-300 border-white/10";
}

function fmt(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(2) : "—";
}

function fmtNum(v, digits = 2) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : "—";
}

// One rationale line in the Why modal. Null-safe: skips empties.
function WRow({ k, v }) {
  if (v == null || v === "") return null;
  return (
    <div className="flex justify-between gap-3 border-t border-white/5 py-1 tabular-nums">
      <span className="shrink-0 text-neutral-500">{k}</span>
      <span className="text-right text-neutral-200">{String(v)}</span>
    </div>
  );
}

// Modal with the engine's full reasoning for one call.
// m = call.model {version, score, v1_score, direction, trend, trigger,
// positioning, volatility, v2{strength, htf, ...}} — all fields optional.
function WhyModal({ row, onClose }) {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!row) return null;
  const m = row.model;
  const title = `${row.symbol ?? ""} ${row.side ?? ""} ${row.strike ?? ""}`.trim();

  let body;
  if (!m || typeof m !== "object") {
    body = <p className="text-neutral-400">No engine rationale recorded (manual / external call).</p>;
  } else {
    const v2 = m.v2 ?? {};
    const trend = m.trend ?? {};
    const parts = trend.parts ?? {};
    const st = v2.strength ?? {};
    const htf = v2.htf ?? {};
    const btw = v2.supertrend ?? {};
    const exh = v2.exhaustion ?? {};
    const ext = v2.extension ?? {};
    body = (
      <div>
        <div className="py-1 text-[14px] font-bold tabular-nums">
          score {m.score ?? "—"}
          {m.v1_score != null ? ` (v1 ${m.v1_score})` : ""}
          {m.direction ? ` · ${m.direction}` : ""}
          {m.version ? <span className="text-neutral-500 text-[11px]"> · {m.version}</span> : null}
        </div>
        <WRow k="Trigger" v={m.trigger} />
        <WRow
          k="Trend"
          v={`score ${trend.score ?? "—"}${
            parts.ema_stack != null || parts.vwap_side != null
              ? ` (ema ${parts.ema_stack}, vwap ${parts.vwap_side})`
              : ""
          }`}
        />
        <WRow k="Positioning" v={m.positioning?.reason} />
        <WRow k="Volatility" v={m.volatility?.reason} />
        <WRow
          k="ADX"
          v={st.adx == null ? null : `adx ${st.adx} (${st.trending ? "trending" : "range"}, DI+ ${st.plus_di} DI- ${st.minus_di})`}
        />
        <WRow k="HTF 15m" v={htf.bias == null ? null : `${htf.bias} (${htf.tf_bars} bars)`} />
        <WRow k="Supertrend" v={btw.direction ? `${btw.direction}${btw.value != null ? ` (${fmtNum(btw.value)})` : ""}` : null} />
        <WRow k="Pos composite" v={v2.positioning_eff?.reason} />
        <WRow k="OI flow" v={v2.oi_flow?.reason} />
        <WRow k="IV skew" v={v2.iv_skew?.reason} />
        <WRow
          k="VWAP stretch"
          v={exh.z == null ? null : `z ${exh.z}${exh.stretched ? " CLIMAX" : " ok"}`}
        />
        <WRow
          k="Day extension"
          v={ext.ratio == null ? null : `ratio ${ext.ratio}${ext.vetoed ? " SPENT" : ""}`}
        />
        <WRow
          k="Straddle"
          v={
            v2.straddle?.chg_pct == null
              ? null
              : `chg ${v2.straddle.chg_pct}%${
                  v2.straddle.expanding === true ? " EXPAND" : v2.straddle.expanding === false ? " CRUSH" : ""
                }`
          }
        />
        <WRow
          k="VIX"
          v={v2.vix?.state != null ? `${v2.vix.state}${v2.vix.vix != null ? ` (${v2.vix.vix})` : ""}` : null}
        />
      </div>
    );
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg max-h-[85vh] overflow-y-auto rounded-xl border border-white/10 bg-zinc-950 p-4 text-[12px]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between pb-2">
          <span className="text-[13px] font-bold">
            Why this call? <span className="text-neutral-400">{title}</span>
          </span>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded-lg border border-white/10 px-2 py-0.5 text-neutral-400 hover:text-neutral-100"
          >
            ✕
          </button>
        </div>
        {body}
      </div>
    </div>
  );
}

export default function CallsView({ rows = [] }) {
  const [whyFor, setWhyFor] = useState(null);

  return (
    <div className="space-y-3 text-[12px]">
      <div className="overflow-x-auto rounded-xl border border-white/10 bg-zinc-950">
        <table className="w-full text-[12px] tabular-nums text-neutral-200">
          <thead>
            <tr className="text-left text-neutral-400">
              <th className="px-3 py-2 font-medium">Symbol</th>
              <th className="px-3 py-2 font-medium text-right">Price</th>
              <th className="px-3 py-2 font-medium text-right">RSI</th>
              <th className="px-3 py-2 font-medium text-center">Bias</th>
              <th className="px-3 py-2 font-medium text-right">Entry</th>
              <th className="px-3 py-2 font-medium text-right">SL</th>
              <th className="px-3 py-2 font-medium text-right">Target</th>
              <th className="px-3 py-2 font-medium text-right">Why</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id ?? r.symbol} className="border-t border-white/5">
                <td className="px-3 py-2 font-semibold text-neutral-100">
                  {r.symbol}
                </td>
                <td className="px-3 py-2 text-right">{fmt(r.price)}</td>
                <td className="px-3 py-2 text-right">
                  {Number.isFinite(Number(r.rsi))
                    ? Number(r.rsi).toFixed(1)
                    : "—"}
                </td>
                <td className="px-3 py-2 text-center">
                  <span
                    className={`inline-block whitespace-nowrap rounded-full border px-2 py-0.5 font-medium ${biasTone(r.bias)}`}
                  >
                    {r.bias ?? "NEUTRAL"}
                  </span>
                </td>
                <td className="px-3 py-2 text-right">{fmt(r.entry)}</td>
                <td className="px-3 py-2 text-right text-red-300">
                  {fmt(r.sl)}
                </td>
                <td className="px-3 py-2 text-right text-green-300">
                  {fmt(r.tgt)}
                </td>
                <td className="px-3 py-2 text-right">
                  <button
                    onClick={() => setWhyFor(r)}
                    className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 font-semibold text-emerald-200 hover:bg-emerald-500/20"
                  >
                    Why?
                  </button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr className="border-t border-white/5">
                <td colSpan={8} className="px-3 py-3 text-neutral-500">
                  No signals yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <WhyModal row={whyFor} onClose={() => setWhyFor(null)} />
    </div>
  );
}
