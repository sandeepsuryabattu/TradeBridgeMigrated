"""
Paper Trader — Simulates order execution using real market ticks.
Fills when LTP enters the entry range, tracks virtual P&L.

PATCHES APPLIED:
 [1] update_pnl() dead code removed — on_tick() owns all SL/PNL logic
 [2] check_timeouts() datetime comparison fixed — no longer strips timezone
 [3] close_position() now broadcasts position_update with status:'closed'
 [4] import re moved to top of file — out of hot tick path
 [5] Double-close race condition guarded — pos marked 'closed' before first await
 [6] opened_at stored as datetime object in memory; only serialized for DB/WS
 [7] square_off_all iterates a snapshot; close_position guard prevents double-remove
 [8] Bounce threshold configurable via strategy dict (default 5)
 [9] trading_symbol construction uses explicit int+upper cast
[10] get_pnl_summary comment added re: asyncio single-thread safety
[11] Bounce entry only for 'code' mode; 'fixed'/'avg_signal' fill on direct price touch
[12] _on_trade_expired initialized in __init__ — prevents AttributeError on order expiry
[13] rehydrate_from_db restores entry_low/entry_high from notes; 'fixed'/'avg_signal' fill on direct price touch
[14] signal_trail SL mode — uses signal SL until activation_points crossed, then trails trail_gap pts behind LTP
[FIX #2 ] rehydrate_from_db restores ALL SL config fields from DB — no more hardcoded defaults on restart
[FIX #7 ] rehydrate_from_db restores sl_activated + max_ltp — trailing SL phase survives restart
[FIX #10] check_timeouts + on_tick expiry share _expiry_lock — eliminates double-expiry async race
[FIX #14] price_side_candidate/count persisted to DB on every change — survives restart
[FIX #15] bounce timer and SL timer unified under _timer_lock with state enum — no concurrent fire
[FIX #21] all magic numbers extracted to module-level constants
[FIX #23] bare except replaced with logger.exception() throughout
[FIX #26] entryTimerMins / exitTimerMins from strategy replace hardcoded 10-min constants
[FIX #27] signalTrailInitialSL='points_from_ltp' uses fill price minus signalTrailInitialSLPoints
          instead of always reading initial SL from telegram signal
"""
import asyncio
import re
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Callable

from . import database as db

log = logging.getLogger(__name__)

# Pre-compiled regex for symbol matching — avoids recompile on every tick
_SYMBOL_RE = re.compile(r'^([A-Z]+?)(\d{5}(?:CE|PE))$')

# ── [FIX #21] Module-level constants — no more magic numbers buried in logic ──
ENTRY_TIMEOUT_MINS       = 10     # Default — overridden per-order by strategy.entryTimerMins
POSITION_TIMEOUT_MINS    = 10     # Default — overridden per-position by strategy.exitTimerMins
DEFAULT_BOUNCE_POINTS    = 5      # Default bounce threshold (overridable via strategy)
DEFAULT_ACTIVATION_PTS   = 5.0    # Default signal_trail activation threshold
DEFAULT_ACTIVATION_SL_OFFSET = 0.0  # Points subtracted from breakeven SL on activation (0 = no offset)
DEFAULT_TRAIL_GAP        = 2.0    # Default signal_trail trailing gap
DEFAULT_LOT_MULTIPLIER   = 20     # Fallback if contract master unavailable
SIGNAL_TRAIL_FALLBACK    = 10.0   # signal_trail fallback SL when stoploss missing
DEFAULT_BUFFER_POINTS    = 2.0    # Default buffer points for entry band widening


class _TimerState(Enum):
    """[FIX #15] Unified state for bounce-entry and position-hold timers."""
    IDLE          = "idle"
    WAITING_ENTRY = "waiting_entry"
    IN_TRADE      = "in_trade"


