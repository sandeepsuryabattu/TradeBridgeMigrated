"""
Proprietary signal models — versioned, pure, tested.

These are OUR models (original heuristics, not copied from any vendor).
Every generated call stores model_version + per-model scores so paper P&L
can be attributed per model and thresholds retuned from data — see module
docstring of autotrender.py scheduler. Starting weights/thresholds are
textbook seeds (a hunch until paper data confirms); the PROCESS around
them (confluence, vetoes, kill rules) is the solid part.

Conventions mirror autotrender_engine: candles are {o,h,l,c,v} dicts,
chain rows are engine-ready (strike/callOI/putOI/.../atm).
"""
from statistics import median
from typing import Any, Dict, List, Optional

from . import autotrender_engine as engine

MODEL_VERSION = "at-models-v1"

# Fusion weights (seed values — retune from paper attribution).
W_TREND = 0.40
W_TRIGGER = 0.30
W_POSITIONING = 0.20
W_VOLATILITY = 0.10
FIRE_THRESHOLD = 65.0


def session_vwap(bars: List[Dict[str, Any]]) -> Optional[float]:
    """Volume-weighted average price over the given bars (tick-count weights).

    Returns None when there is no volume (all v == 0).
    """
    num = sum(engine._num(b.get("c")) * engine._num(b.get("v")) for b in (bars or []))
    den = sum(engine._num(b.get("v")) for b in (bars or []))
    if den <= 0:
        return None
    return num / den


def trend_score(bars: List[Dict[str, Any]], vwap: Optional[float] = None) -> Dict[str, Any]:
    """TrendModel: EMA stack (±1) + VWAP side (±1, 5bp tolerance). Range -2..+2."""
    closes = [engine._num(b.get("c")) for b in bars]
    e5 = engine._last_defined(engine.ema(closes, 5))
    e20 = engine._last_defined(engine.ema(closes, 20))
    last = closes[-1] if closes else 0.0
    parts: Dict[str, int] = {}
    if e5 is not None and e20 is not None:
        if last > e5 > e20:
            parts["ema_stack"] = 1
        elif last < e5 < e20:
            parts["ema_stack"] = -1
        else:
            parts["ema_stack"] = 0
    else:
        parts["ema_stack"] = 0
    if vwap is not None and vwap > 0:
        tol = vwap * 0.0005
        if last > vwap + tol:
            parts["vwap_side"] = 1
        elif last < vwap - tol:
            parts["vwap_side"] = -1
        else:
            parts["vwap_side"] = 0
    else:
        parts["vwap_side"] = 0
    score = max(-2, min(2, parts["ema_stack"] + parts["vwap_side"]))
    return {"score": score, "parts": parts}


def _crossed_up(closes: List[float], lookback: int = 3) -> bool:
    e5 = engine.ema(closes, 5)
    e20 = engine.ema(closes, 20)
    for i in range(max(1, len(closes) - lookback), len(closes)):
        a_now, b_now = e5[i], e20[i]
        a_prv, b_prv = e5[i - 1], e20[i - 1]
        if None in (a_now, b_now, a_prv, b_prv):
            continue
        if a_prv <= b_prv and a_now > b_now:
            return True
    return False


def _crossed_down(closes: List[float], lookback: int = 3) -> bool:
    e5 = engine.ema(closes, 5)
    e20 = engine.ema(closes, 20)
    for i in range(max(1, len(closes) - lookback), len(closes)):
        a_now, b_now = e5[i], e20[i]
        a_prv, b_prv = e5[i - 1], e20[i - 1]
        if None in (a_now, b_now, a_prv, b_prv):
            continue
        if a_prv >= b_prv and a_now < b_now:
            return True
    return False


