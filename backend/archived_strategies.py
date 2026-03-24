"""
Archived Strategy Implementations — Removed from active codebase.
Can be re-integrated by importing and routing to these functions.

Archived on: 2026-03-23
Reason: Strategy simplification — keeping only bounce-back entry + signal_trail SL.

Contains:
  ENTRY STRATEGIES:
    - avg_signal (high/low/avg pick from signal range)
    - fixed (enter at a specific price)

  EXIT / TRAILING SL STRATEGIES:
    - code (stepped 2pt trail)
    - signal (trail LTP by fixed gap derived from entry_low - signal SL)
    - ltp (SL = previous tick's LTP)
    - fixed (trail SL N pts below LTP peak)

  COMPARE MODE:
    - _execute_paper_compare: runs all 5 entry strategies simultaneously
"""


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY STRATEGY: avg_signal
# Used in paper_trader.place_order() — determined entry price from signal range
# ══════════════════════════════════════════════════════════════════════════════

def avg_signal_entry_price(signal: dict, avg_pick: str) -> float:
    """
    Calculate entry price from signal range based on pick mode.

    Args:
        signal: parsed signal dict with 'entry_low' and 'entry_high'
        avg_pick: one of 'low', 'high', 'avg'

    Returns:
        Target entry price
    """
    hi = float(signal.get("entry_high") or 0)
    lo = float(signal.get("entry_low") or 0)

    if avg_pick == "low":
        return lo
    elif avg_pick == "high":
        return hi
    else:  # "avg"
        return (lo + hi) / 2.0 if (lo and hi) else (lo or hi)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY STRATEGY: fixed
# Used in paper_trader.place_order() — entered at a user-specified fixed price
# ══════════════════════════════════════════════════════════════════════════════

def fixed_entry_price(signal: dict, entry_fixed: float = None) -> float:
    """
    Return a fixed entry price, falling back to live LTP or entry_high.

    Args:
        signal: parsed signal dict
        entry_fixed: user-specified fixed price (from strategy)

    Returns:
        Target entry price
    """
    if entry_fixed:
        return float(entry_fixed)

    live_ltp = float(signal.get("live_ltp") or 0)
    entry_high = float(signal.get("entry_high") or 0)
    entry_low = float(signal.get("entry_low") or 0)

    return live_ltp if live_ltp > 0 else (entry_high or entry_low or 0)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY FILL: fixed / avg_signal price-side touch detection
# Used in paper_trader.on_tick() — filled when LTP crossed the target price
# ══════════════════════════════════════════════════════════════════════════════

PRICE_SIDE_CONFIRM_TICKS = 3  # Ticks needed to lock price_side for fixed/avg fills


def check_price_side_fill(order: dict, ltp: float) -> bool:
    """
    Determine if a fixed/avg_signal order should fill based on price-side crossing.

    The logic:
    1. Observe ticks to determine if LTP starts 'above' or 'below' the target price
    2. Lock price_side after PRICE_SIDE_CONFIRM_TICKS consecutive same-side ticks
    3. Fill when LTP crosses to the other side (or touches exactly)

    Args:
        order: pending order dict (mutated in-place to track price_side state)
        ltp: current last traded price

    Returns:
        True if the order should be filled at this tick
    """
    fill_price_target = order.get("price")
    if not fill_price_target:
        return False

    this_side = (
        "above" if ltp > fill_price_target else
        "below" if ltp < fill_price_target else
        "touch"
    )

    if order.get("price_side") is None:
        if this_side == order.get("price_side_candidate"):
            order["price_side_confirm_count"] += 1
        else:
            order["price_side_candidate"] = this_side
            order["price_side_confirm_count"] = 1

        if order["price_side_confirm_count"] >= PRICE_SIDE_CONFIRM_TICKS:
            order["price_side"] = order["price_side_candidate"]

    price_side = order.get("price_side")
    if price_side:
        touched = (
            price_side == "touch" or
            (price_side == "above" and ltp <= fill_price_target) or
            (price_side == "below" and ltp >= fill_price_target)
        )
        return touched

    return False


# ══════════════════════════════════════════════════════════════════════════════
# TRAILING SL: code (stepped 2pt trail)
# Used in paper_trader._process_position_tick()
# ══════════════════════════════════════════════════════════════════════════════

CODE_SL_PCT = 0.03       # 3% of entry price as initial SL offset
CODE_SL_MIN = 5.0        # minimum SL offset in points
CODE_TRAIL_STEP = 2.0    # trail step size in points


