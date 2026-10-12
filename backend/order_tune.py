"""Per-trade strategy tuning for pending orders.

A pending order carries a snapshot of the strategy values it was created
with (bounce, trail config, timers, ...). The Tune API edits that snapshot
for ONE trade without touching the global Default/NIFTY strategies.

Persisted as JSON (tune_json) on the trades + pending_orders rows so tuning
survives restarts via rehydrate_from_db.
"""
import json
import logging

log = logging.getLogger(__name__)

# Tunable per-trade fields (subset of the global strategy).
TUNE_FIELDS = (
    "lots", "lot_multiplier",
    "bounce_points",
    "activation_points", "activation_sl_offset", "trail_gap",
    "entry_timer_mins", "exit_timer_mins",
    "signal_trail_initial_sl", "signal_trail_initial_sl_points",
    "sl_mode", "snap_levels", "snap_trail_after_l3", "snap_trail_gap",
)

_DEFAULT_SNAPS = [
    {"snapPts": 10, "offset": 2},
    {"snapPts": 10, "offset": 2},
    {"snapPts": 10, "offset": 2},
]


def _num(v, default, cast=float, lo=None, hi=None):
    try:
        n = cast(v)
    except (ValueError, TypeError):
        return default
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def build_tune(strategy: dict, lots: int, multiplier: int) -> dict:
    """Snapshot strategy -> per-order tune dict at placement time."""
    strategy = strategy or {}
    return {
        "lots": max(1, int(lots or 1)),
        "lot_multiplier": max(1, int(multiplier or 1)),
        "bounce_points": float(strategy.get("bouncePoints") or 5),
        "activation_points": float(strategy.get("activationPoints") or 5.0),
        "activation_sl_offset": float(strategy.get("activationSLOffset") or 0.0),
        "trail_gap": float(strategy.get("trailGap") or 2.0),
        "entry_timer_mins": int(strategy.get("entryTimerMins") or 10),
        "exit_timer_mins": int(strategy.get("exitTimerMins") or 10),
        "signal_trail_initial_sl": strategy.get("signalTrailInitialSL", "telegram"),
        "signal_trail_initial_sl_points": float(strategy.get("signalTrailInitialSLPoints") or 5.0),
        "sl_mode": strategy.get("slMode", "signal_trail"),
        "snap_levels": strategy.get("snapLevels") or [dict(d) for d in _DEFAULT_SNAPS],
        "snap_trail_after_l3": bool(strategy.get("snapTrailAfterL3", False)),
        "snap_trail_gap": float(strategy.get("snapTrailGap") or 3.0),
    }


def tune_from_order(order: dict) -> dict:
    """Read the effective tune of a live pending-order dict."""
    o = order or {}
    mult = _num(o.get("lot_multiplier"), 1, int, 1)
    qty = _num(o.get("quantity"), mult, int, 1)
    return {
        "lots": _num(o.get("lots"), max(1, round(qty / mult)) if mult else 1, int, 1),
        "lot_multiplier": mult,
        "bounce_points": _num(o.get("bounce_points"), 5.0),
        "activation_points": _num(o.get("activation_points"), 5.0),
        "activation_sl_offset": _num(o.get("activation_sl_offset"), 0.0),
        "trail_gap": _num(o.get("trail_gap"), 2.0),
        "entry_timer_mins": _num(o.get("entry_timer_mins"), 10, int),
        "exit_timer_mins": _num(o.get("exit_timer_mins"), 10, int),
        "signal_trail_initial_sl": o.get("signal_trail_initial_sl", "telegram"),
        "signal_trail_initial_sl_points": _num(o.get("signal_trail_initial_sl_points"), 5.0),
        "sl_mode": o.get("sl_mode", "signal_trail"),
        "snap_levels": o.get("snap_levels") or [dict(d) for d in _DEFAULT_SNAPS],
        "snap_trail_after_l3": bool(o.get("snap_trail_after_l3", False)),
        "snap_trail_gap": _num(o.get("snap_trail_gap"), 3.0),
    }


def parse_tune_json(raw) -> dict | None:
    """Parse a stored tune_json blob; None when absent/invalid."""
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: data[k] for k in TUNE_FIELDS if k in data}


def tune_to_json(tune: dict) -> str:
    return json.dumps({k: tune.get(k) for k in TUNE_FIELDS})