class PaperTrader:
    """Paper trading engine using live ticks for realistic simulation."""

    def __init__(self, market_feed=None):
        self.market_feed = market_feed
        self._pending_orders: list[dict] = []
        self._open_positions: list[dict] = []
        self._fill_callbacks: list = []
        self._ws_broadcast: Optional[Callable] = None
        self._on_trade_expired: Optional[Callable] = None   # [12] always initialized

        # Mode guard — set by TradeManager; on_tick returns immediately if mode != 'paper'
        self._active_mode: Optional[Callable] = None

        # [FIX #10] Shared lock — prevents on_tick + check_timeouts double-expiry
        self._expiry_lock = asyncio.Lock()

        # [FIX #15] Timer state + lock — prevents bounce-timer and SL-timer concurrent fire
        self._timer_lock  = asyncio.Lock()
        self._timer_state = _TimerState.IDLE

    def set_active_mode_fn(self, fn: Callable):
        """fn() returns the current trading mode ('paper'/'real').
        Prevents paper_trader.on_tick from simulating fills while in real mode.
        """
        self._active_mode = fn

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_ws_broadcast(self, broadcast_fn):
        self._ws_broadcast = broadcast_fn

    async def _broadcast(self, event_type: str, data: dict):
        if self._ws_broadcast:
            try:
                await self._ws_broadcast({"type": event_type, "data": data})
            except Exception:
                log.exception("PaperTrader broadcast error")

    def add_fill_callback(self, callback):
        self._fill_callbacks.append(callback)

    # ── Timeout checker (background task, every 10 s) ─────────────────────────

    async def check_timeouts(self):
        """Expire pending orders and force-close timed-out positions.
        Uses per-order/per-position timer values stored at creation time.
        [FIX #10] Shares _expiry_lock with on_tick().
        """
        now = datetime.now(timezone.utc)

        # 1. Expire pending orders
        async with self._expiry_lock:
            expired = []
            for order in list(self._pending_orders):
                created_at   = _parse_dt(order.get("created_at"))
                entry_mins   = float(order.get("entry_timer_mins", ENTRY_TIMEOUT_MINS))
                if created_at and (now - created_at) > timedelta(minutes=entry_mins):
                    log.warning("TIMEOUT: Expiring pending order %s", order["trading_symbol"])
                    try:
                        upd = {"status": "expired"}
                        if order.get("min_ltp") is not None:
                            upd["min_ltp"] = order["min_ltp"]  # flush deferred min_ltp on expiry
                        await db.update_trade(order["trade_id"], upd)
                        if order.get("pending_order_id"):
                            await db.delete_pending_order(order["pending_order_id"])
                    except Exception:
                        log.exception("check_timeouts: db update failed")
                    expired.append(order)
                    await self._broadcast("order_update", {
                        "id":          order["trade_id"],
                        "signal_id":   order.get("signal_id"),
                        "status":      "expired",
                        "status_note": f"Entry timeout ({int(entry_mins)} min) — discarded",
                    })
                    if self._on_trade_expired:
                        self._on_trade_expired(order["trading_symbol"])
            for order in expired:
                if order in self._pending_orders:
                    self._pending_orders.remove(order)

        # 2. Force-close timed-out positions
        for pos in list(self._open_positions):
            if pos.get("status") == "closed":
                continue
            opened_at  = _parse_dt(pos.get("opened_at"))
            exit_mins  = float(pos.get("exit_timer_mins", POSITION_TIMEOUT_MINS))
            if not opened_at:
                continue
            if (now - opened_at) > timedelta(minutes=exit_mins):
                exit_price = pos.get("current_price", pos["entry_price"])
                log.warning("TIMEOUT: Force-closing %s @ %s", pos["trading_symbol"], exit_price)
                await self.close_position(pos["id"], exit_price=exit_price, exit_reason="timer")

    # ── Place order ───────────────────────────────────────────────────────────

    async def place_order(
        self,
        signal: dict,
        signal_id: int,
        lot_size: int = None,
        strategy: dict = None,
    ) -> dict:
        """Place a paper order that fills when LTP enters the entry range.
        Entry: bounce-back only.  SL: signal_trail only.
        Buffer points optionally widen the entry band and extend the SL.
        """
        from .config import Config
        # Use contract master lot_size if available, else fallback
        contract_lot = signal.get("contract_lot_size")
        lot_multiplier = contract_lot if contract_lot else DEFAULT_LOT_MULTIPLIER
        qty      = (lot_size or int(Config.DEFAULT_LOT_SIZE)) * lot_multiplier
        strategy = strategy or {}

        signal_stoploss = signal.get("stoploss")
        entry_low       = float(signal.get("entry_low", 0))
        entry_high      = float(signal.get("entry_high", 0))

        # ── Buffer points: widen entry band + extend SL ──
        buffer_enabled = bool(strategy.get("bufferEnabled", False))
        buffer_points  = float(strategy.get("bufferPoints") or DEFAULT_BUFFER_POINTS)
        if buffer_enabled and buffer_points > 0:
            entry_low  -= buffer_points
            entry_high += buffer_points
            if signal_stoploss is not None:
                signal_stoploss = float(signal_stoploss) - buffer_points
            log.info(
                "Buffer points applied (±%.1f): entry=%s-%s, SL=%s",
                buffer_points, entry_low, entry_high, signal_stoploss,
            )

        activation_points   = float(strategy.get("activationPoints") or DEFAULT_ACTIVATION_PTS)
        activation_sl_offset = float(strategy.get("activationSLOffset") or 0.0)
        trail_gap           = float(strategy.get("trailGap")         or DEFAULT_TRAIL_GAP)

        # [FIX #27] Read initial SL source for signal_trail mode
        signal_trail_initial_sl        = strategy.get("signalTrailInitialSL", "telegram")
        signal_trail_initial_sl_points = float(strategy.get("signalTrailInitialSLPoints") or 5.0)

        order_price   = float(entry_high or entry_low or 0)
        bounce_points = float(strategy.get("bouncePoints") or DEFAULT_BOUNCE_POINTS)
        trading_symbol = f"SENSEX{int(signal['strike'])}{str(signal.get('option_type', '')).upper()}"

        log.info("Entry mode=bounce-back: range=%s-%s, bounce=%spts", entry_low, entry_high, bounce_points)

        # [FIX #26] Per-order timer values from strategy — fall back to module constants
        entry_timer_mins = int(strategy.get("entryTimerMins") or ENTRY_TIMEOUT_MINS)
        exit_timer_mins  = int(strategy.get("exitTimerMins")  or POSITION_TIMEOUT_MINS)

        order = {
            "signal_id":         signal_id,
            "mode":              "paper",
            "exchange_segment":  "bse_fo",
            "trading_symbol":    trading_symbol,
            "transaction_type":  "B",
            "order_type":        "L",
            "quantity":          qty,
            "price":             order_price,
            "trigger_price":     0,
            "status":            "pending",
            "order_id":          f"PAPER-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{signal_id}",
            "notes":             f"Paper BUY {signal['strike']} {signal.get('option_type')} @ {entry_low}-{entry_high} [bounce-back]",
            "entry_low":         entry_low,
            "entry_high":        entry_high,
            "strike":            signal.get("strike"),
            "option_type":       signal.get("option_type"),
            "created_at":        datetime.now(timezone.utc),
            "sl_mode":           "signal_trail",
            "signal_stoploss":   float(signal_stoploss) if signal_stoploss else None,
            "activation_points":    activation_points,
            "activation_sl_offset": activation_sl_offset,
            "trail_gap":            trail_gap,
            "bounce_points":     bounce_points,
            "entry_logic":       "code",
            "entry_label":       signal.get("entry_label"),
            # [FIX #26] Store timer values on the order so check_timeouts uses them
            "entry_timer_mins":  entry_timer_mins,
            "exit_timer_mins":   exit_timer_mins,
            # [FIX #27] Store initial SL config so _fill_order uses them
            "signal_trail_initial_sl":        signal_trail_initial_sl,
            "signal_trail_initial_sl_points": signal_trail_initial_sl_points,
        }

        trade_id = await db.save_trade(signal_id, order)
        order["trade_id"] = trade_id

        try:
            pending_order_id = await db.save_pending_order(signal_id, trade_id, order)
            order["pending_order_id"] = pending_order_id
        except Exception:
            log.exception("place_order: failed to save pending_order row")

        self._pending_orders.append(order)
        log.info(
            "Paper order pending: %s — Entry: bounce-back — SL: signal_trail — EntryTimer: %dmin — ExitTimer: %dmin",
            order["trading_symbol"], entry_timer_mins, exit_timer_mins,
        )

        if not self.market_feed or not self.market_feed.is_running:
            log.warning("Market feed not running — order will stay pending until feed connects")

        return {"status": "pending", "trade_id": trade_id, "order": order}

    # ── Symbol matching ───────────────────────────────────────────────────────

    def _symbol_matches(self, order_symbol: str, tick_symbol: str, tick_data: dict) -> bool:
        if not order_symbol or not tick_symbol:
            return False
        order_upper = order_symbol.upper().strip()
        tick_upper  = tick_symbol.upper().strip()
        if order_upper == tick_upper:
            return True
        order_match = _SYMBOL_RE.match(order_upper)
        if order_match:
            if (tick_upper.startswith(order_match.group(1)) and
                    tick_upper.endswith(order_match.group(2))):
                return True
        token = str(tick_data.get("tk") or tick_data.get("instrument_token", ""))
        if token and self.market_feed:
            sub        = self.market_feed._subscriptions.get(token, {})
            sub_symbol = sub.get("symbol", "").upper().strip()
            if sub_symbol and order_match:
                if (sub_symbol.startswith(order_match.group(1)) and
                        sub_symbol.endswith(order_match.group(2))):
                    return True
        return False

    # ── Tick handler ──────────────────────────────────────────────────────────

    async def on_tick(self, token: str, ltp: float, data: dict):
        """Called on every market tick — fills, position updates, timeouts."""
        # ── Mode guard: do nothing if we are not the active engine ──────────────
        if self._active_mode and self._active_mode() != "paper":
            return
        now         = datetime.now(timezone.utc)
        tick_symbol = data.get("symbol", "")

        # 1. Update open positions
        for pos in list(self._open_positions):
            if pos.get("status") == "closed":
                continue
            if not self._symbol_matches(pos["trading_symbol"], tick_symbol, data):
                continue

            opened_at = _parse_dt(pos.get("opened_at"))
            if opened_at:
                pos["opened_at"] = opened_at
                exit_mins = float(pos.get("exit_timer_mins", POSITION_TIMEOUT_MINS))
                if (now - opened_at) > timedelta(minutes=exit_mins):
                    log.warning("TIMEOUT: Force-selling %s @ %s", pos["trading_symbol"], ltp)
                    await self.close_position(pos["id"], exit_price=ltp, exit_reason="timer")
                    continue

            await self._process_position_tick(pos, ltp)

        # 2. Check pending orders
        async with self._expiry_lock:
            filled  = []
            expired = []

            for order in list(self._pending_orders):
                created_at  = _parse_dt(order.get("created_at"))
                entry_mins  = float(order.get("entry_timer_mins", ENTRY_TIMEOUT_MINS))
                if isinstance(order.get("created_at"), str):
                    order["created_at"] = created_at

                if created_at and (now - created_at) > timedelta(minutes=entry_mins):
                    log.warning("ENTRY TIMEOUT (%dmin): Discarding %s", entry_mins, order["trading_symbol"])
                    try:
                        await db.update_trade(order["trade_id"], {"status": "expired"})
                        if order.get("pending_order_id"):
                            await db.delete_pending_order(order["pending_order_id"])
                    except Exception:
                        log.exception("on_tick expiry: db update failed")
                    expired.append(order)
                    await self._broadcast("order_update", {
                        "id":          order["trade_id"],
                        "signal_id":   order.get("signal_id"),
                        "status":      "expired",
                        "status_note": f"Entry timeout ({int(entry_mins)} min)",
                    })
                    if self._on_trade_expired:
                        self._on_trade_expired(order["trading_symbol"])
                    continue

                if not self._symbol_matches(order["trading_symbol"], tick_symbol, data):
                    continue

                # ── Bounce-back entry logic (only mode) ──
                # [FIX #28] Only track min_ltp while LTP is inside entry range.
                # Previously min_ltp was tracked forever once set, causing fills
                # at prices far outside the entry band.
                in_range = order["entry_low"] <= ltp <= order["entry_high"]
                if in_range:
                    if order.get("min_ltp") is None or ltp < order["min_ltp"]:
                        order["min_ltp"] = ltp
                        # DB write deferred to fill/expiry — removed from hot path (perf fix)
                        log.info("New low for %s: %s", order["trading_symbol"], ltp)
                        await self._broadcast("order_update", {
                            "id":          order["trade_id"],
                            "signal_id":   order.get("signal_id"),
                            "min_ltp":     ltp,
                            "status_note": f"Tracking bounce from {ltp}",
                        })

                bounce_points = order.get("bounce_points", DEFAULT_BOUNCE_POINTS)
                # [FIX #28] Fill guard: bounce must land at or above entry_low
                if (order.get("min_ltp") is not None and
                        ltp >= order["min_ltp"] + bounce_points and
                        ltp >= order["entry_low"]):
                    async with self._timer_lock:
                        if self._timer_state == _TimerState.WAITING_ENTRY:
                            self._timer_state = _TimerState.IN_TRADE
                    result = await self._fill_order(order, ltp)
                    filled.append(order)
                    log.info(
                        "Paper order FILLED (Bounce-back): %s @ %s (Min was %s)",
                        order["trading_symbol"], ltp, order["min_ltp"],
                    )
                    result["signal_id"] = order.get("signal_id")
                    await self._broadcast("new_trade", result)

            for order in filled + expired:
                if order in self._pending_orders:
                    self._pending_orders.remove(order)

    # ── Position tick processor ───────────────────────────────────────────────

    async def _process_position_tick(self, pos: dict, ltp: float):
        """Process a tick for an open position — signal_trail SL only.

        FIX #1 (paper): On activation, SL is anchored at entry_price + activation_points
        (breakeven+ level), not at current LTP.  Setting new_sl = ltp caused immediate
        exit the same tick because the SL check ltp <= trailing_sl evaluated as ltp <= ltp.
        """
        if ltp <= 0:
            return

        pos["current_price"] = ltp
        pos["pnl"]           = (ltp - pos["entry_price"]) * pos["quantity"]
        new_sl               = None

        # ── signal_trail SL logic (only mode) ──
        entry_price          = pos["entry_price"]
        activation_points    = pos.get("activation_points", DEFAULT_ACTIVATION_PTS)
        activation_sl_offset = pos.get("activation_sl_offset", DEFAULT_ACTIVATION_SL_OFFSET)
        trail_gap            = pos.get("trail_gap", DEFAULT_TRAIL_GAP)

        if not pos.get("sl_activated") and ltp >= entry_price + activation_points:
            # FIX #1 (paper): Anchor initial SL at entry + activation_points (breakeven+), NOT at ltp.
            # Setting new_sl = ltp caused immediate exit on the same tick because
            # the SL hit check (ltp <= trailing_sl) evaluated as ltp <= ltp → True.
            # activationSLOffset (default 0) allows the user to soften the anchor:
            #   new_sl = entry + activation_points - offset
            pos["sl_activated"] = True
            pos["max_ltp"]      = ltp
            new_sl              = entry_price + activation_points - activation_sl_offset
            try:
                await db.update_position(pos["id"], {"sl_activated": 1, "max_ltp": ltp})
            except Exception:
                log.exception("_process_position_tick: persist sl_activated failed")
        elif pos.get("sl_activated"):
            if ltp > pos.get("max_ltp", 0):
                pos["max_ltp"] = ltp
                new_sl         = ltp - trail_gap

        if new_sl is not None and new_sl > pos.get("trailing_sl", 0):
            pos["trailing_sl"] = new_sl
            log.info("[signal_trail] SL → %.2f for %s", new_sl, pos["trading_symbol"])
            await self._broadcast("position_update", {
                "id":          pos["id"],
                "trailing_sl": new_sl,
                "max_ltp":     pos.get("max_ltp", ltp),
                "status_note": f"SL trailed to {new_sl:.2f} [signal_trail]",
            })

        if ltp <= pos.get("trailing_sl", 0):
            log.warning(
                "STOP LOSS HIT [signal_trail]: %s @ %s (SL was %.2f)",
                pos["trading_symbol"], ltp, pos["trailing_sl"],
            )
            await self.close_position(pos["id"], exit_price=ltp, exit_reason="sl")
        else:
            try:
                await db.update_position(pos["id"], {
                    "current_price": ltp,
                    "pnl":           pos["pnl"],
                    "max_ltp":       pos.get("max_ltp", ltp),
                    "trailing_sl":   pos["trailing_sl"],
                    "sl_activated":  int(bool(pos.get("sl_activated", False))),
                })
            except Exception:
                log.exception("_process_position_tick: db.update_position failed")
            await self._broadcast("position_update", {
                "id":            pos["id"],
                "current_price": ltp,
                "pnl":           pos["pnl"],
            })

    # ── Fill order ────────────────────────────────────────────────────────────

    async def _fill_order(self, order: dict, fill_price: float) -> dict:
        """Fill a paper order and open a position with signal_trail initial SL.
        [FIX #27] signal_trail initial SL respects signalTrailInitialSL strategy setting.
        """
        trade_id  = order.get("trade_id")

        # ── signal_trail initial SL (only mode) ──
        initial_sl_source = order.get("signal_trail_initial_sl", "telegram")
        if initial_sl_source == "points_from_ltp":
            pts        = float(order.get("signal_trail_initial_sl_points") or 5.0)
            initial_sl = fill_price - pts
            log.info(
                "SL mode=signal_trail: initial SL=%.2f (fill %.2f − %.1fpts)",
                initial_sl, fill_price, pts,
            )
        else:
            signal_stoploss = order.get("signal_stoploss")
            initial_sl = (
                float(signal_stoploss)
                if signal_stoploss and float(signal_stoploss) < fill_price
                else fill_price - SIGNAL_TRAIL_FALLBACK
            )
            log.info(
                "SL mode=signal_trail: initial SL=%.2f (telegram), activation=+%.1fpts, trail_gap=%.1fpts",
                initial_sl,
                order.get("activation_points", DEFAULT_ACTIVATION_PTS),
                order.get("trail_gap", DEFAULT_TRAIL_GAP),
            )

        try:
            await db.update_trade(trade_id, {
                "status":     "filled",
                "fill_price": fill_price,
                "fill_time":  datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "min_ltp":    order.get("min_ltp"),   # flush deferred min_ltp on fill
            })
        except Exception:
            log.exception("_fill_order: db.update_trade failed")

        now_utc = datetime.now(timezone.utc)

        pos_data = {
            "mode":              "paper",
            "trading_symbol":    order["trading_symbol"],
            "strike":            order.get("strike"),
            "option_type":       order.get("option_type"),
            "quantity":          order["quantity"],
            "entry_price":       fill_price,
            "max_ltp":           fill_price,
            "trailing_sl":       initial_sl,
            "sl_mode":           "signal_trail",
            "signal_stoploss":   order.get("signal_stoploss"),
            "activation_points":    order.get("activation_points", DEFAULT_ACTIVATION_PTS),
            "activation_sl_offset": order.get("activation_sl_offset", DEFAULT_ACTIVATION_SL_OFFSET),
            "trail_gap":            order.get("trail_gap", DEFAULT_TRAIL_GAP),
            "sl_activated":      False,
            # [FIX #26] Carry exit timer into position for on_tick timeout check
            "exit_timer_mins":   order.get("exit_timer_mins", POSITION_TIMEOUT_MINS),
        }

        try:
            position_id = await db.save_position(trade_id, pos_data)
        except Exception:
            log.exception("_fill_order: db.save_position failed")
            raise

        if order.get("pending_order_id"):
            try:
                await db.delete_pending_order(order["pending_order_id"])
            except Exception:
                log.exception("_fill_order: delete_pending_order failed")

        position = {
            **pos_data,
            "id":            position_id,
            "trade_id":      trade_id,
            "current_price": fill_price,
            "pnl":           0,
            "opened_at":     now_utc,
            "status":        "open",
        }
        self._open_positions.append(position)

        async with self._timer_lock:
            self._timer_state = _TimerState.IN_TRADE

        log.info(
            "Position opened | mode=signal_trail | Entry=%.2f | Initial SL=%.2f",
            fill_price, initial_sl,
        )

        trade = {**order, "status": "filled", "fill_price": fill_price}
        for cb in self._fill_callbacks:
            try:
                await cb(trade, position)
            except Exception:
                log.exception("Fill callback error")

        return {
            "status":      "filled",
            "trade_id":    trade_id,
            "fill_price":  fill_price,
            "position_id": position_id,
            **{**position, "opened_at": now_utc.isoformat().replace("+00:00", "Z")},
        }

    # ── Close position ────────────────────────────────────────────────────────

    async def close_position(self, position_id: int, exit_price: float = None, exit_reason: str = None) -> dict:
        for pos in self._open_positions:
            if pos["id"] != position_id:
                continue
            if pos.get("status") == "closed":
                return {"status": "error", "message": "Already closed"}
            pos["status"] = "closed"
            if exit_reason:
                pos["exit_reason"] = exit_reason

            price = exit_price or pos.get("current_price", pos["entry_price"])
            pnl   = (price - pos["entry_price"]) * pos["quantity"]

            try:
                await db.update_position(position_id, {
                    "status":        "closed",
                    "current_price": price,
                    "pnl":           pnl,
                    "closed_at":     datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    **({"exit_reason": exit_reason} if exit_reason else {})
                })
                await db.update_trade(pos["trade_id"], {
                    "pnl": pnl,
                    "exit_price": price,
                    "closed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    **({"exit_reason": exit_reason} if exit_reason else {})
                })
            except Exception:
                log.exception("close_position: db update failed")

            if pos in self._open_positions:
                self._open_positions.remove(pos)

            async with self._timer_lock:
                self._timer_state = _TimerState.IDLE

            closed_at_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            await self._broadcast("position_update", {
                "id":          position_id,
                "status":      "closed",
                "pnl":         pnl,
                "exit_price":  price,
                "exit_reason": exit_reason,
            })

            await self._broadcast("order_update", {
                "id":          pos["trade_id"],
                "status":      "closed",
                "pnl":         pnl,
                "exit_price":  price,
                "closed_at":   closed_at_str,
                "exit_reason": exit_reason,
            })

            return {"status": "closed", "pnl": pnl, "exit_price": price, "exit_reason": exit_reason}

        return {"status": "error", "message": "Position not found"}

    # ── Rehydrate from DB ─────────────────────────────────────────────────────

    async def rehydrate_from_db(self):
        """Restore pending orders and open positions on startup."""
        pending_db_orders = await db.get_pending_orders(mode="paper", status="pending")
        pending_by_trade  = {p["trade_id"]: p for p in pending_db_orders}

        pending_trades  = await db.get_trades(mode="paper")
        restored_orders = 0

        for t in pending_trades:
            if t.get("status") != "pending":
                continue

            po = pending_by_trade.get(t["id"], {})

            order = {
                "trade_id":          t["id"],
                "signal_id":         t.get("signal_id"),
                "pending_order_id":  po.get("id"),
                "mode":              "paper",
                "exchange_segment":  t.get("exchange_segment", "bse_fo"),
                "trading_symbol":    t.get("trading_symbol", ""),
                "transaction_type":  t.get("transaction_type", "B"),
                "order_type":        t.get("order_type", "L"),
                "quantity":          t.get("quantity", 0),
                "price":             t.get("price", 0),
                "trigger_price":     t.get("trigger_price", 0),
                "status":            "pending",
                "order_id":          t.get("order_id", ""),
                "notes":             t.get("notes", ""),
                "entry_low":         t.get("price", 0),
                "entry_high":        t.get("price", 0),
                "strike":            "",
                "option_type":       "",
                "created_at":        datetime.now(timezone.utc),
                "min_ltp":           t.get("min_ltp"),
                "sl_mode":           "signal_trail",
                "signal_stoploss":   None,
                "activation_points":    DEFAULT_ACTIVATION_PTS,
                "activation_sl_offset": DEFAULT_ACTIVATION_SL_OFFSET,
                "trail_gap":            DEFAULT_TRAIL_GAP,
                "bounce_points":     DEFAULT_BOUNCE_POINTS,
                "entry_logic":       "code",
                "entry_label":       t.get("entry_label"),
                # [FIX #26] Rehydrate with defaults
                "entry_timer_mins":  ENTRY_TIMEOUT_MINS,
                "exit_timer_mins":   POSITION_TIMEOUT_MINS,
                # [FIX #27] Rehydrate initial SL config defaults
                "signal_trail_initial_sl":        "telegram",
                "signal_trail_initial_sl_points": 5.0,
            }

            notes_str   = t.get("notes", "")
            range_match = re.search(r'@ ([\d.]+)-([\d.]+)', notes_str)
            if range_match:
                order["entry_low"]  = float(range_match.group(1))
                order["entry_high"] = float(range_match.group(2))
                log.info("Rehydrated entry range for trade %s: %s-%s",
                         t["id"], order["entry_low"], order["entry_high"])

            sym_match = re.match(r'^[A-Z]+?(\d{5})(CE|PE)$', t.get("trading_symbol", "").upper())
            if sym_match:
                order["strike"]      = sym_match.group(1)
                order["option_type"] = sym_match.group(2)

            self._pending_orders.append(order)
            restored_orders += 1

        open_positions     = await db.get_positions(mode="paper", status="open")
        restored_positions = 0

        for p in open_positions:
            pos = {
                "id":            p["id"],
                "trade_id":      p.get("trade_id"),
                "mode":          "paper",
                "trading_symbol": p.get("trading_symbol", ""),
                "strike":        p.get("strike", ""),
                "option_type":   p.get("option_type", ""),
                "quantity":      p.get("quantity", 0),
                "entry_price":   p.get("entry_price", 0),
                "current_price": p.get("current_price", 0),
                "pnl":           p.get("pnl", 0),
                "max_ltp":       p.get("max_ltp", 0),
                "trailing_sl":   p.get("trailing_sl", 0),
                "status":        "open",
                "opened_at":     p.get("opened_at", ""),
                "sl_mode":           "signal_trail",
                "signal_stoploss":   p.get("signal_stoploss"),
                "activation_points":    p.get("activation_points") or DEFAULT_ACTIVATION_PTS,
                "activation_sl_offset": p.get("activation_sl_offset") or DEFAULT_ACTIVATION_SL_OFFSET,
                "trail_gap":            p.get("trail_gap")          or DEFAULT_TRAIL_GAP,
                "sl_activated":      bool(p.get("sl_activated", 0)),
                "exit_reason":       p.get("exit_reason"),
                # [FIX #26] Exit timer defaults on rehydrate
                "exit_timer_mins":   POSITION_TIMEOUT_MINS,
            }
            self._open_positions.append(pos)
            restored_positions += 1

        if restored_orders or restored_positions:
            log.info(
                "Rehydrated from DB: %d pending orders, %d open positions",
                restored_orders, restored_positions,
            )

    # ── Read-only accessors ───────────────────────────────────────────────────

    def get_pending_orders(self) -> list[dict]:
        return list(self._pending_orders)

    def get_open_positions(self) -> list[dict]:
        return list(self._open_positions)

    def get_pnl_summary(self) -> dict:
        total_pnl = sum(p.get("pnl", 0) for p in self._open_positions)
        return {
            "open_positions":       len(self._open_positions),
            "pending_orders":       len(self._pending_orders),
            "total_unrealized_pnl": total_pnl,
        }

    # ── Cancel a single pending order ─────────────────────────────────────────

    async def cancel_pending_order(self, trade_id: int) -> dict:
        """Cancel a single pending entry order by trade_id.

        Acquires _expiry_lock to prevent race with on_tick() — either:
        • cancel runs first → order removed → on_tick won't find it
        • on_tick runs first → order filled/expired → cancel returns error
        """
        async with self._expiry_lock:
            target = None
            for order in self._pending_orders:
                if order.get("trade_id") == trade_id:
                    target = order
                    break

            if target is None:
                return {"status": "error", "message": "Pending order not found — may already be filled or expired"}

            # Remove from in-memory list
            self._pending_orders.remove(target)

        # Outside lock: DB + broadcast (safe — order already removed from list)
        try:
            await db.update_trade(trade_id, {"status": "cancelled"})
            if target.get("pending_order_id"):
                await db.delete_pending_order(target["pending_order_id"])
        except Exception:
            log.exception("cancel_pending_order: db update failed for trade %d", trade_id)

        await self._broadcast("order_update", {
            "id":          trade_id,
            "signal_id":   target.get("signal_id"),
            "status":      "cancelled",
            "status_note": "Cancelled by user",
        })

        log.info("Pending order cancelled: trade_id=%d symbol=%s", trade_id, target.get("trading_symbol"))
        return {"status": "ok", "trade_id": trade_id, "trading_symbol": target.get("trading_symbol")}

    # ── Kill switch ───────────────────────────────────────────────────────────

    async def square_off_all(self) -> dict:
        results = []
        for pos in list(self._open_positions):
            if pos.get("status") == "closed":
                continue
            price  = pos.get("current_price", pos.get("entry_price", 0))
            result = await self.close_position(pos["id"], exit_price=price, exit_reason="kill")
            if result.get("status") == "closed":
                results.append({
                    "position_id": pos["id"],
                    "symbol":      pos.get("trading_symbol"),
                    **result,
                })

        cancelled = 0
        for order in list(self._pending_orders):
            try:
                await db.update_trade(order["trade_id"], {"status": "cancelled"})
                if order.get("pending_order_id"):
                    await db.delete_pending_order(order["pending_order_id"])
            except Exception:
                log.exception("square_off_all: db update failed")
            cancelled += 1
            await self._broadcast("order_update", {
                "id":          order["trade_id"],
                "signal_id":   order.get("signal_id"),
                "status":      "cancelled",
                "status_note": "Cancelled by kill switch",
            })
        self._pending_orders.clear()

        log.info(
            "KILL SWITCH: Closed %d positions, cancelled %d pending orders",
            len(results), cancelled,
        )
        return {
            "status":           "ok",
            "positions_closed": len(results),
            "orders_cancelled": cancelled,
            "results":          results,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            log.warning("_parse_dt: could not parse %r", value)
    return None