def code_trailing_sl(pos: dict, ltp: float) -> float | None:
    """
    Stepped trailing SL: initial SL = entry - max(3% of entry, 5pts).
    Trails upward in 2pt steps as LTP rises.

    Returns new SL value if it should be updated, else None.
    """
    if ltp <= pos.get("max_ltp", 0):
        return None

    pos["max_ltp"] = ltp
    steps = int((ltp - pos["entry_price"]) // CODE_TRAIL_STEP)
    if steps > 0:
        offset = max(CODE_SL_MIN, CODE_SL_PCT * pos["entry_price"])
        return (pos["entry_price"] - offset) + (steps * CODE_TRAIL_STEP)

    return None


def code_initial_sl(fill_price: float) -> float:
    """Initial SL for 'code' mode."""
    offset = max(CODE_SL_MIN, CODE_SL_PCT * fill_price)
    return fill_price - offset


# ══════════════════════════════════════════════════════════════════════════════
# TRAILING SL: signal (fixed gap from signal SL)
# Used in paper_trader._process_position_tick()
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_SL_GAP = 30.0  # Default signal-mode SL gap when not provided


def signal_trailing_sl(pos: dict, ltp: float) -> float | None:
    """
    Trail SL by a fixed gap derived from (entry_low - signal_stoploss).
    As LTP rises, SL = LTP - gap.

    Returns new SL value if it should be updated, else None.
    """
    sl_gap = pos.get("sl_gap") or DEFAULT_SL_GAP
    if ltp > pos.get("max_ltp", 0):
        pos["max_ltp"] = ltp
        return ltp - sl_gap
    return None


def signal_initial_sl(fill_price: float, sl_gap: float) -> float:
    """Initial SL for 'signal' mode."""
    return fill_price - sl_gap


# ══════════════════════════════════════════════════════════════════════════════
# TRAILING SL: ltp (previous tick's LTP)
# Used in paper_trader._process_position_tick()
# ══════════════════════════════════════════════════════════════════════════════

def ltp_trailing_sl(pos: dict, ltp: float) -> float | None:
    """
    SL = previous tick's LTP. Very tight trailing.

    Returns new SL value (the previous LTP), updates prev_ltp to current.
    """
    new_sl = pos.get("prev_ltp", pos["entry_price"])
    pos["prev_ltp"] = ltp
    if ltp > pos.get("max_ltp", 0):
        pos["max_ltp"] = ltp
    return new_sl


def ltp_initial_sl(fill_price: float) -> float:
    """Initial SL for 'ltp' mode — starts at fill price."""
    return fill_price


# ══════════════════════════════════════════════════════════════════════════════
# TRAILING SL: fixed (N pts below LTP peak)
# Used in paper_trader._process_position_tick()
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_SL_POINTS = 5.0  # Default fixed SL distance in points


def fixed_trailing_sl(pos: dict, ltp: float) -> float | None:
    """
    Trail SL a fixed number of points below LTP peak.

    Returns new SL value if it should be updated, else None.
    """
    sl_points = pos.get("sl_points", DEFAULT_SL_POINTS)
    if ltp > pos.get("max_ltp", 0):
        pos["max_ltp"] = ltp
        return ltp - sl_points
    return None


def fixed_initial_sl(fill_price: float, sl_points: float = DEFAULT_SL_POINTS) -> float:
    """Initial SL for 'fixed' mode."""
    return fill_price - sl_points


# ══════════════════════════════════════════════════════════════════════════════
# COMPARE MODE: execute all 5 entry strategies simultaneously
# Used in trade_manager._execute_paper_compare()
# ══════════════════════════════════════════════════════════════════════════════

def get_compare_variants(signal: dict):
    """
    Return the 5 strategy variants used in compare mode.
    Each variant overrides entryLogic/entryAvgPick/entryFixed.

    Args:
        signal: parsed signal dict (needs 'entry_high' and 'live_ltp')

    Returns:
        List of 5 variant dicts
    """
    hi = float(signal.get("entry_high") or 0)
    live_ltp = float(signal.get("live_ltp") or 0)

    return [
        {"entryLogic": "code",       "entryAvgPick": "avg",  "entryFixed": None,           "label": "code"},
        {"entryLogic": "avg_signal", "entryAvgPick": "high", "entryFixed": None,           "label": "high"},
        {"entryLogic": "avg_signal", "entryAvgPick": "low",  "entryFixed": None,           "label": "low"},
        {"entryLogic": "avg_signal", "entryAvgPick": "avg",  "entryFixed": None,           "label": "avg"},
        {"entryLogic": "fixed",      "entryAvgPick": "avg",  "entryFixed": live_ltp or hi, "label": "fixed"},
    ]
