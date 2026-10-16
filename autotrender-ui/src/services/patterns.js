// Pure technical-analysis helpers (no React, no side effects).

export function ema(values, period) {
  if (!Array.isArray(values) || values.length === 0) return [];
  if (!Number.isFinite(period) || period <= 0) return [];
  const k = 2 / (period + 1);
  const out = new Array(values.length);
  // Seed with SMA of first `period` values so early trend is stable.
  const seedCount = Math.min(period, values.length);
  let sma = 0;
  for (let i = 0; i < seedCount; i++) sma += Number(values[i]);
  sma /= seedCount;
  for (let i = 0; i < values.length; i++) {
    if (i < seedCount - 1) {
      out[i] = null;
    } else if (i === seedCount - 1) {
      out[i] = sma;
    } else {
      out[i] = Number(values[i]) * k + out[i - 1] * (1 - k);
    }
  }
  return out;
}

function lastDefined(arr) {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] !== null && arr[i] !== undefined && Number.isFinite(arr[i])) return arr[i];
  }
  return null;
}

export function rsi(closes, period = 14) {
  if (!Array.isArray(closes) || closes.length < period + 1) return 50;
  let gain = 0;
  let loss = 0;
  for (let i = closes.length - period; i < closes.length; i++) {
    const diff = Number(closes[i]) - Number(closes[i - 1]);
    if (diff >= 0) gain += diff;
    else loss -= diff;
  }
  let avgGain = gain / period;
  let avgLoss = loss / period;
  // Wilder smoothing over the whole series for a stable last value:
  // recompute from scratch using Wilder recursion.
  avgGain = 0;
  avgLoss = 0;
  for (let i = 1; i < closes.length; i++) {
    const diff = Number(closes[i]) - Number(closes[i - 1]);
    const g = diff > 0 ? diff : 0;
    const l = diff < 0 ? -diff : 0;
    if (i <= period) {
      avgGain += g / period;
      avgLoss += l / period;
    } else {
      avgGain = (avgGain * (period - 1) + g) / period;
      avgLoss = (avgLoss * (period - 1) + l) / period;
    }
  }
  if (avgLoss === 0) return 100;
  if (avgGain === 0) return 0;
  const rs = avgGain / avgLoss;
  return 100 - 100 / (1 + rs);
}

function trueRange(cur, prevClose) {
  const h = Number(cur.h);
  const l = Number(cur.l);
  const c = prevClose === null ? Number(cur.c) : Number(prevClose);
  return Math.max(h - l, Math.abs(h - c), Math.abs(l - c));
}

export function atr(candles, period = 14) {
  if (!Array.isArray(candles) || candles.length === 0) return 0;
  if (!Number.isFinite(period) || period <= 0) return 0;
  const trs = [];
  for (let i = 0; i < candles.length; i++) {
    const prevClose = i === 0 ? null : Number(candles[i - 1].c);
    trs.push(trueRange(candles[i], prevClose));
  }
  const n = Math.min(period, trs.length);
  // Wilder's ATR: first value is SMA, then smoothed.
  let a = 0;
  for (let i = 0; i < n; i++) a += trs[i];
  a /= n;
  for (let i = n; i < trs.length; i++) {
    a = (a * (period - 1) + trs[i]) / period;
  }
  return a;
}