def trigger_ok(found: Dict[str, Any], bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """TriggerModel: entry timer. Breakout or fresh EMA cross + RSI/volume confirm.

    Returns {"fire": bool, "direction": "BULLISH"|"BEARISH"|None, "reason": str}.
    """
    closes = [engine._num(b.get("c")) for b in bars]
    rsi = float(found.get("rsi", 50))
    vol = bool(found.get("vol_spike"))
    brk = found.get("breakout")
    if (brk == "HIGH" or _crossed_up(closes)) and (vol or rsi > 60):
        return {"fire": True, "direction": "BULLISH",
                "reason": f"breakout={brk} cross_up rsi={rsi:.1f} vol_spike={vol}"}
    if (brk == "LOW" or _crossed_down(closes)) and (vol or rsi < 40):
        return {"fire": True, "direction": "BEARISH",
                "reason": f"breakout={brk} cross_down rsi={rsi:.1f} vol_spike={vol}"}
    return {"fire": False, "direction": None, "reason": "no trigger"}


def positioning(rows: List[Dict[str, Any]], spot: float,
                direction: Optional[str]) -> Dict[str, Any]:
    """PositioningModel: OI regime tailwind/headwind + OI-wall veto.

    Score in {-1, 0, 1} relative to `direction`. Veto fires when charging
    into a dominant opposing OI wall (resistance OI > 2x support OI within
    0.5% above spot for longs, mirrored for shorts). Documented heuristic.
    """
    if direction not in ("BULLISH", "BEARISH"):
        return {"score": 0, "veto": False, "reason": "no direction"}
    pcr = engine.calc_pcr(rows)
    sr = engine.get_support_resistance(rows)
    is_bull = direction == "BULLISH"
    if is_bull:
        score = 1 if pcr > 1.05 else (-1 if pcr < 0.95 else 0)
    else:
        score = 1 if pcr < 0.95 else (-1 if pcr > 1.05 else 0)
    veto, wall = False, None
    if spot and spot > 0:
        if is_bull and sr["resistance"] > 0:
            dist = (sr["resistance"] - spot) / spot
            if 0 <= dist <= 0.005 and sr["resistanceOI"] > 2 * max(sr["supportOI"], 1):
                veto, wall = True, sr["resistance"]
        if not is_bull and sr["support"] > 0:
            dist = (spot - sr["support"]) / spot
            if 0 <= dist <= 0.005 and sr["supportOI"] > 2 * max(sr["resistanceOI"], 1):
                veto, wall = True, sr["support"]
    reason = f"pcr={pcr} wall={wall} veto={veto}"
    return {"score": score, "veto": veto, "reason": reason}


def volatility_gate(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """VolatilityModel: veto chop. Fires when the last bar's range is under
    35% of the median range of the last 20 bars (compression = chop)."""
    if len(bars) < 20:
        return {"ok": False, "reason": "too few bars"}
    ranges = [engine._num(b.get("h")) - engine._num(b.get("l")) for b in bars[-20:]]
    med = median(ranges)
    last = ranges[-1]
    if med <= 0:
        return {"ok": True, "reason": "no range data"}
    if last < 0.35 * med:
        return {"ok": False, "reason": f"chop: last_range={last:.2f} med={med:.2f}"}
    return {"ok": True, "reason": f"range_ok last={last:.2f} med={med:.2f}"}


def fuse(trend: Dict[str, Any], trigger: Dict[str, Any],
         pos: Dict[str, Any], vol: Dict[str, Any]) -> Dict[str, Any]:
    """Weighted fusion. Fires only on trigger + vol-ok + no veto + score>=65
    + trend/trigger direction agreement."""
    t = max(-2, min(2, int(trend.get("score", 0))))
    trig = bool(trigger.get("fire"))
    p = max(-1, min(1, int(pos.get("score", 0))))
    v = bool(vol.get("ok"))
    score = 100.0 * (W_TREND * (t + 2) / 4 + W_TRIGGER * (1.0 if trig else 0.0)
                     + W_POSITIONING * (p + 1) / 2 + W_VOLATILITY * (1.0 if v else 0.0))
    direction = trigger.get("direction")
    agree = (direction == "BULLISH" and t > 0) or (direction == "BEARISH" and t < 0)
    fire = trig and v and not pos.get("veto") and score >= FIRE_THRESHOLD and agree
    return {"fire": fire, "score": round(score, 1), "direction": direction if fire else None,
            "version": MODEL_VERSION,
            "detail": {"trend": t, "trigger": trig, "positioning": p,
                       "vol_ok": v, "veto": bool(pos.get("veto")), "agree": agree}}
