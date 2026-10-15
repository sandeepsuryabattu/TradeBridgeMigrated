"""Day-regime classifier + early-entry setups (pure, tested).

Anticipation over confirmation: the old trigger fires ON the breakout bar
(often the local extreme). This module answers two earlier questions:

1. What KIND of day is it? (TREND-DAY-CANDIDATE vs CHOP, from the first
   30-45 minutes) — confirmation entries are only allowed on trend days.
2. Is there a coil-and-release setup? (squeeze + reclaim) — the only
   entries allowed on chop days.

Conventions mirror autotrender_engine: candles are {o,h,l,c,v} dicts.
All functions are pure: bars in, dict out. Thresholds are seeds — retune
from replay + paper attribution.
"""
from statistics import median
from typing import Any, Dict, List, Optional

from . import autotrender_engine as engine

# --- seeds (retune from replay + paper attribution) ---
CLASSIFY_MIN_BARS = 15      # need at least this many session bars to label
DISPLACE_TREND = 0.55       # |last-open|/day-range above = driving, not rotating
VWAP_PERSIST = 0.60          # fraction of recent bars on one VWAP side = one-sided day
SQUEEZE_LOOKBACK = 20       # bars for coil measurement
SQUEEZE_RATIO = 0.50        # recent range < 50% of median range = coiled
RECLAIM_LOOKBACK = 3        # bars within which the reclaim must hold
REENTRY_HOLD = 3            # bars the reclaim of entry must hold
REENTRY_BUFFER = 0.0005     # must clear entry by this fraction (noise filter)


def _closes(bars: List[Dict[str, Any]]) -> List[float]:
    return [engine._num(b.get("c")) for b in bars]


def _session_vwap(bars: List[Dict[str, Any]]) -> Optional[float]:
    num = sum(engine._num(b.get("c")) * engine._num(b.get("v")) for b in bars)
    den = sum(engine._num(b.get("v")) for b in bars)
    return (num / den) if den > 0 else None


def classify(bars: List[Dict[str, Any]],
             prev_close: Optional[float] = None) -> Dict[str, Any]:
    """Label the day from session bars so far.

    Returns {"label": "TREND-DAY-CANDIDATE"|"CHOP"|"UNDECIDED",
             "parts": {...}, "reasons": [...]}.
    """
    parts: Dict[str, Any] = {}
    reasons: List[str] = []
    if not bars or len(bars) < CLASSIFY_MIN_BARS:
        return {"label": "UNDECIDED", "parts": {"bars": len(bars or [])},
                "reasons": ["too early (<15 bars)"]}

    opens = engine._num(bars[0].get("o")) or _closes(bars)[0]
    closes = _closes(bars)
    last = closes[-1]
    hi = max(engine._num(b.get("h")) for b in bars)
    lo = min(engine._num(b.get("l")) for b in bars)
    day_range = hi - lo if hi > lo else 0.0

    displace = abs(last - opens) / day_range if day_range > 0 else 0.0
    parts["displacement"] = round(displace, 3)
    parts["direction"] = ("UP" if last > opens else
                          "DOWN" if last < opens else "FLAT")

    vw = _session_vwap(bars)
    window = closes[-20:]
    if vw is not None and vw > 0:
        tol = vw * 0.0005
        above = sum(1 for c in window if c > vw + tol)
        below = sum(1 for c in window if c < vw - tol)
        persist = max(above, below) / len(window)
        parts["vwap_persist"] = round(persist, 3)
        parts["vwap_side"] = ("ABOVE" if above >= below else "BELOW")
    else:
        persist = 0.0
        parts["vwap_persist"] = 0.0
        parts["vwap_side"] = "UNKNOWN"

    if prev_close:
        gap = (opens - prev_close) / prev_close
        parts["gap_pct"] = round(gap * 100, 3)
        if abs(gap) > 0.004:
            reasons.append(f"gap {gap * 100:+.2f}%")

    votes_trend = sum([displace >= DISPLACE_TREND, persist >= VWAP_PERSIST])
    if votes_trend == 2:
        label = "TREND-DAY-CANDIDATE"
        reasons.append(f"driving (disp={displace:.2f}) + one-sided "
                       f"(persist={persist:.2f})")
    elif votes_trend == 0:
        label = "CHOP"
        reasons.append(f"rotating (disp={displace:.2f}) + two-sided "
                       f"(persist={persist:.2f})")
    else:
        label = "UNDECIDED"
        reasons.append(f"split vote (disp={displace:.2f}, persist={persist:.2f})")
    parts["bars"] = len(bars)
    return {"label": label, "parts": parts, "reasons": reasons}


