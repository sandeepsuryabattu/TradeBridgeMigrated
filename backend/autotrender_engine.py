"""
Autotrender engine — pure technical-analysis helpers (no React, no I/O, no trading).

Faithful Python port of autotrender-personal's src/services/marketData.js
(derivatives) and src/services/patterns.js (indicators). Behaviour, thresholds
and rounding match the JS originals so calls reproduce 1:1.

Conventions:
- EMA series use None for warm-up bars (JS null).
- round2() emulates JS Math.round (half up) to 2 decimals.
- Candles are dicts {o,h,l,c,v} (+ optional t). v may be tick-count when
  exchange volume is unavailable (volume-spike then means activity-spike).
"""
import math
from typing import Any, Dict, List, Optional

LOT_NIFTY = 75  # NSE lot changes periodically — keep configurable per call.


def round2(n: Any) -> float:
    try:
        x = float(n)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(x):
        return 0.0
    # JS Math.round: half up toward +inf.
    return math.floor(x * 100 + 0.5) / 100


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


# ── Indicators ──────────────────────────────────────────────────────────────

def ema(values: List[Any], period: int) -> List[Optional[float]]:
    if not values:
        return []
    if not isinstance(period, int) or period <= 0:
        return []
    k = 2 / (period + 1)
    out: List[Optional[float]] = [None] * len(values)
    seed_count = min(period, len(values))
    sma = sum(_num(values[i]) for i in range(seed_count)) / seed_count
    for i in range(len(values)):
        if i < seed_count - 1:
            out[i] = None
        elif i == seed_count - 1:
            out[i] = sma
        else:
            prev = out[i - 1]
            out[i] = _num(values[i]) * k + (prev if prev is not None else sma) * (1 - k)
    return out


def _last_defined(arr: List[Any]) -> Optional[float]:
    for v in reversed(arr):
        if v is not None:
            x = _num(v, default=float("nan"))
            if math.isfinite(x):
                return x
    return None


def rsi(closes: List[Any], period: int = 14) -> float:
    if not closes or len(closes) < period + 1:
        return 50.0
    cs = [_num(c) for c in closes]
    avg_gain = 0.0
    avg_loss = 0.0
    for i in range(1, len(cs)):
        diff = cs[i] - cs[i - 1]
        g = diff if diff > 0 else 0.0
        ll = -diff if diff < 0 else 0.0
        if i <= period:
            avg_gain += g / period
            avg_loss += ll / period
        else:
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + ll) / period
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _true_range(cur: Dict[str, Any], prev_close: Optional[float]) -> float:
    h = _num(cur.get("h"))
    ll = _num(cur.get("l"))
    c = _num(cur.get("c")) if prev_close is None else prev_close
    return max(h - ll, abs(h - c), abs(ll - c))


def atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if not candles or not isinstance(period, int) or period <= 0:
        return 0.0
    trs = []
    for i, cur in enumerate(candles):
        prev = None if i == 0 else _num(candles[i - 1].get("c"))
        trs.append(_true_range(cur, prev))
    n = min(period, len(trs))
    a = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
    return a


