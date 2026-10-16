"""Session location layer: Initial Balance + developing value area.

Pure functions (bars in, dicts out) implementing the Vtrender-style auction
read from 1-minute bars: WHERE is price trading relative to the morning's
Initial Balance and the developing value area (POC/VAH/VAL)?

Location never fires on its own — it votes as a fusion witness that
confirms (or abstains on) the trigger direction:

- BULLISH confirmed when price is ACCEPTED above value, or holding above
  the developing POC inside value.
- BEARISH mirror.
- Anything else (below value on a long signal, thin tape) abstains: an
  abstaining witness must never silence the engine.

Conventions mirror day_regime: candles are {o,h,l,c,v} dicts.
Thresholds are seeds — retune from replay + paper attribution.
"""
from typing import Any, Dict, List, Optional

IB_BARS = 60            # first-hour range = Initial Balance (1-min bars)
READY_BARS = 65         # need IB complete + a little development to vote
VALUE_FRAC = 0.70       # value area holds 70% of session volume (classic)
MIN_BIN_WIDTH = 0.05    # never bin finer than the tick grid


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _value_area(bars: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Volume profile over the session: POC + 70% value area. None if empty."""
    rows = [(max(_num(b.get("h")), _num(b.get("l"))) +
             min(_num(b.get("h")), _num(b.get("l"))) +
             _num(b.get("c"))) / 3.0 for b in bars]
    vols = [_num(b.get("v")) for b in bars]
    total = sum(vols)
    if total <= 0 or not rows:
        return None
    span = max(rows) - min(rows)
    width = max(MIN_BIN_WIDTH, span / 100.0) if span > 0 else MIN_BIN_WIDTH
    lo = min(rows)
    bins: Dict[int, float] = {}
    for px, v in zip(rows, vols):
        bins[int((px - lo) / width)] = bins.get(int((px - lo) / width), 0.0) + v
    poc_bin = max(bins, key=lambda k: bins[k])
    area = bins[poc_bin]
    up, dn = poc_bin + 1, poc_bin - 1
    top, bottom = max(bins), min(bins)
    while area < total * VALUE_FRAC and (up <= top or dn >= bottom):
        if bins.get(up, 0.0) >= bins.get(dn, 0.0):
            area += bins.get(up, 0.0)
            up += 1
        else:
            area += bins.get(dn, 0.0)
            dn -= 1
    return {"poc": round(lo + (poc_bin + 0.5) * width, 2),
            "vah": round(lo + up * width, 2),
            "val": round(lo + dn * width, 2)}


def locate(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Auction location from session bars so far. Never raises."""
    try:
        n = len(bars or [])
        if n < READY_BARS:
            ib = bars[:IB_BARS] if n >= IB_BARS else []
            return {"ready": False, "bars": n, "reason": "developing",
                    "ib_high": (max(_num(b.get("h")) for b in ib)
                                if ib else None),
                    "ib_low": (min(_num(b.get("l")) for b in ib)
                               if ib else None)}
        ib = bars[:IB_BARS]
        ib_high = max(_num(b.get("h")) for b in ib)
        ib_low = min(_num(b.get("l")) for b in ib)
        va = _value_area(bars)
        last = _num(bars[-1].get("c"))
        if va is None:
            return {"ready": False, "bars": n, "reason": "no volume",
                    "ib_high": ib_high, "ib_low": ib_low}
        if last > va["vah"]:
            location = "ABOVE_VALUE"
        elif last < va["val"]:
            location = "BELOW_VALUE"
        else:
            location = "IN_VALUE"
        if last > ib_high:
            ib_pos = "ABOVE"
        elif last < ib_low:
            ib_pos = "BELOW"
        else:
            ib_pos = "INSIDE"
        return {"ready": True, "bars": n, "ib_high": ib_high,
                "ib_low": ib_low, "poc": va["poc"], "vah": va["vah"],
                "val": va["val"], "last": last, "location": location,
                "ib_pos": ib_pos,
                "poc_side": ("ABOVE" if last > va["poc"] else
                             "BELOW" if last < va["poc"] else "AT")}
    except Exception:
        return {"ready": False, "bars": len(bars or []), "reason": "error"}


def location_vote(loc: Dict[str, Any], direction: Optional[str]) -> Dict[str, Any]:
    """Fusion witness vote for a trigger direction. Pure. Never raises.

    +1 confirms acceptance in the trigger direction, -1 never (location
    only confirms, opposing reads abstain), 0 abstains.
    """
    try:
        loc = loc or {}
        if not loc.get("ready"):
            return {"score": 0, "reason": "developing"}
        where = loc.get("location")
        above_poc = loc.get("poc_side") == "ABOVE"
        below_poc = loc.get("poc_side") == "BELOW"
        if direction == "BULLISH":
            if where == "ABOVE_VALUE":
                return {"score": 1, "reason": "accepted above value"}
            if where == "IN_VALUE" and above_poc:
                return {"score": 1, "reason": "holding above developing POC"}
        elif direction == "BEARISH":
            if where == "BELOW_VALUE":
                return {"score": 1, "reason": "accepted below value"}
            if where == "IN_VALUE" and below_poc:
                return {"score": 1, "reason": "holding below developing POC"}
        return {"score": 0, "reason": f"{where} does not confirm {direction}"}
    except Exception:
        return {"score": 0, "reason": "error"}