export function detect(candles) {
  if (!Array.isArray(candles) || candles.length < 20) {
    throw new Error("detect requires candles array with at least 20 bars");
  }
  const closes = candles.map((k) => Number(k.c));
  const ema5 = ema(closes, 5);
  const ema20 = ema(closes, 20);
  const rsiVal = rsi(closes, 14);

  // 20-bar breakout: compare last close against prior 19 bars' extremes.
  const window20 = candles.slice(-20);
  const prior = window20.slice(0, -1);
  const last = window20[window20.length - 1];
  let highestHigh = -Infinity;
  let lowestLow = Infinity;
  for (const b of prior) {
    if (Number(b.h) > highestHigh) highestHigh = Number(b.h);
    if (Number(b.l) < lowestLow) lowestLow = Number(b.l);
  }
  let breakout = null;
  if (Number(last.c) > highestHigh) breakout = "HIGH";
  else if (Number(last.c) < lowestLow) breakout = "LOW";

  // Volume spike: last volume > 1.5x mean of last 20 volumes.
  const vols = window20.map((b) => Number(b.v));
  const avg20 = vols.reduce((s, v) => s + v, 0) / vols.length;
  const volSpike = avg20 > 0 ? Number(last.v) > 1.5 * avg20 : false;

  const lastClose = Number(last.c);
  const ema20Last = lastDefined(ema20);
  let bias = "NEUTRAL";
  if (ema20Last !== null) {
    if (lastClose > ema20Last && rsiVal > 55) bias = "BULLISH";
    else if (lastClose < ema20Last && rsiVal < 45) bias = "BEARISH";
  }

  return { ema5, ema20, rsi: rsiVal, breakout, volSpike, bias };
}

function resolvePrice(candle) {
  if (typeof candle === "number") return candle;
  if (candle === null || typeof candle !== "object") return NaN;
  const v = candle.c ?? candle.close ?? candle.price ?? candle.entry;
  return Number(v);
}

const round2 = (n) => Math.round(Number(n) * 100) / 100;

export function entryExit(candle, atrVal, bias, rr = 2) {
  const empty = { entry: null, sl: null, tgt: null, rr };
  const price = resolvePrice(candle);
  const a = Number(atrVal);
  if (!Number.isFinite(price) || !Number.isFinite(a) || a <= 0) return empty;
  const risk = 1.5 * a;
  if (bias === "BULLISH") {
    return {
      entry: round2(price),
      sl: round2(price - risk),
      tgt: round2(price + risk * Number(rr)),
      rr: Number(rr),
    };
  }
  if (bias === "BEARISH") {
    return {
      entry: round2(price),
      sl: round2(price + risk),
      tgt: round2(price - risk * Number(rr)),
      rr: Number(rr),
    };
  }
  return empty;
}

// Nifty options call builder (original heuristic, research only).
// Maps spot levels to ATM option premium using ~0.5 delta approximation.
export function optionCallDetails(chainRows, spotPrice, bias, spotEntry, spotSL, spotTgt, lot = 75) {
  const empty = { side: "NO TRADE", strike: 0, premium: 0, entryLow: 0, entryHigh: 0, sl: 0, tgt: 0, lot };
  const s = Number(spotPrice);
  if (!Array.isArray(chainRows) || !chainRows.length || !Number.isFinite(s)) return empty;
  const b = String(bias ?? "").toUpperCase();
  const isBull = b.includes("BULLISH");
  const isBear = b.includes("BEARISH");
  if (!isBull && !isBear) return empty;
  const sorted = [...chainRows].sort((x, y) => Math.abs(x.strike - s) - Math.abs(y.strike - s));
  const atm = sorted[0];
  const strike = Number(atm.strike) || 0;
  const premium = round2(isBull ? atm.callLTP : atm.putLTP);
  if (!premium || premium <= 0) return { ...empty, strike, premium };
  const entry = Number(spotEntry);
  const sl = Number(spotSL);
  const tgt = Number(spotTgt);
  const spotRisk = Number.isFinite(entry) && Number.isFinite(sl) ? Math.abs(entry - sl) : 0;
  const spotReward = Number.isFinite(entry) && Number.isFinite(tgt) ? Math.abs(tgt - entry) : 0;
  const optRisk = spotRisk > 0 ? spotRisk * 0.5 : premium * 0.3;
  const optReward = spotReward > 0 ? spotReward * 0.5 : premium * 0.6;
  return {
    side: isBull ? "BUY CE" : "BUY PE",
    strike,
    premium,
    entryLow: round2(Math.max(1, premium * 0.98)),
    entryHigh: round2(premium * 1.02),
    sl: round2(Math.max(1, premium - optRisk)),
    tgt: round2(premium + optReward),
    lot,
  };
}
