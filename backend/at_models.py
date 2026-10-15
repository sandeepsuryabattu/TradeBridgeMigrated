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

MODEL_VERSION = "at-models-v2"

# Fusion weights (seed values — retune from paper attribution).
W_TREND = 0.40
W_TRIGGER = 0.30
W_POSITIONING = 0.20
W_VOLATILITY = 0.10
FIRE_THRESHOLD = 65.0

# --- v2 fusion weights/seeds (retune from replay + paper attribution) ---
W2_TREND = 0.25
W2_TRIGGER = 0.25
W2_POSITIONING = 0.15
W2_STRENGTH = 0.15
W2_HTF = 0.10
W2_VOLATILITY = 0.10
FIRE_THRESHOLD_V2 = 70.0
MIN_WITNESSES = 2           # of 5 independent witnesses must actively agree;
                            # neutral-heavy setups (score without conviction)
                            # are vetoed as "thin-agreement"
ADX_FLOOR = 15.0          # below = drift, not trend: veto fresh entries
EXTENSION_RATIO = 2.5     # day-range vs √time expectation; blowoffs only
EXHAUST_Z = 3.0           # |vwap-distance in ATR| beyond = stretched leg
CLIMAX_RATIO = 1.3        # 5-bar slope vs 20-bar slope above = climax, not ride
HTF_RESAMPLE = 15         # higher-timeframe bar size in minutes
STRADDLE_CRUSH_PCT = 5.0  # ATM straddle collapse beyond = premium crush veto


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


# ============================ v2 models ============================

