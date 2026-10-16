import { useEffect, useState } from "react";

// Polls the bot's live engine snapshot (same origin when bundled).
// Returns { ready, data } — data matches /api/autotrender/live shape.
export default function useBotLive(pollMs = 15000) {
  const [data, setData] = useState(null);
  useEffect(() => {
    let stop = false;
    async function poll() {
      try {
        const res = await fetch("/api/autotrender/live");
        if (!res.ok) return;
        const j = await res.json();
        if (!stop && j && j.ready) setData(j);
      } catch {
        /* bot unreachable — show nothing, never mocks */
      }
    }
    poll();
    const id = setInterval(poll, pollMs);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [pollMs]);
  return { ready: !!data, data };
}
