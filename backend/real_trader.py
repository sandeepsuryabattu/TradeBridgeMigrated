"""
Real Trader — Places real orders on Kotak Neo using live market ticks.
Mirrors PaperTrader's bounce-back entry + signal_trail SL logic,
but uses actual broker API calls for order placement, SL management, and exits.

ORDER MECHANICS:
  Entry:    Limit BUY IOC at bounce_price + slippage
  SL:       Exchange-level SL order (trigger=SL, price=0.05, validity=DAY)
  Trailing: modify_order() when SL level changes (activation + trail_gap)
  Exit:     Cancel SL + Limit SELL IOC at LTP - slippage

SAFETY:
  Level 1: Immediate response check on every API call (retry up to 2x)
  Level 2: Order Feed WebSocket for real-time confirmations
  Level 3: Periodic reconciliation (verify SL orders exist on exchange)
"""
import asyncio
import re
import logging
from datetime import datetime, timedelta, timezone, time as dt_time
from typing import Optional, Callable
from zoneinfo import ZoneInfo

from . import database as db

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
ENTRY_TIMEOUT_MINS       = 10
POSITION_TIMEOUT_MINS    = 10
DEFAULT_BOUNCE_POINTS    = 5
DEFAULT_ACTIVATION_PTS   = 5.0
DEFAULT_TRAIL_GAP        = 2.0
DEFAULT_LOT_MULTIPLIER   = 20
SIGNAL_TRAIL_FALLBACK    = 10.0
DEFAULT_BUFFER_POINTS    = 2.0
DEFAULT_ENTRY_SLIPPAGE   = 1.0
DEFAULT_EXIT_SLIPPAGE    = 1.0
SL_LIMIT_PRICE           = 0.05   # Guarantees fill at best bid on SL trigger
MAX_ORDER_RETRIES        = 2
ORDER_RETRY_DELAY_S      = 0.5
RECONCILE_INTERVAL_S     = 30
EOD_EXIT_MINUTES_BEFORE  = 5      # Close positions N minutes before market close

_IST = ZoneInfo("Asia/Kolkata")
_MARKET_CLOSE = dt_time(15, 30)


