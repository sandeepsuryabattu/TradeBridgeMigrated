import { useEffect, useState } from "react";

// The engine's own 1-min tape: GET /api/autotrender/candles. Real bars only.
export default function useCandles(symbol = "NIFTY", n = 60, pollMs = 15000) {
  const [bars, setBars] = useState([]);
  useEffect(() => {
    let stop = false;
    async function poll() {
      try {
        const res = await fetch(
          `/api/autotrender/candles?symbol=${encodeURIComponent(symbol)}&n=${n}`
        );
        if (!res.ok) return;
        const j = await res.json();
        if (!stop && Array.isArray(j.bars)) setBars(j.bars);
      } catch {
        /* bot unreachable — show nothing, never mocks */
      }
    }
    poll();
    const ms = Number(pollMs) > 0 ? Number(pollMs) : 15000;
    const id = setInterval(poll, ms);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [symbol, n, pollMs]);
  return bars;
}