def detect(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pattern scan over >= 20 candles. Raises ValueError if fewer."""
    if not candles or len(candles) < 20:
        raise ValueError("detect requires at least 20 candles")
    closes = [_num(k.get("c")) for k in candles]
    ema5 = ema(closes, 5)
    ema20 = ema(closes, 20)
    rsi_val = rsi(closes, 14)

    window20 = candles[-20:]
    prior = window20[:-1]
    last = window20[-1]
    highest_high = max(_num(b.get("h")) for b in prior)
    lowest_low = min(_num(b.get("l")) for b in prior)
    last_c = _num(last.get("c"))
    breakout = None
    if last_c > highest_high:
        breakout = "HIGH"
    elif last_c < lowest_low:
        breakout = "LOW"

    vols = [_num(b.get("v")) for b in window20]
    avg20 = sum(vols) / len(vols)
    vol_spike = avg20 > 0 and _num(last.get("v")) > 1.5 * avg20

    ema20_last = _last_defined(ema20)
    bias = "NEUTRAL"
    if ema20_last is not None:
        if last_c > ema20_last and rsi_val > 55:
            bias = "BULLISH"
        elif last_c < ema20_last and rsi_val < 45:
            bias = "BEARISH"
    return {"ema5": ema5, "ema20": ema20, "rsi": rsi_val,
            "breakout": breakout, "vol_spike": vol_spike, "bias": bias}


def _resolve_price(candle: Any) -> float:
    if isinstance(candle, (int, float)):
        return float(candle)
    if not isinstance(candle, dict):
        return float("nan")
    for k in ("c", "close", "price", "entry"):
        if candle.get(k) is not None:
            return _num(candle[k], default=float("nan"))
    return float("nan")


def entry_exit(candle: Any, atr_val: Any, bias: Any, rr: float = 2.0) -> Dict[str, Any]:
    empty = {"entry": None, "sl": None, "tgt": None, "rr": rr}
    price = _resolve_price(candle)
    a = _num(atr_val, default=float("nan"))
    if not math.isfinite(price) or not math.isfinite(a) or a <= 0:
        return empty
    risk = 1.5 * a
    rr = float(rr)
    if bias == "BULLISH":
        return {"entry": round2(price), "sl": round2(price - risk),
                "tgt": round2(price + risk * rr), "rr": rr}
    if bias == "BEARISH":
        return {"entry": round2(price), "sl": round2(price + risk),
                "tgt": round2(price - risk * rr), "rr": rr}
    return empty


# ── Derivatives (option chain rows: strike/callOI/putOI/callChgOI/...) ───────

def _rows_sum(rows: List[Dict[str, Any]], key: str) -> float:
    total = 0.0
    for r in rows or []:
        total += _num((r or {}).get(key))
    return total


def calc_pcr(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    call = _rows_sum(rows, "callOI")
    put = _rows_sum(rows, "putOI")
    if not call:
        return 0.0
    return round2(put / call)


def calc_pcr_chg(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    call = _rows_sum(rows, "callChgOI")
    put = _rows_sum(rows, "putChgOI")
    if not call:
        return 0.0
    return round2(put / call)


def gen_signal(pcr: Any) -> str:
    try:
        v = float(pcr)
    except (TypeError, ValueError):
        return "NEUTRAL"
    if not math.isfinite(v):
        return "NEUTRAL"
    if v > 1.25:
        return "STRONG BUY"
    if v > 1.05:
        return "BUY"
    if v < 0.75:
        return "STRONG SELL"
    if v < 0.95:
        return "SELL"
    return "NEUTRAL"


def get_support_resistance(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"support": 0, "supportOI": 0, "resistance": 0, "resistanceOI": 0}
    sup = rows[0] or {}
    res = rows[0] or {}
    for r in rows:
        r = r or {}
        if _num(r.get("putOI")) > _num(sup.get("putOI")):
            sup = r
        if _num(r.get("callOI")) > _num(res.get("callOI")):
            res = r
    return {"support": _num(sup.get("strike")), "supportOI": _num(sup.get("putOI")),
            "resistance": _num(res.get("strike")), "resistanceOI": _num(res.get("callOI"))}


def max_pain(rows: List[Dict[str, Any]]) -> float:
    """Strike minimising total option-buyer payout at expiry (call/put OI weighted)."""
    if not rows:
        return 0.0
    strikes = sorted({_num(r.get("strike")) for r in (rows or []) if r})
    best, best_cost = 0.0, None
    for s in strikes:
        cost = 0.0
        for r in rows:
            r = r or {}
            k = _num(r.get("strike"))
            cost += max(k - s, 0) * _num(r.get("callOI"))
            cost += max(s - k, 0) * _num(r.get("putOI"))
        if best_cost is None or cost < best_cost:
            best, best_cost = s, cost
    return best


def straddle(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    atm = next((r for r in rows if r and r.get("atm")), None)
    if atm is None:
        atm = rows[len(rows) // 2]
    return round2(_num(atm.get("callLTP")) + _num(atm.get("putLTP")))


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"totalCallOI": _rows_sum(rows, "callOI"),
            "totalPutOI": _rows_sum(rows, "putOI"),
            "totalCallChg": _rows_sum(rows, "callChgOI"),
            "totalPutChg": _rows_sum(rows, "putChgOI"),
            "pcr": calc_pcr(rows), "pcrChg": calc_pcr_chg(rows)}


def calc_position(capital: Any, risk_pct: Any, entry: Any, stop: Any) -> Dict[str, Any]:
    cap = _num(capital)
    risk = _num(risk_pct)
    en = _num(entry)
    st = _num(stop)
    risk_amt = round2(cap * risk / 100)
    per_share = abs(en - st)
    if not per_share or risk_amt <= 0:
        return {"riskAmt": risk_amt, "qty": 0, "positionValue": 0}
    qty = math.floor(risk_amt / per_share)
    return {"riskAmt": risk_amt, "qty": qty, "positionValue": round2(qty * en)}


def option_call_details(chain_rows: List[Dict[str, Any]], spot_price: Any,
                        bias: Any, spot_entry: Any, spot_sl: Any,
                        spot_tgt: Any, lot: int = LOT_NIFTY) -> Dict[str, Any]:
    """Map spot levels to the ATM option premium (~0.5 delta approximation).

    Research-only heuristic, mirrors the JS original 1:1.
    """
    empty = {"side": "NO TRADE", "strike": 0, "premium": 0,
             "entryLow": 0, "entryHigh": 0, "sl": 0, "tgt": 0, "lot": lot}
    s = _num(spot_price, default=float("nan"))
    if not chain_rows or not math.isfinite(s):
        return empty
    b = str(bias or "").upper()
    is_bull = "BULLISH" in b
    is_bear = "BEARISH" in b
    if not is_bull and not is_bear:
        return empty
    atm = min(chain_rows, key=lambda r: abs(_num((r or {}).get("strike")) - s))
    strike = _num(atm.get("strike"))
    premium = round2(atm.get("callLTP") if is_bull else atm.get("putLTP"))
    if not premium or premium <= 0:
        return {**empty, "strike": strike, "premium": premium}
    entry = _num(spot_entry, default=float("nan"))
    sl = _num(spot_sl, default=float("nan"))
    tgt = _num(spot_tgt, default=float("nan"))
    spot_risk = abs(entry - sl) if math.isfinite(entry) and math.isfinite(sl) else 0
    spot_reward = abs(tgt - entry) if math.isfinite(entry) and math.isfinite(tgt) else 0
    opt_risk = spot_risk * 0.5 if spot_risk > 0 else premium * 0.3
    opt_reward = spot_reward * 0.5 if spot_reward > 0 else premium * 0.6
    return {"side": "BUY CE" if is_bull else "BUY PE",
            "strike": strike, "premium": premium,
            "entryLow": round2(max(1, premium * 0.98)),
            "entryHigh": round2(premium * 1.02),
            "sl": round2(max(1, premium - opt_risk)),
            "tgt": round2(premium + opt_reward), "lot": lot}