def squeeze(bars: List[Dict[str, Any]],
            lookback: int = SQUEEZE_LOOKBACK) -> Dict[str, Any]:
    """Coil detector: recent range vs median range = compression before release.

    Returns {"coiled": bool, "ratio": float, "recent_range": float}.
    """
    if not bars or len(bars) < lookback * 2:
        return {"coiled": False, "ratio": 1.0, "recent_range": 0.0,
                "reason": "too few bars"}
    spans = [engine._num(b.get("h")) - engine._num(b.get("l")) for b in bars]
    recent = spans[-lookback:]
    prior = spans[:-lookback]
    recent_range = max(recent) if recent else 0.0
    base = median(prior) if prior else 0.0
    ratio = (recent_range / base) if base > 0 else 1.0
    return {"coiled": bool(ratio < SQUEEZE_RATIO),
            "ratio": round(ratio, 3),
            "recent_range": round(recent_range, 2)}


def reclaim(bars: List[Dict[str, Any]],
            lookback: int = RECLAIM_LOOKBACK) -> Dict[str, Any]:
    """Reclaim setup: price retook session VWAP within the last `lookback`
    bars after trading the opposite side — enter the TEST, not the spike.

    Returns {"setup": bool, "side": "BULLISH"|"BEARISH"|None, "reason": str}.
    """
    if not bars or len(bars) < 10:
        return {"setup": False, "side": None, "reason": "too few bars"}
    vw = _session_vwap(bars)
    if vw is None or vw <= 0:
        return {"setup": False, "side": None, "reason": "no vwap"}
    tol = vw * 0.0005
    closes = _closes(bars)
    tail = closes[-lookback:]
    head = closes[:-lookback]
    tail_above = all(c > vw - tol for c in tail) and tail[-1] > vw + tol
    tail_below = all(c < vw + tol for c in tail) and tail[-1] < vw - tol
    was_below = any(c < vw - tol for c in head[-10:])
    was_above = any(c > vw + tol for c in head[-10:])
    if tail_above and was_below:
        return {"setup": True, "side": "BULLISH",
                "reason": "reclaimed vwap from below, holding"}
    if tail_below and was_above:
        return {"setup": True, "side": "BEARISH",
                "reason": "lost vwap from above, holding under"}
    return {"setup": False, "side": None, "reason": "no reclaim"}


def reentry_setup(bars: List[Dict[str, Any]], entry: float,
                  direction: str, hold: int = REENTRY_HOLD) -> Dict[str, Any]:
    """Second-chance entry: price stopped us out, then reclaimed the entry
    level and is HOLDING above it (BULLISH) — "I was right, just early".

    Returns {"setup": bool, "reason": str}. The hold requirement (not the
    first reclaim tick) is what separates re-entry from revenge trading.
    """
    if not bars or len(bars) < hold + 1 or not entry:
        return {"setup": False, "reason": "too few bars"}
    closes = _closes(bars)
    tail = closes[-hold:]
    buf = abs(entry) * REENTRY_BUFFER
    if direction == "BULLISH":
        holding = all(c > entry + buf for c in tail)
        was_under = any(c < entry for c in closes[:-hold][-15:])
        if holding and was_under:
            return {"setup": True,
                    "reason": f"reclaimed entry {entry:.1f}, holding {hold} bars"}
        return {"setup": False,
                "reason": "no hold above entry" if not holding else "never lost entry"}
    if direction == "BEARISH":
        holding = all(c < entry - buf for c in tail)
        was_over = any(c > entry for c in closes[:-hold][-15:])
        if holding and was_over:
            return {"setup": True,
                    "reason": f"reclaimed entry {entry:.1f} from above, "
                              f"holding {hold} bars"}
        return {"setup": False,
                "reason": "no hold under entry" if not holding else "never lost entry"}
    return {"setup": False, "reason": "no direction"}


def recheck(prior_label: str, direction: Optional[str],
            bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Intraday flip check: is the morning thesis still alive?

    Kills a TREND-DAY-CANDIDATE when price is back on the wrong side of
    VWAP (thesis dead) — caller then downgrades to chop rules immediately.
    """
    if prior_label != "TREND-DAY-CANDIDATE" or not bars:
        return {"flip": False, "label": prior_label, "reason": "no thesis"}
    vw = _session_vwap(bars)
    if vw is None or vw <= 0:
        return {"flip": False, "label": prior_label, "reason": "no vwap"}
    last = _closes(bars)[-1]
    tol = vw * 0.0005
    dead = ((direction == "UP" and last < vw - tol)
            or (direction == "DOWN" and last > vw + tol))
    if dead:
        return {"flip": True, "label": "CHOP",
                "reason": f"thesis dead: price {last:.1f} wrong side of "
                          f"vwap {vw:.1f}"}
    return {"flip": False, "label": prior_label, "reason": "thesis alive"}
