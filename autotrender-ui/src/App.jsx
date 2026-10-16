import { useState } from "react";
import Header from "./components/Header.jsx";
import Footer from "./components/Footer.jsx";
import DashboardView from "./components/DashboardView.jsx";
import OptionChainView from "./components/OptionChainView.jsx";
import CallsView from "./components/CallsView.jsx";
import OIBarChart from "./components/OIBarChart.jsx";
import EnginePanel from "./components/EnginePanel.jsx";
import DecisionFeed from "./components/DecisionFeed.jsx";
import CandleChart from "./components/CandleChart.jsx";
import useBotLive from "./hooks/useBotLive.js";
import useLiveTicks from "./hooks/useLiveTicks.js";
import useCandles from "./hooks/useCandles.js";
import useSignals from "./hooks/useSignals.js";

const TABS = [
  ["dashboard", "Dashboard"],
  ["chain", "Nifty Chain"],
  ["calls", "Calls generated"],
];

function signalFromPcr(pcr) {
  const v = Number(pcr);
  if (!Number.isFinite(v)) return "—";
  if (v >= 1.2) return "BULLISH";
  if (v <= 0.8) return "BEARISH";
  return "NEUTRAL";
}

export default function App() {
  const [tab, setTab] = useState("dashboard");
  // Real bot data only — two independent streams:
  // ticks (live LTPs, flow from boot) and engine snapshot (chain/calls).
  const { live: ticksLive, nifty, vix } = useLiveTicks();
  const { ready: botLive, data: bot } = useBotLive();

  const rows = botLive ? bot.rows ?? [] : [];
  const liveSpot = botLive
    ? { symbol: "NIFTY 50", price: bot.spot?.price ?? 0 }
    : { symbol: "NIFTY 50", price: 0 };
  const pcr = botLive ? bot.pcr : null;
  const pcrChg = botLive ? bot.pcrChg : null;
  const signal = botLive ? signalFromPcr(pcr) : "—";
  const support = botLive ? bot.support : null;
  const resistance = botLive ? bot.resistance : null;
  const pain = botLive ? bot.maxPain : null;
  const stradd = botLive ? bot.straddle : null;
  const snapshotAt = botLive ? bot.at : null;
  const chainSource = botLive ? bot.chain_source ?? null : null;
  const { rows: callRows } = useSignals();
  const candles = useCandles("NIFTY", 60, 15000);

  return (
    <div className="min-h-screen flex flex-col bg-[#0b0f0e]">
      <Header nifty={nifty} vix={vix} expiry={botLive ? bot.expiry : "—"} live={ticksLive} />

      <div className="max-w-6xl w-full mx-auto px-4 pt-4 flex gap-2 flex-wrap items-center">
        {TABS.map(([k, label]) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className={`px-3 py-1.5 rounded-full text-[12px] font-semibold border ${
              tab === k
                ? "bg-emerald-500/15 border-emerald-500/30 text-emerald-300"
                : "border-white/10 text-neutral-400 hover:text-neutral-200"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <main className="flex-1 max-w-6xl w-full mx-auto px-4 py-4 space-y-4">
        {!botLive && (
          <div className="rounded-xl border border-amber-400/20 bg-amber-400/5 p-4 text-center">
            <p className="text-[12px] text-amber-200">
              {ticksLive
                ? "Live prices flowing — chain snapshot arrives with the next engine tick."
                : "Waiting for live bot data…"}
            </p>
          </div>
        )}
        {tab === "dashboard" && (
          <>
            <EnginePanel engine={botLive ? bot.engine ?? null : null} />
            <DecisionFeed
              engine={botLive ? bot.engine ?? null : null}
              decisions={botLive ? bot.decisions ?? [] : []}
            />
            <div className="rounded-xl border border-white/10 bg-white/5 p-3">
              <p className="text-[12px] text-neutral-400 mb-2">NIFTY 1-min — the engine tape</p>
              <CandleChart bars={candles} />
            </div>
            <DashboardView
              spot={liveSpot}
              rows={rows}
              pcr={pcr}
              pcrChg={pcrChg}
              signal={signal}
              support={support}
              resistance={resistance}
              maxPain={pain}
              straddle={stradd}
              snapshotAt={snapshotAt}
              chainSource={chainSource}
            />
            {rows.length > 0 && (
              <div className="rounded-xl border border-white/10 bg-white/5 p-3">
                <p className="text-[12px] text-neutral-400 mb-2">OI by strike (live)</p>
                <OIBarChart rows={rows} />
              </div>
            )}
          </>
        )}
        {tab === "chain" && <OptionChainView rows={rows} />}
        {tab === "calls" && (
          <CallsView rows={callRows} />
        )}
      </main>

      <Footer />
    </div>
  );
}
