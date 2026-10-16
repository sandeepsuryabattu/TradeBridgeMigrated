import { useEffect, useState } from "react";

// Live engine calls from the paper bot — the ONLY signal source.
// No local computation, no mock candles: rows mirror stored engine calls.
export default function useSignals(refreshMs = 15000) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const res = await fetch("/api/autotrender/calls?limit=20");
        if (!res.ok) return;
        const j = await res.json();
        const calls = Array.isArray(j.calls) ? j.calls : [];
        if (cancelled) return;
        setRows(
          calls.map((c) => ({
            id: c.id,
            symbol: c.underlying ?? "NIFTY",
            price:
              (Number(c.entry_low) + Number(c.entry_high)) / 2 || null,
            rsi: null,
            bias: c.model?.direction ?? null,
            entry:
              (Number(c.entry_low) + Number(c.entry_high)) / 2 || null,
            sl: Number(c.stoploss) || null,
            tgt: Number(c.target) || null,
            side: c.side,
            strike: c.strike,
            status: c.status,
            created: c.created_at ?? c.createdAt ?? null,
            model: c.model ?? null,
          }))
        );
      } catch {
        /* bot unreachable — show nothing, never mocks */
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    setLoading(true);
    poll();
    const ms = Number(refreshMs) > 0 ? Number(refreshMs) : 15000;
    const id = setInterval(poll, ms);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [refreshMs]);

  return { rows, loading };
}