def _clean_snaps(levels) -> list[dict] | None:
    if not isinstance(levels, list) or len(levels) != 3:
        return None
    out = []
    for lv in levels:
        if not isinstance(lv, dict):
            return None
        out.append({
            "snapPts": _num(lv.get("snapPts"), 10.0, lo=0, hi=999),
            "offset": _num(lv.get("offset"), 2.0, lo=0, hi=999),
        })
    return out


def validate_patch(patch: dict) -> tuple[dict, list[str]]:
    """Validate a TuneRequest-style dict. Returns (clean_patch, errors)."""
    clean: dict = {}
    errors: list[str] = []
    p = patch or {}

    def _take(key, cast, lo, hi, label):
        if p.get(key) is None:
            return
        try:
            v = cast(p[key])
        except (ValueError, TypeError):
            errors.append(f"{label} must be a number")
            return
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            errors.append(f"{label} out of range")
            return
        clean[key] = v

    _take("lots", int, 1, 50, "Lots")
    _take("bouncePoints", float, 1, 999, "Bounce-back")
    _take("activationPoints", float, 0, 999, "Activation points")
    _take("activationSLOffset", float, 0, 999, "Activation offset")
    _take("trailGap", float, 0.5, 999, "Trail gap")
    _take("entryTimerMins", int, 1, 120, "Entry timer")
    _take("exitTimerMins", int, 1, 120, "Exit timer")
    _take("signalTrailInitialSLPoints", float, 1, 999, "Initial SL points")

    if p.get("signalTrailInitialSL") is not None:
        if p["signalTrailInitialSL"] not in ("telegram", "points_from_ltp"):
            errors.append("Initial SL source must be telegram or points_from_ltp")
        else:
            clean["signalTrailInitialSL"] = p["signalTrailInitialSL"]
    if p.get("slMode") is not None:
        if p["slMode"] not in ("signal_trail", "snap_levels"):
            errors.append("SL mode must be signal_trail or snap_levels")
        else:
            clean["slMode"] = p["slMode"]
    if p.get("snapLevels") is not None:
        lvls = _clean_snaps(p["snapLevels"])
        if lvls is None:
            errors.append("snapLevels must be 3 {snapPts, offset} levels")
        else:
            clean["snapLevels"] = lvls
    if p.get("snapTrailGap") is not None:
        _take("snapTrailGap", float, 0.5, 999, "Trail gap after L3")
    if isinstance(p.get("snapTrailAfterL3"), bool):
        clean["snapTrailAfterL3"] = p["snapTrailAfterL3"]

    if not clean and not errors:
        errors.append("Nothing to update")
    return clean, errors


# Maps TuneRequest keys -> order/tune keys.
_PATCH_MAP = {
    "lots": "lots",
    "bouncePoints": "bounce_points",
    "activationPoints": "activation_points",
    "activationSLOffset": "activation_sl_offset",
    "trailGap": "trail_gap",
    "entryTimerMins": "entry_timer_mins",
    "exitTimerMins": "exit_timer_mins",
    "signalTrailInitialSL": "signal_trail_initial_sl",
    "signalTrailInitialSLPoints": "signal_trail_initial_sl_points",
    "slMode": "sl_mode",
    "snapLevels": "snap_levels",
    "snapTrailGap": "snap_trail_gap",
    "snapTrailAfterL3": "snap_trail_after_l3",
}


def apply_patch(order: dict, clean: dict) -> dict:
    """Apply a validated patch to a live pending-order dict (in place).

    Returns the refreshed tune dict. Recomputes quantity when lots change.
    """
    tune = tune_from_order(order)
    for req_key, tune_key in _PATCH_MAP.items():
        if req_key in clean:
            tune[tune_key] = clean[req_key]
    order.update({k: tune[k] for k in TUNE_FIELDS if k in tune})
    order["lots"] = tune["lots"]
    order["lot_multiplier"] = tune["lot_multiplier"]
    order["quantity"] = tune["lots"] * tune["lot_multiplier"]
    return tune


def summary(tune: dict) -> str:
    """One-line human summary for toasts/broadcasts."""
    t = tune or {}
    mode = "Snap" if t.get("sl_mode") == "snap_levels" else "Trail"
    return (f"{t.get('lots', 1)} lot(s) | {mode} | Bounce {t.get('bounce_points', 5)} | "
            f"Act {t.get('activation_points', 5)}/{t.get('activation_sl_offset', 0)} "
            f"Gap {t.get('trail_gap', 2)}")