def trend_strength(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """StrengthModel: Wilder ADX regime. TREND only when ADX >= floor and
    the dominant DI agrees with the EMA-stack side; else RANGE."""
    d = engine.adx(bars)
    closes = [engine._num(b.get("c")) for b in bars]
    e5 = engine._last_defined(engine.ema(closes, 5))
    e20 = engine._last_defined(engine.ema(closes, 20))
    ema_side = 0
    if e5 is not None and e20 is not None:
        ema_side = 1 if e5 > e20 else (-1 if e5 < e20 else 0)
    di_side = 0
    if d["plus_di"] > d["minus_di"]:
        di_side = 1
    elif d["minus_di"] > d["plus_di"]:
        di_side = -1
    trending = d["adx"] >= ADX_FLOOR and ema_side != 0 and ema_side == di_side
    return {"adx": round(d["adx"], 1), "plus_di": round(d["plus_di"], 1),
            "minus_di": round(d["minus_di"], 1), "ema_side": ema_side,
            "trending": trending,
            "regime": "TREND" if trending else "RANGE"}


def higher_tf_bias(bars: List[Dict[str, Any]],
                   n: int = HTF_RESAMPLE) -> Dict[str, Any]:
    """HTFModel: resampled n-min EMA-20 side. 1-min triggers must agree."""
    tf = engine.resample(bars, n)
    closes = [engine._num(b.get("c")) for b in tf]
    e20 = engine._last_defined(engine.ema(closes, 20))
    last = closes[-1] if closes else 0.0
    bias = "FLAT"
    if e20 is not None and last:
        bias = "BULL" if last > e20 else ("BEAR" if last < e20 else "FLAT")
    return {"bias": bias, "tf_bars": len(tf), "ema20": e20, "last": last}


def extension_check(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """ExtensionModel: day-range vs √time-scaled expectation
    (median-bar-range × √N). A random-walk day stays ~1.0; a spent
    directional move prints well above — veto fresh continuation then."""
    if len(bars) < 20:
        return {"vetoed": False, "ratio": 0.0, "reason": "too few bars"}
    import math
    ranges = [engine._num(b.get("h")) - engine._num(b.get("l")) for b in bars[-20:]]
    med = median(ranges)
    day_range = max(engine._num(b.get("h")) for b in bars) - \
        min(engine._num(b.get("l")) for b in bars)
    expected = med * math.sqrt(len(bars)) if med > 0 else 0.0
    ratio = (day_range / expected) if expected > 0 else 0.0
    vetoed = ratio > EXTENSION_RATIO
    return {"vetoed": vetoed, "ratio": round(ratio, 2),
            "reason": f"day_range={day_range:.1f} expected={expected:.1f}"}


def exhaustion(bars: List[Dict[str, Any]],
               vwap: Optional[float] = None) -> Dict[str, Any]:
    """ExhaustionModel: VWAP distance in ATR units + climax check. Far from
    VWAP alone is just a trend day (price rides ATRs above VWAP for hours);
    the killer is ACCELERATION into the extreme — 5-bar slope outrunning
    the 20-bar slope means climax, and chasing a climax is what kills.
    stretched = |z| > EXHAUST_Z and climax_ratio > CLIMAX_RATIO.
    Vetoes triggers pointing AWAY from VWAP (chasing)."""
    closes = [engine._num(b.get("c")) for b in bars]
    if not closes or not vwap or vwap <= 0 or len(closes) < 21:
        return {"z": 0.0, "climax": 0.0, "stretched": False, "side": 0}
    atr_v = engine.atr(bars)
    if atr_v <= 0:
        return {"z": 0.0, "climax": 0.0, "stretched": False, "side": 0}
    z = (closes[-1] - vwap) / atr_v
    s5 = (closes[-1] - closes[-6]) / 5.0
    s20 = (closes[-1] - closes[-21]) / 20.0
    # Noise floor: when the 20-bar drift is < 0.1 ATR/bar there is no move
    # to climax against (sideways chop) — a near-zero denominator would
    # print fake climax ratios >100 and veto valid range-breaks.
    if abs(s20) < 0.1 * atr_v:
        return {"z": round(z, 2), "climax": 0.0,
                "stretched": False, "side": 1 if z > 0 else (-1 if z < 0 else 0)}
    climax = (abs(s5) / abs(s20)) if s20 else 0.0
    side = 1 if z > 0 else (-1 if z < 0 else 0)
    stretched = abs(z) > EXHAUST_Z and climax > CLIMAX_RATIO
    return {"z": round(z, 2), "climax": round(climax, 2),
            "stretched": stretched, "side": side}


def htf_exhaustion(bars: List[Dict[str, Any]],
                   n: int = HTF_RESAMPLE) -> Dict[str, Any]:
    """HTFExhaustionModel: resampled n-min RSI extremes. RSI <= 30 vetoes
    fresh BEARISH continuation (sellers spent); >= 70 vetoes BULLISH.
    Abstains (no veto) until 16+ TF bars exist."""
    tf = engine.resample(bars, n)
    closes = [engine._num(b.get("c")) for b in tf]
    if len(closes) < 16:
        return {"rsi": None, "extreme": None, "veto_bear": False,
                "veto_bull": False}
    r = engine.rsi(closes)
    extreme = "OVERSOLD" if r <= 30 else ("OVERBOUGHT" if r >= 70 else None)
    return {"rsi": round(r, 1), "extreme": extreme,
            "veto_bear": extreme == "OVERSOLD",
            "veto_bull": extreme == "OVERBOUGHT"}


# ======================= v2 positioning models =======================

def _rows_by_strike(rows: List[Dict[str, Any]]) -> Dict[float, Dict[str, Any]]:
    out: Dict[float, Dict[str, Any]] = {}
    for r in rows or []:
        try:
            out[float(r.get("strike"))] = r
        except (TypeError, ValueError):
            continue
    return out


def oi_flow(prev_rows: List[Dict[str, Any]], rows: List[Dict[str, Any]],
            spot: float, direction: Optional[str]) -> Dict[str, Any]:
    """OIFlowModel: strike-level OI-price divergence between two snapshots.

    Near-ATM/ITM strikes classify per side into buildup
    (price↑OI↑ = fresh money) vs covering/unwinding (price↑OI↓ etc).
    Score +1 when flow supports `direction`, -1 when it fights it.
    Abstains (score 0) when snapshots are unusable.
    """
    if direction not in ("BULLISH", "BEARISH"):
        return {"score": 0, "reason": "no direction"}
    prev, cur = _rows_by_strike(prev_rows), _rows_by_strike(rows)
    common = [k for k in cur if k in prev and spot and abs(k - spot) / spot <= 0.01]
    if len(common) < 3:
        return {"score": 0, "reason": "too few common strikes"}
    bull_pts = bear_pts = 0
    for k in common:
        for side, is_call in (("call", True), ("put", False)):
            p, c = prev[k], cur[k]
            d_oi = engine._num(c.get(f"{side}OI")) - engine._num(p.get(f"{side}OI"))
            d_px = engine._num(c.get(f"{side}LTP")) - engine._num(p.get(f"{side}LTP"))
            if not d_oi or not d_px:
                continue
            fresh_long = (d_px > 0 and d_oi > 0)
            fresh_short = (d_px < 0 and d_oi > 0)
            # Calls: fresh longs = bullish, fresh shorts = bearish.
            # Puts: fresh longs (price up = fear bid) = bearish, unwinding = bullish.
            if is_call:
                bull_pts += 1 if fresh_long else (0 if not fresh_short else 0)
                bear_pts += 1 if fresh_short else 0
            else:
                bear_pts += 1 if fresh_long else 0
                bull_pts += 1 if (d_px < 0 and d_oi < 0) else 0
    if direction == "BULLISH":
        score = 1 if bull_pts > bear_pts else (-1 if bear_pts > bull_pts else 0)
    else:
        score = 1 if bear_pts > bull_pts else (-1 if bull_pts > bear_pts else 0)
    return {"score": score, "reason": f"bull_flow={bull_pts} bear_flow={bear_pts}"}


def _bs_iv(price: float, s: float, k: float, t: float, r: float,
           is_call: bool) -> Optional[float]:
    """Bisection implied vol (price/S/K/T in matching units, T in years).
    Returns None when unsolvable (arbitrage-violating input)."""
    import math
    if price <= 0 or s <= 0 or k <= 0 or t <= 0:
        return None
    intrinsic = max(s - k, 0.0) if is_call else max(k - s, 0.0)
    if price < intrinsic * 0.999:
        return None
    def bs_price(v: float) -> float:
        if v <= 0:
            return intrinsic
        d1 = (math.log(s / k) + (r + 0.5 * v * v) * t) / (v * math.sqrt(t))
        d2 = d1 - v * math.sqrt(t)
        nd = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
        disc = math.exp(-r * t)
        if is_call:
            return s * nd(d1) - k * disc * nd(d2)
        return k * disc * nd(-d2) - s * nd(-d1)
    lo, hi = 0.001, 5.0
    if bs_price(hi) < price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if bs_price(mid) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def iv_skew(rows: List[Dict[str, Any]], spot: float,
            days_to_expiry: float, r: float = 0.065) -> Dict[str, Any]:
    """IVSkewModel: self-computed put-IV minus call-IV at ATM (needs no IV
    feed — Black-Scholes from leg LTPs). Rising put bid vs calls = fear =
    bearish fuel; collapsing skew = complacency. Score is directional
    context, not a trigger: +1 tailwind / -1 headwind / 0 flat."""
    t = max(float(days_to_expiry or 0.0), 1.0) / 365.0  # days → years
    best, best_d = None, float("inf")
    for row in rows or []:
        try:
            k = float(row.get("strike"))
        except (TypeError, ValueError):
            continue
        d = abs(k - (spot or 0))
        if d < best_d:
            best, best_d = row, d
    if best is None or not spot:
        return {"skew": None, "score": 0, "reason": "no atm strike"}
    try:
        k = float(best.get("strike"))
        civ = _bs_iv(engine._num(best.get("callLTP")), spot, k, t, r, True)
        piv = _bs_iv(engine._num(best.get("putLTP")), spot, k, t, r, False)
    except (TypeError, ValueError):
        return {"skew": None, "score": 0, "reason": "bad ltp"}
    if civ is None or piv is None:
        return {"skew": None, "score": 0, "reason": "iv unsolvable"}
    skew = piv - civ
    if skew > 0.02:
        score = -1  # fear bid on puts = bearish context
    elif skew < -0.02:
        score = 1
    else:
        score = 0
    return {"skew": round(skew, 4), "call_iv": round(civ, 4),
            "put_iv": round(piv, 4), "score": score,
            "reason": f"p-c={skew:.4f}"}


def straddle_momentum(prev_rows: List[Dict[str, Any]],
                      rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """StraddleModel: ATM straddle expanding (fuel for continuation) vs
    collapsing (calm — continuation entries die here)."""
    def atm_straddle(rs: List[Dict[str, Any]]) -> Optional[float]:
        atm = [r for r in rs or [] if r.get("atm")]
        pick = atm[0] if atm else (rs[0] if rs else None)
        if not pick:
            return None
        v = engine._num(pick.get("callLTP")) + engine._num(pick.get("putLTP"))
        return v if v > 0 else None
    a, b = atm_straddle(prev_rows), atm_straddle(rows)
    if a is None or b is None or a <= 0:
        return {"expanding": None, "chg_pct": 0.0}
    chg = (b - a) / a * 100.0
    return {"expanding": chg > 2.0, "chg_pct": round(chg, 1)}


def vix_regime(vix_now: Optional[float],
               vix_prev: Optional[float] = None) -> Dict[str, Any]:
    """VIXModel: ripping VIX (>20 and rising) vetoes fresh entries —
    panic moves mean-revert; crushing VIX confirms trend fuel."""
    if not vix_now or vix_now <= 0:
        return {"veto": False, "state": "unknown"}
    ripping = vix_now > 20 and vix_prev is not None and vix_now > vix_prev
    calm = vix_now < 13
    state = "panic" if ripping else ("calm" if calm else "normal")
    return {"veto": ripping, "state": state, "vix": vix_now}


def supertrend_witness(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """SupertrendModel: second trend witness, ATR-band math family."""
    return engine.supertrend(bars)


def composite_positioning(pos: Dict[str, Any], flow: Optional[Dict[str, Any]],
                          skew: Optional[Dict[str, Any]],
                          direction: Optional[str]) -> Dict[str, Any]:
    """Composite positioning vote: PCR regime + OI-flow + IV-skew aligned
    to `direction`, clamped to [-1, 1]. Skew scores absolute context
    (-1 = put fear bid = bearish), so it counts as-is for BULLISH
    composites and flipped for BEARISH ones.
    Preserves the OI-wall veto from `pos`."""
    p = max(-1, min(1, int((pos or {}).get("score", 0))))
    f = int(((flow or {}).get("score", 0) or 0))
    s = int(((skew or {}).get("score", 0) or 0))
    s_aligned = s if direction == "BULLISH" else (-s if direction == "BEARISH" else 0)
    total = max(-1, min(1, p + (f if direction else 0) + s_aligned))
    return {"score": total, "veto": bool((pos or {}).get("veto")),
            "reason": f"pcr={p} flow={f} skew={s_aligned}",
            "parts": {"pcr": p, "flow": f, "skew": s_aligned}}


def fuse_v2(trend: Dict[str, Any], trigger: Dict[str, Any],
            pos: Dict[str, Any], vol: Dict[str, Any],
            strength: Dict[str, Any], htf: Dict[str, Any],
            exh: Dict[str, Any], ext: Dict[str, Any],
            htfx: Optional[Dict[str, Any]] = None,
            flow: Optional[Dict[str, Any]] = None,
            skew: Optional[Dict[str, Any]] = None,
            strad: Optional[Dict[str, Any]] = None,
            vixr: Optional[Dict[str, Any]] = None,
            stw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Weighted v2 fusion with hard vetoes. Fires only when every gate
    below holds; score must clear FIRE_THRESHOLD_V2."""
    t = max(-2, min(2, int(trend.get("score", 0))))
    trig = bool(trigger.get("fire"))
    p = max(-1, min(1, int(pos.get("score", 0))))
    v = bool(vol.get("ok"))
    s = 1.0 if strength.get("trending") else 0.0
    direction = trigger.get("direction")
    htf_bias = htf.get("bias")
    # Young session (< ~1 HTF EMA window) reports FLAT: abstain, don't veto.
    # An abstaining witness must never silence the engine (same rule as
    # htf_exhaustion). Only a REAL opposing bias vetoes.
    htf_agree = htf_bias == "FLAT" or \
        ((direction == "BULLISH" and htf_bias == "BULL") or
         (direction == "BEARISH" and htf_bias == "BEAR"))
    exh_chase = bool(exh.get("stretched")) and (
        (direction == "BULLISH" and exh.get("side", 0) > 0) or
        (direction == "BEARISH" and exh.get("side", 0) < 0))
    vetoes: List[str] = []
    if not trig:
        vetoes.append("no-trigger")
    if not v:
        vetoes.append("chop")
    if pos.get("veto"):
        vetoes.append("oi-wall")
    # A fresh Donchian range-break IS a trend being born — ADX is
    # legitimately low at birth. The no-trend veto applies only to
    # mid-range EMA-cross continuations (chasing without strength).
    reason = str(trigger.get("reason") or "")
    range_break = "breakout=HIGH" in reason or "breakout=LOW" in reason
    if not strength.get("trending") and not range_break:
        vetoes.append("no-trend")
    if not htf_agree:
        vetoes.append("htf-disagree")
    if exh_chase:
        vetoes.append("exhaust-chase")
    if ext.get("vetoed"):
        vetoes.append("extended")
    htfx = htfx or {}
    if direction == "BEARISH" and htfx.get("veto_bear"):
        vetoes.append("htf-oversold")
    if direction == "BULLISH" and htfx.get("veto_bull"):
        vetoes.append("htf-overbought")
    # Supertrend witness: different math family (ATR bands) from the EMA
    # stack. A real opposing read vetoes; None (young tape) abstains.
    stw = stw or {}
    st_dir = stw.get("direction")
    if ((direction == "BULLISH" and st_dir == "DOWN") or
            (direction == "BEARISH" and st_dir == "UP")):
        vetoes.append("st-disagree")
    vixr = vixr or {}
    if vixr.get("veto"):
        vetoes.append("vix-panic")
    strad = strad or {}
    if strad.get("expanding") is False and \
            abs(strad.get("chg_pct", 0.0)) > STRADDLE_CRUSH_PCT:
        vetoes.append("straddle-crush")
    agree = (direction == "BULLISH" and t > 0) or \
        (direction == "BEARISH" and t < 0)
    if not agree:
        vetoes.append("trend-disagree")
    # Multi-witness agreement: neutral witnesses must not ride free. At
    # least MIN_WITNESSES of the five must ACTIVELY support the direction
    # (abstain/neutral counts as silence, opposition is vetoed above).
    _bull = direction == "BULLISH"
    wit = 0
    if bool(strength.get("trending")):
        wit += 1
    if htf_bias == ("BULL" if _bull else "BEAR"):
        wit += 1
    if stw.get("direction") == ("UP" if _bull else "DOWN"):
        wit += 1
    _p = int(pos.get("score", 0))
    if (_bull and _p > 0) or (not _bull and _p < 0):
        wit += 1
    _f = int((flow or {}).get("score", 0))
    if (_bull and _f > 0) or (not _bull and _f < 0):
        wit += 1
    if wit < MIN_WITNESSES:
        vetoes.append("thin-agreement")
    score = 100.0 * (W2_TREND * (t + 2) / 4 + W2_TRIGGER * (1.0 if trig else 0.0)
                      + W2_POSITIONING * (p + 1) / 2 + W2_STRENGTH * s
                      + W2_HTF * (1.0 if htf_agree else 0.0)
                      + W2_VOLATILITY * (1.0 if v else 0.0))
    fire = not vetoes and score >= FIRE_THRESHOLD_V2
    return {"fire": fire, "score": round(score, 1),
            "direction": direction if fire else None,
            "version": MODEL_VERSION, "vetoes": vetoes,
            "detail": {"trend": t, "trigger": trig, "positioning": p,
                       "vol_ok": v, "trending": bool(s),
                       "witnesses": wit,
                       "htf": htf_bias, "htf_agree": htf_agree,
                       "vwap_z": exh.get("z"), "ext_ratio": ext.get("ratio"),
                       "rsi15": htfx.get("rsi"), "agree": agree,
                       "oi_flow": (flow or {}).get("score"),
                       "iv_skew": (skew or {}).get("skew"),
                       "straddle_chg": strad.get("chg_pct"),
                       "supertrend": (stw or {}).get("direction"),
                       "vix": vixr.get("state")}}