class RealTrader:
    """Real trading engine using Kotak Neo API with exchange-level SL orders."""

    def __init__(self, kotak_trader=None, market_feed=None):
        self.kotak = kotak_trader
        self.market_feed = market_feed
        self._pending_orders: list[dict] = []
        self._open_positions: list[dict] = []
        self._ws_broadcast: Optional[Callable] = None
        self._on_trade_expired: Optional[Callable] = None

        # Symbol cache — resolved via search_scrip, valid for the day
        self._symbol_cache: dict[str, str] = {}

        # Locks
        self._expiry_lock = asyncio.Lock()
        self._order_lock  = asyncio.Lock()

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_ws_broadcast(self, broadcast_fn):
        self._ws_broadcast = broadcast_fn

    async def _broadcast(self, event_type: str, data: dict):
        if self._ws_broadcast:
            try:
                await self._ws_broadcast({"type": event_type, "data": data})
            except Exception:
                log.exception("RealTrader broadcast error")

    # ── Symbol Resolution ─────────────────────────────────────────────────────

    def _resolve_symbol(self, signal: dict) -> str:
        """Resolve the exact pTrdSymbol via search_scrip, with day cache."""
        strike = str(signal.get("strike", ""))
        opt_type = str(signal.get("option_type", "")).upper()
        cache_key = f"{strike}{opt_type}"

        if cache_key in self._symbol_cache:
            return self._symbol_cache[cache_key]

        fallback = f"SENSEX{int(strike)}{opt_type}"

        if not self.kotak or not self.kotak.is_authenticated:
            log.warning("Kotak not authenticated — using fallback symbol: %s", fallback)
            return fallback

        try:
            scrip = self.kotak.search_scrip(
                symbol="SENSEX",
                option_type=opt_type,
                strike_price=strike,
            )
            if scrip and isinstance(scrip, dict):
                instruments = scrip.get("data", [])
                if instruments:
                    resolved = instruments[0].get("pTrdSymbol", fallback)
                    self._symbol_cache[cache_key] = resolved
                    log.info("Resolved symbol: %s → %s", cache_key, resolved)
                    return resolved
        except Exception:
            log.exception("search_scrip failed for %s", cache_key)

        log.warning("Using fallback symbol: %s", fallback)
        return fallback

    # ── Place Order (Pending — no Kotak call yet) ─────────────────────────────

    async def place_order(
        self,
        signal: dict,
        signal_id: int,
        lot_size: int = None,
        strategy: dict = None,
    ) -> dict:
        """Create a pending order. Kotak BUY is placed only when bounce confirms."""
        from .config import Config

        contract_lot = signal.get("contract_lot_size")
        lot_multiplier = contract_lot if contract_lot else DEFAULT_LOT_MULTIPLIER
        qty = (lot_size or int(Config.DEFAULT_LOT_SIZE)) * lot_multiplier
        strategy = strategy or {}

        signal_stoploss = signal.get("stoploss")
        entry_low  = float(signal.get("entry_low", 0))
        entry_high = float(signal.get("entry_high", 0))

        # Buffer points
        buffer_enabled = bool(strategy.get("bufferEnabled", False))
        buffer_points  = float(strategy.get("bufferPoints") or DEFAULT_BUFFER_POINTS)
        if buffer_enabled and buffer_points > 0:
            entry_low  -= buffer_points
            entry_high += buffer_points
            if signal_stoploss is not None:
                signal_stoploss = float(signal_stoploss) - buffer_points

        activation_points = float(strategy.get("activationPoints") or DEFAULT_ACTIVATION_PTS)
        trail_gap         = float(strategy.get("trailGap")         or DEFAULT_TRAIL_GAP)
        entry_slippage    = float(strategy.get("entrySlippage")    or DEFAULT_ENTRY_SLIPPAGE)
        exit_slippage     = float(strategy.get("exitSlippage")     or DEFAULT_EXIT_SLIPPAGE)

        signal_trail_initial_sl        = strategy.get("signalTrailInitialSL", "telegram")
        signal_trail_initial_sl_points = float(strategy.get("signalTrailInitialSLPoints") or 5.0)

        order_price   = float(entry_high or entry_low or 0)
        bounce_points = float(strategy.get("bouncePoints") or DEFAULT_BOUNCE_POINTS)
        trading_symbol = self._resolve_symbol(signal)

        entry_timer_mins = int(strategy.get("entryTimerMins") or ENTRY_TIMEOUT_MINS)
        exit_timer_mins  = int(strategy.get("exitTimerMins")  or POSITION_TIMEOUT_MINS)

        order = {
            "signal_id":         signal_id,
            "mode":              "real",
            "exchange_segment":  "bse_fo",
            "trading_symbol":    trading_symbol,
            "transaction_type":  "B",
            "order_type":        "L",
            "quantity":          qty,
            "price":             order_price,
            "trigger_price":     0,
            "status":            "pending",
            "order_id":          f"REAL-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{signal_id}",
            "notes":             f"Real BUY {signal['strike']} {signal.get('option_type')} @ {entry_low}-{entry_high} [bounce-back]",
            "entry_low":         entry_low,
            "entry_high":        entry_high,
            "strike":            signal.get("strike"),
            "option_type":       signal.get("option_type"),
            "created_at":        datetime.now(timezone.utc),
            "sl_mode":           "signal_trail",
            "signal_stoploss":   float(signal_stoploss) if signal_stoploss else None,
            "activation_points": activation_points,
            "trail_gap":         trail_gap,
            "bounce_points":     bounce_points,
            "entry_logic":       "code",
            "entry_label":       signal.get("entry_label"),
            "entry_timer_mins":  entry_timer_mins,
            "exit_timer_mins":   exit_timer_mins,
            "entry_slippage":    entry_slippage,
            "exit_slippage":     exit_slippage,
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
            "Real order pending: %s — Entry: bounce-back — SL: signal_trail — EntryTimer: %dmin",
            order["trading_symbol"], entry_timer_mins,
        )

        return {"status": "pending", "trade_id": trade_id, "order": order}

    # ── Kotak API Call with Retry ─────────────────────────────────────────────

    async def _kotak_call_with_retry(self, fn, description: str, **kwargs) -> dict:
        """Call a Kotak API function with retry + error broadcasting."""
        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                result = fn(**kwargs)
                if isinstance(result, dict) and result.get("status") == "error":
                    raise Exception(result.get("message", "Unknown error"))
                if isinstance(result, dict) and (result.get("Error") or result.get("Error Message")):
                    raise Exception(str(result.get("Error") or result.get("Error Message")))
                return result
            except Exception as e:
                log.error("%s failed (attempt %d/%d): %s", description, attempt, MAX_ORDER_RETRIES, e)
                if attempt < MAX_ORDER_RETRIES:
                    await asyncio.sleep(ORDER_RETRY_DELAY_S)
                else:
                    await self._broadcast("order_alert", {
                        "level":   "error",
                        "message": f"{description} FAILED after {MAX_ORDER_RETRIES} attempts: {e}",
                    })
                    raise

    # ── Place Entry on Kotak (Bounce Confirmed) ──────────────────────────────

    async def _place_entry_order(self, order: dict, bounce_price: float) -> Optional[dict]:
        """Place Limit BUY IOC on Kotak. Returns fill info or None."""
        slippage = order.get("entry_slippage", DEFAULT_ENTRY_SLIPPAGE)
        limit_price = round(bounce_price + slippage, 2)

        log.info(
            "Placing real BUY: %s @ %.2f (bounce=%.2f + slip=%.2f) IOC",
            order["trading_symbol"], limit_price, bounce_price, slippage,
        )

        try:
            result = await self._kotak_call_with_retry(
                self.kotak.place_order,
                f"BUY {order['trading_symbol']}",
                exchange_segment="bse_fo",
                trading_symbol=order["trading_symbol"],
                transaction_type="B",
                order_type="L",
                quantity=order["quantity"],
                price=limit_price,
                validity="IOC",
            )

            kotak_order_id = ""
            if isinstance(result, dict):
                data = result.get("data", result)
                if isinstance(data, dict):
                    kotak_order_id = data.get("nOrdNo", "")

            await db.update_trade(order["trade_id"], {"kotak_order_id": kotak_order_id})

            await self._broadcast("order_update", {
                "id":          order["trade_id"],
                "signal_id":   order.get("signal_id"),
                "status_note": f"✅ BUY Placed — Order #{kotak_order_id}",
                "kotak_order_id": kotak_order_id,
            })

            log.info("BUY order placed: %s, kotak_order_id=%s", order["trading_symbol"], kotak_order_id)
            return {
                "kotak_order_id": kotak_order_id,
                "limit_price":    limit_price,
                "bounce_price":   bounce_price,
            }

        except Exception:
            log.exception("Failed to place entry order for %s", order["trading_symbol"])
            await self._broadcast("order_update", {
                "id":          order["trade_id"],
                "signal_id":   order.get("signal_id"),
                "status_note": f"❌ BUY FAILED for {order['trading_symbol']}",
            })
            return None

    # ── Place SL Order on Exchange ────────────────────────────────────────────

    async def _place_sl_order(self, pos: dict, trigger_price: float) -> Optional[str]:
        """Place exchange SL order. Returns kotak sl_order_id or None."""
        log.info(
            "Placing SL order: %s trigger=%.2f price=%.2f",
            pos["trading_symbol"], trigger_price, SL_LIMIT_PRICE,
        )

        try:
            result = await self._kotak_call_with_retry(
                self.kotak.place_order,
                f"SL {pos['trading_symbol']}",
                exchange_segment="bse_fo",
                trading_symbol=pos["trading_symbol"],
                transaction_type="S",
                order_type="SL",
                quantity=pos["quantity"],
                price=SL_LIMIT_PRICE,
                trigger_price=trigger_price,
                validity="DAY",
            )

            sl_order_id = ""
            if isinstance(result, dict):
                data = result.get("data", result)
                if isinstance(data, dict):
                    sl_order_id = data.get("nOrdNo", "")

            await db.update_position(pos["id"], {"sl_order_id": sl_order_id})
            pos["sl_order_id"] = sl_order_id

            await self._broadcast("order_update", {
                "id":          pos.get("trade_id"),
                "status_note": f"✅ SL Placed — Order #{sl_order_id} (trigger: {trigger_price:.2f})",
            })

            log.info("SL order placed: sl_order_id=%s, trigger=%.2f", sl_order_id, trigger_price)
            return sl_order_id

        except Exception:
            log.exception("Failed to place SL order for %s", pos["trading_symbol"])
            await self._broadcast("order_alert", {
                "level":   "error",
                "message": f"⚠️ SL order FAILED for {pos['trading_symbol']} — position UNPROTECTED",
            })
            return None

    # ── Update SL Order (Trail) ───────────────────────────────────────────────

    async def _update_sl_order(self, pos: dict, new_trigger: float):
        """Modify exchange SL order to trail the trigger price."""
        sl_order_id = pos.get("sl_order_id")
        if not sl_order_id:
            log.warning("No sl_order_id for position %s — cannot trail", pos["id"])
            # Attempt to re-place SL
            sl_order_id = await self._place_sl_order(pos, new_trigger)
            return

        log.info("Trailing SL: %s trigger → %.2f", pos["trading_symbol"], new_trigger)

        try:
            await self._kotak_call_with_retry(
                self.kotak.modify_order,
                f"Trail SL {pos['trading_symbol']}",
                order_id=sl_order_id,
                trigger_price=new_trigger,
                price=SL_LIMIT_PRICE,
                order_type="SL",
                quantity=pos["quantity"],
                validity="DAY",
                trading_symbol=pos["trading_symbol"],
                transaction_type="S",
            )

            await self._broadcast("order_update", {
                "id":          pos.get("trade_id"),
                "status_note": f"🔄 SL Trailed → trigger: {new_trigger:.2f} ✅",
            })

        except Exception:
            log.exception("Failed to trail SL for %s", pos["trading_symbol"])
            await self._broadcast("order_alert", {
                "level":   "warning",
                "message": f"⚠️ SL trail FAILED for {pos['trading_symbol']} — SL may be stale",
            })

    # ── Fill Order ────────────────────────────────────────────────────────────

    async def _fill_order(self, order: dict, fill_price: float, filled_qty: int = None) -> dict:
        """Open a position after entry fill, place exchange SL order."""
        trade_id = order.get("trade_id")
        qty = filled_qty or order["quantity"]

        # Calculate initial SL
        initial_sl_source = order.get("signal_trail_initial_sl", "telegram")
        if initial_sl_source == "points_from_ltp":
            pts = float(order.get("signal_trail_initial_sl_points") or 5.0)
            initial_sl = fill_price - pts
        else:
            signal_stoploss = order.get("signal_stoploss")
            initial_sl = (
                float(signal_stoploss)
                if signal_stoploss and float(signal_stoploss) < fill_price
                else fill_price - SIGNAL_TRAIL_FALLBACK
            )

        try:
            await db.update_trade(trade_id, {
                "status":     "filled",
                "fill_price": fill_price,
                "fill_time":  datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "quantity":   qty,
            })
        except Exception:
            log.exception("_fill_order: db.update_trade failed")

        now_utc = datetime.now(timezone.utc)

        pos_data = {
            "mode":              "real",
            "trading_symbol":    order["trading_symbol"],
            "strike":            order.get("strike"),
            "option_type":       order.get("option_type"),
            "quantity":          qty,
            "entry_price":       fill_price,
            "max_ltp":           fill_price,
            "trailing_sl":       initial_sl,
            "sl_mode":           "signal_trail",
            "signal_stoploss":   order.get("signal_stoploss"),
            "activation_points": order.get("activation_points", DEFAULT_ACTIVATION_PTS),
            "trail_gap":         order.get("trail_gap", DEFAULT_TRAIL_GAP),
            "sl_activated":      False,
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
            "exit_slippage": order.get("exit_slippage", DEFAULT_EXIT_SLIPPAGE),
        }
        self._open_positions.append(position)

        # Place exchange SL order
        sl_order_id = await self._place_sl_order(position, initial_sl)
        position["sl_order_id"] = sl_order_id

        log.info(
            "Position opened | Real | Entry=%.2f | Initial SL=%.2f | SL Order=%s",
            fill_price, initial_sl, sl_order_id,
        )

        result = {
            "status":      "filled",
            "trade_id":    trade_id,
            "fill_price":  fill_price,
            "position_id": position_id,
            "signal_id":   order.get("signal_id"),
            **{**position, "opened_at": now_utc.isoformat().replace("+00:00", "Z")},
        }

        await self._broadcast("new_trade", result)
        return result

    # ── Symbol Matching ───────────────────────────────────────────────────────

    def _symbol_matches(self, order_symbol: str, tick_symbol: str, tick_data: dict) -> bool:
        if not order_symbol or not tick_symbol:
            return False
        order_upper = order_symbol.upper().strip()
        tick_upper  = tick_symbol.upper().strip()
        if order_upper == tick_upper:
            return True
        order_match = re.match(r'^([A-Z]+?)(\d{5}(?:CE|PE))$', order_upper)
        if order_match:
            if (tick_upper.startswith(order_match.group(1)) and
                    tick_upper.endswith(order_match.group(2))):
                return True
        token = str(tick_data.get("tk") or tick_data.get("instrument_token", ""))
        if token and self.market_feed:
            sub = self.market_feed._subscriptions.get(token, {})
            sub_symbol = sub.get("symbol", "").upper().strip()
            if sub_symbol and order_match:
                if (sub_symbol.startswith(order_match.group(1)) and
                        sub_symbol.endswith(order_match.group(2))):
                    return True
        return False

    # ── Tick Handler ──────────────────────────────────────────────────────────

    async def on_tick(self, token: str, ltp: float, data: dict):
        """Called on every market tick — bounce entry, position SL trailing."""
        now = datetime.now(timezone.utc)
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

        # 2. Check pending orders — bounce-back logic
        async with self._expiry_lock:
            filled  = []
            expired = []

            for order in list(self._pending_orders):
                created_at = _parse_dt(order.get("created_at"))
                entry_mins = float(order.get("entry_timer_mins", ENTRY_TIMEOUT_MINS))
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

                # Bounce-back entry logic
                in_range = order["entry_low"] <= ltp <= order["entry_high"]
                if in_range or order.get("min_ltp") is not None:
                    if order.get("min_ltp") is None or ltp < order["min_ltp"]:
                        order["min_ltp"] = ltp
                        try:
                            await db.update_trade(order["trade_id"], {"min_ltp": ltp})
                        except Exception:
                            log.exception("on_tick: update min_ltp failed")
                        await self._broadcast("order_update", {
                            "id":          order["trade_id"],
                            "signal_id":   order.get("signal_id"),
                            "min_ltp":     ltp,
                            "status_note": f"Tracking bounce from {ltp}",
                        })

                bounce_points = order.get("bounce_points", DEFAULT_BOUNCE_POINTS)
                if (order.get("min_ltp") is not None and
                        ltp >= order["min_ltp"] + bounce_points):
                    # Bounce confirmed — place real BUY on Kotak
                    async with self._order_lock:
                        entry_result = await self._place_entry_order(order, ltp)
                        if entry_result:
                            # IOC — assume filled at limit price for now
                            # Order feed will confirm actual fill
                            result = await self._fill_order(order, ltp)
                            filled.append(order)
                            log.info(
                                "Real order FILLED (Bounce-back): %s @ %s (Min was %s)",
                                order["trading_symbol"], ltp, order["min_ltp"],
                            )

            for order in filled + expired:
                if order in self._pending_orders:
                    self._pending_orders.remove(order)

    # ── Position Tick Processor ───────────────────────────────────────────────

    async def _process_position_tick(self, pos: dict, ltp: float):
        """Process a tick for an open position — signal_trail SL logic."""
        if ltp <= 0:
            return

        pos["current_price"] = ltp
        pos["pnl"]           = (ltp - pos["entry_price"]) * pos["quantity"]
        trail_update         = False
        new_sl               = None

        entry_price       = pos["entry_price"]
        activation_points = pos.get("activation_points", DEFAULT_ACTIVATION_PTS)
        trail_gap         = pos.get("trail_gap", DEFAULT_TRAIL_GAP)

        if not pos.get("sl_activated") and ltp >= entry_price + activation_points:
            pos["sl_activated"] = True
            pos["max_ltp"]      = ltp
            new_sl              = ltp
            trail_update        = True
            try:
                await db.update_position(pos["id"], {"sl_activated": 1, "max_ltp": ltp})
            except Exception:
                log.exception("_process_position_tick: persist sl_activated failed")
        elif pos.get("sl_activated"):
            if ltp > pos.get("max_ltp", 0):
                pos["max_ltp"] = ltp
                new_sl         = ltp - trail_gap
                trail_update   = True

        if new_sl is not None and new_sl > pos.get("trailing_sl", 0):
            pos["trailing_sl"] = new_sl
            log.info("[signal_trail] SL → %.2f for %s", new_sl, pos["trading_symbol"])

            # Update exchange SL order
            await self._update_sl_order(pos, new_sl)

            await self._broadcast("position_update", {
                "id":          pos["id"],
                "trailing_sl": new_sl,
                "max_ltp":     pos.get("max_ltp", ltp),
                "status_note": f"SL trailed to {new_sl:.2f} [signal_trail]",
            })

        # Note: We do NOT check ltp <= trailing_sl here because the exchange
        # SL order handles the actual trigger. If the exchange triggers the SL,
        # the order feed callback will close the position.
        # We only update PnL display here.

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

    # ── Close Position ────────────────────────────────────────────────────────

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

            # 1. Cancel exchange SL order
            sl_order_id = pos.get("sl_order_id")
            if sl_order_id:
                try:
                    await self._kotak_call_with_retry(
                        self.kotak.cancel_order,
                        f"Cancel SL {pos['trading_symbol']}",
                        order_id=sl_order_id,
                    )
                    log.info("Cancelled SL order %s", sl_order_id)
                except Exception:
                    log.exception("Failed to cancel SL order %s — may already be triggered", sl_order_id)

            # 2. Place Limit SELL IOC at ₹0.05 (fills at best bid, guarantees exit)
            if exit_reason in ("timer", "kill", "eod", "manual"):
                try:
                    await self._kotak_call_with_retry(
                        self.kotak.place_order,
                        f"SELL {pos['trading_symbol']}",
                        exchange_segment="bse_fo",
                        trading_symbol=pos["trading_symbol"],
                        transaction_type="S",
                        order_type="L",
                        quantity=pos["quantity"],
                        price=SL_LIMIT_PRICE,   # ₹0.05 — fills at best bid
                        validity="IOC",
                    )
                    await self._broadcast("order_update", {
                        "id":          pos.get("trade_id"),
                        "status_note": f"✅ SELL Placed @ best bid ({exit_reason})",
                    })
                except Exception:
                    log.exception("Failed to place exit SELL for %s", pos["trading_symbol"])
                    await self._broadcast("order_alert", {
                        "level":   "error",
                        "message": f"❌ EXIT SELL FAILED for {pos['trading_symbol']} — MANUAL EXIT REQUIRED",
                    })

            # 3. Update DB
            try:
                closed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                await db.update_position(position_id, {
                    "status":        "closed",
                    "current_price": price,
                    "pnl":           pnl,
                    "closed_at":     closed_at,
                    **({  "exit_reason": exit_reason} if exit_reason else {}),
                })
                await db.update_trade(pos["trade_id"], {
                    "pnl":        pnl,
                    "exit_price": price,
                    "closed_at":  closed_at,
                    **({  "exit_reason": exit_reason} if exit_reason else {}),
                })
            except Exception:
                log.exception("close_position: db update failed")

            if pos in self._open_positions:
                self._open_positions.remove(pos)

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
                "closed_at":   datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "exit_reason": exit_reason,
            })

            return {"status": "closed", "pnl": pnl, "exit_price": price, "exit_reason": exit_reason}

        return {"status": "error", "message": "Position not found"}

    # ── Timeout Checker ───────────────────────────────────────────────────────

    async def check_timeouts(self):
        """Expire pending orders and force-close timed-out positions."""
        now = datetime.now(timezone.utc)

        async with self._expiry_lock:
            expired = []
            for order in list(self._pending_orders):
                created_at = _parse_dt(order.get("created_at"))
                entry_mins = float(order.get("entry_timer_mins", ENTRY_TIMEOUT_MINS))
                if created_at and (now - created_at) > timedelta(minutes=entry_mins):
                    log.warning("TIMEOUT: Expiring pending order %s", order["trading_symbol"])
                    try:
                        await db.update_trade(order["trade_id"], {"status": "expired"})
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
            for order in expired:
                if order in self._pending_orders:
                    self._pending_orders.remove(order)

        for pos in list(self._open_positions):
            if pos.get("status") == "closed":
                continue
            opened_at = _parse_dt(pos.get("opened_at"))
            exit_mins = float(pos.get("exit_timer_mins", POSITION_TIMEOUT_MINS))
            if not opened_at:
                continue
            if (now - opened_at) > timedelta(minutes=exit_mins):
                exit_price = pos.get("current_price", pos["entry_price"])
                log.warning("TIMEOUT: Force-closing %s @ %s", pos["trading_symbol"], exit_price)
                await self.close_position(pos["id"], exit_price=exit_price, exit_reason="timer")

    # ── EOD Auto-Close ────────────────────────────────────────────────────────

    async def check_eod(self):
        """Auto-close all positions before market close (DAY SL orders expire at close)."""
        now_ist = datetime.now(_IST).time()
        eod_time = dt_time(
            _MARKET_CLOSE.hour,
            _MARKET_CLOSE.minute - EOD_EXIT_MINUTES_BEFORE,
        )
        if now_ist >= eod_time and now_ist < _MARKET_CLOSE:
            if self._open_positions:
                log.warning("EOD: Auto-closing %d positions before market close", len(self._open_positions))
                await self.square_off_all(exit_reason="eod")

    # ── Order Feed Handler ────────────────────────────────────────────────────

    async def handle_order_feed(self, message: dict):
        """Process order feed WebSocket events from Kotak."""
        if not isinstance(message, dict):
            return

        data = message.get("data", message)
        if isinstance(data, str):
            try:
                import json
                data = json.loads(data)
            except (ValueError, TypeError):
                return

        if not isinstance(data, dict):
            return

        order_id = data.get("nOrdNo", "")
        status   = str(data.get("ordSt", "")).lower()

        if not order_id:
            return

        # Check if this is an SL order that got triggered (traded)
        if status == "traded":
            for pos in list(self._open_positions):
                if pos.get("sl_order_id") == order_id and pos.get("status") != "closed":
                    fill_price = float(data.get("flPrc", pos.get("trailing_sl", 0)))
                    log.warning("SL TRIGGERED by exchange: %s @ %s", pos["trading_symbol"], fill_price)
                    await self._broadcast("order_update", {
                        "id":          pos.get("trade_id"),
                        "status_note": f"🔴 SL Triggered @ {fill_price} — Exited ✅",
                    })
                    pos["sl_order_id"] = None  # Already triggered
                    await self.close_position(pos["id"], exit_price=fill_price, exit_reason="sl")
                    return

        if status in ("rejected", "cancelled"):
            for pos in list(self._open_positions):
                if pos.get("sl_order_id") == order_id and pos.get("status") != "closed":
                    reason = data.get("rejRsn", "Unknown")
                    log.error("SL order %s %s for %s: %s", status, order_id, pos["trading_symbol"], reason)
                    await self._broadcast("order_alert", {
                        "level":   "error",
                        "message": f"⚠️ SL order {status.upper()} for {pos['trading_symbol']}: {reason}",
                    })
                    # Attempt to re-place SL
                    new_sl_id = await self._place_sl_order(pos, pos.get("trailing_sl", 0))
                    if new_sl_id:
                        pos["sl_order_id"] = new_sl_id
                    return

    # ── Reconciliation ────────────────────────────────────────────────────────

    async def reconcile_orders(self):
        """Verify all expected SL orders still exist on exchange."""
        if not self.kotak or not self.kotak.is_authenticated:
            return

        if not self._open_positions:
            return

        try:
            order_book = self.kotak.get_order_book()
            if not order_book or not isinstance(order_book, dict):
                return

            orders = order_book.get("data", [])
            if not isinstance(orders, list):
                return

            active_order_ids = {
                str(o.get("nOrdNo", ""))
                for o in orders
                if str(o.get("ordSt", "")).lower() in ("pending", "trigger pending", "open")
            }

            for pos in list(self._open_positions):
                if pos.get("status") == "closed":
                    continue
                sl_order_id = pos.get("sl_order_id")
                if sl_order_id and sl_order_id not in active_order_ids:
                    log.error(
                        "RECONCILE: SL order %s NOT FOUND for %s — re-placing",
                        sl_order_id, pos["trading_symbol"],
                    )
                    await self._broadcast("order_alert", {
                        "level":   "warning",
                        "message": f"⚠️ SL order missing for {pos['trading_symbol']} — re-placing",
                    })
                    new_sl_id = await self._place_sl_order(pos, pos.get("trailing_sl", 0))
                    if new_sl_id:
                        pos["sl_order_id"] = new_sl_id

        except Exception:
            log.exception("reconcile_orders failed")

    # ── Kill Switch ───────────────────────────────────────────────────────────

    async def square_off_all(self, exit_reason: str = "kill") -> dict:
        results = []
        for pos in list(self._open_positions):
            if pos.get("status") == "closed":
                continue
            price  = pos.get("current_price", pos.get("entry_price", 0))
            result = await self.close_position(pos["id"], exit_price=price, exit_reason=exit_reason)
            if result.get("status") == "closed":
                results.append({"position_id": pos["id"], "symbol": pos.get("trading_symbol"), **result})

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

        log.info("KILL SWITCH (real): Closed %d positions, cancelled %d pending orders", len(results), cancelled)
        return {"status": "ok", "positions_closed": len(results), "orders_cancelled": cancelled, "results": results}

    # ── Rehydrate from DB ─────────────────────────────────────────────────────

    async def rehydrate_from_db(self):
        """Restore pending orders and open positions on startup."""
        pending_db_orders = await db.get_pending_orders(mode="real", status="pending")
        pending_by_trade  = {p["trade_id"]: p for p in pending_db_orders}

        pending_trades = await db.get_trades(mode="real")
        restored_orders = 0

        for t in pending_trades:
            if t.get("status") != "pending":
                continue

            po = pending_by_trade.get(t["id"], {})

            order = {
                "trade_id":          t["id"],
                "signal_id":         t.get("signal_id"),
                "pending_order_id":  po.get("id"),
                "mode":              "real",
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
                "activation_points": DEFAULT_ACTIVATION_PTS,
                "trail_gap":         DEFAULT_TRAIL_GAP,
                "bounce_points":     DEFAULT_BOUNCE_POINTS,
                "entry_logic":       "code",
                "entry_label":       t.get("entry_label"),
                "entry_timer_mins":  ENTRY_TIMEOUT_MINS,
                "exit_timer_mins":   POSITION_TIMEOUT_MINS,
                "entry_slippage":    DEFAULT_ENTRY_SLIPPAGE,
                "exit_slippage":     DEFAULT_EXIT_SLIPPAGE,
                "signal_trail_initial_sl":        "telegram",
                "signal_trail_initial_sl_points": 5.0,
            }

            notes_str   = t.get("notes", "")
            range_match = re.search(r'@ ([\d.]+)-([\d.]+)', notes_str)
            if range_match:
                order["entry_low"]  = float(range_match.group(1))
                order["entry_high"] = float(range_match.group(2))

            sym_match = re.match(r'^[A-Z]+?(\d{5})(CE|PE)$', t.get("trading_symbol", "").upper())
            if sym_match:
                order["strike"]      = sym_match.group(1)
                order["option_type"] = sym_match.group(2)

            self._pending_orders.append(order)
            restored_orders += 1

        open_positions = await db.get_positions(mode="real", status="open")
        restored_positions = 0

        for p in open_positions:
            pos = {
                "id":            p["id"],
                "trade_id":      p.get("trade_id"),
                "mode":          "real",
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
                "activation_points": p.get("activation_points") or DEFAULT_ACTIVATION_PTS,
                "trail_gap":         p.get("trail_gap")          or DEFAULT_TRAIL_GAP,
                "sl_activated":      bool(p.get("sl_activated", 0)),
                "exit_reason":       p.get("exit_reason"),
                "exit_timer_mins":   POSITION_TIMEOUT_MINS,
                "exit_slippage":     DEFAULT_EXIT_SLIPPAGE,
                # Kotak order IDs
                "kotak_entry_order_id": p.get("kotak_entry_order_id"),
                "sl_order_id":          p.get("sl_order_id"),
            }
            self._open_positions.append(pos)
            restored_positions += 1

        if restored_orders or restored_positions:
            log.info(
                "Rehydrated (real) from DB: %d pending orders, %d open positions",
                restored_orders, restored_positions,
            )

    # ── Read-only Accessors ───────────────────────────────────────────────────

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
