import { useEffect, useRef, useState } from "react";

// Live LTPs over the bot's WebSocket (tick-by-tick: index_ltp/instrument_ltp),
// with the HTTP /ticks endpoint as fallback when the socket is down.
// No polling delay on the hot path — prices move when ticks arrive.
function wsUrl() {
  try {
    const o = window.location.origin;
    if (o.includes(":5174")) return "ws://127.0.0.1:8001/ws";
    return o.replace(/^http/, "ws") + "/ws";
  } catch {
    return "ws://127.0.0.1:8001/ws";
  }
}

export default function useLiveTicks(fallbackPollMs = 10000) {
  const [prices, setPrices] = useState({});
  const [wsUp, setWsUp] = useState(false);
  const wsUpRef = useRef(false);

  useEffect(() => {
    let stop = false;
    let ws = null;
    let retry = null;

    function apply(symbol, ltp) {
      const v = Number(ltp);
      if (!symbol || !Number.isFinite(v) || v <= 0) return;
      const key = String(symbol).toUpperCase();
      setPrices((prev) => (prev[key] === v ? prev : { ...prev, [key]: v }));
    }

    async function httpFallback() {
      if (stop || wsUpRef.current) return;
      try {
        const res = await fetch("/api/autotrender/ticks");
        if (!res.ok) return;
        const j = await res.json();
        if (!stop && Array.isArray(j.ticks)) {
          for (const t of j.ticks) apply(t.symbol, t.ltp);
        }
      } catch {
        /* bot unreachable — show nothing, never mocks */
      }
    }

    function connect() {
      if (stop) return;
      try {
        ws = new WebSocket(wsUrl());
      } catch {
        return;
      }
      ws.onopen = () => {
        if (!stop) {
          wsUpRef.current = true;
          setWsUp(true);
        }
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if ((msg.type === "index_ltp" || msg.type === "instrument_ltp") && msg.data) {
            apply(msg.data.symbol, msg.data.ltp);
          }
        } catch {
          /* ignore malformed frames */
        }
      };
      const down = () => {
        if (stop) return;
        wsUpRef.current = false;
        setWsUp(false);
        httpFallback();
        retry = setTimeout(connect, 5000);
      };
      ws.onclose = down;
      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          /* already closed */
        }
      };
    }

    connect();
    httpFallback();
    const ms = Number(fallbackPollMs) > 0 ? Number(fallbackPollMs) : 10000;
    const id = setInterval(httpFallback, ms);
    return () => {
      stop = true;
      clearInterval(id);
      clearTimeout(retry);
      try {
        ws && ws.close();
      } catch {
        /* already closed */
      }
    };
  }, [fallbackPollMs]);

  return {
    live: wsUp || Object.keys(prices).length > 0,
    via: wsUp ? "ws" : "http",
    nifty: prices["NIFTY"] ?? null,
    vix: prices["INDIAVIX"] ?? null,
  };
}
