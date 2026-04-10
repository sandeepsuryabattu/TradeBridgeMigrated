"""
Real Trader — Places real orders on Kotak Neo using live market ticks.
Mirrors PaperTrader's bounce-back entry + signal_trail SL logic,
but uses actual broker API calls for order placement, SL management, and exits.

ORDER MECHANICS:
  Entry:    Limit BUY IOC at bounce_price + slippage
  SL:       Exchange-level SL order (trigger=SL, price=0.05, validity=DAY)
  Trailing: modify_order() when SL level changes (activation + trail_gap)
  Exit:     Cancel SL + Limit SELL IOC near LTP - slippage

SAFETY:
  Level 1: Immediate response check on every API call (retry up to 2x)
  Level 2: Order Feed WebSocket for real-time confirmations
  Level 3: Periodic reconciliation (verify SL orders exist on exchange)

FIXES applied (v2):
  #1  _process_position_tick: SL on activation = entry_price + activation_points
  #2  _update_sl_order: sl_order_id only updated after modify_order succeeds
  #3  _fill_order: orphaned trade gets status='fill_error' on save_position failure
  #4  reconcile_orders: inverted logic — only re-place if order has terminal status
  #5  rehydrate_from_db: verify sl_order_id still active before reconcile
  #6  _update_sl_order: exchange_segment passed to modify_order
  #7  square_off_all: returns failed list for partial close failures
  #8  check_eod: _eod_triggered flag prevents repeated square_off_all calls
  #9  _verify_fill / _verify_exit_fill: consolidated into _poll_order_fill()
  #10 _resolve_symbol: cache keyed by (date, strike, opt_type) — cleared daily
  #11 RECONCILE_INTERVAL_S wiring note added; constant confirmed used in main.py
"""
import asyncio
from contextlib import suppress
import re
import logging
from datetime import datetime, date, timedelta, timezone, time as dt_time
from typing import Optional, Callable
from zoneinfo import ZoneInfo

from . import database as db

log = logging.getLogger(__name__)

# Pre-compiled regex for symbol matching — avoids recompile on every tick
_SYMBOL_RE = re.compile(r'^([A-Z]+?)(\d{5}(?:CE|PE))$')

# ── Constants ────────────────────────────────────────────────────────────────
ENTRY_TIMEOUT_MINS       = 10
POSITION_TIMEOUT_MINS    = 10
DEFAULT_BOUNCE_POINTS    = 5
DEFAULT_ACTIVATION_PTS        = 5.0
DEFAULT_ACTIVATION_SL_OFFSET  = 0.0   # Points subtracted from breakeven SL on activation (0 = no offset)
DEFAULT_TRAIL_GAP             = 2.0
DEFAULT_LOT_MULTIPLIER   = 20
SIGNAL_TRAIL_FALLBACK    = 10.0
DEFAULT_BUFFER_POINTS    = 2.0
DEFAULT_ENTRY_SLIPPAGE   = 1.0
DEFAULT_EXIT_SLIPPAGE    = 1.0
SL_LIMIT_PRICE           = 0.05   # Used as minimum limit price safeguard
EXIT_FLOOR_DISCOUNT      = 0.90   # For guaranteed exits: sell at 10% below LTP (BSE OR starts at ~10%)
MAX_ORDER_RETRIES        = 2
ORDER_RETRY_DELAY_S      = 0.5
SL_PROTECT_RETRIES       = 5
SL_PROTECT_RETRY_DELAY_S = 0.2
RECONCILE_INTERVAL_S     = 30     # Used by the scheduler in main.py
MAX_SL_REPROTECT_ATTEMPTS = 3    # Cap re-placement attempts per position to prevent runaway loops
EOD_EXIT_MINUTES_BEFORE  = 5      # Close positions N minutes before market close
MAX_EXIT_SELL_ATTEMPTS   = 6      # Hard cap on IOC SELL retry loop before declaring close_failed

# Rejection reasons that mean "position already gone" (exchange confirmed no holding)
# Anything matching these lets us safely close in DB without placing another SELL.
_ALREADY_EXITED_REASONS  = frozenset({
    "no holdings", "insufficient qty", "position closed",
    "no open position", "net qty is zero", "quantity exceeds",
})
POS_FLUSH_INTERVAL_S     = 2.0    # Batch position DB writes — flush every N seconds

# Fill confirmation tuning (extended to handle real-world latency)
FILL_VERIFY_POLLS        = 12     # Number of order_history polls to confirm fill (~3s)
FILL_VERIFY_INTERVAL_S   = 0.25   # Seconds between polls
FILL_CONFIRM_TIMEOUT_S   = 4.0    # Hard timeout for feed+poll confirmation race

BSE_TICK_SIZE            = 0.05   # BSE BFO minimum price increment — all order prices MUST be multiples of this


def _round_to_tick(price: float, tick: float = BSE_TICK_SIZE) -> float:
    """Round price DOWN to nearest valid tick size.

    BSE BFO rejects orders with 'RATE NOT MULTIPLE OF TICK[0.05]' if the price
    is not an exact multiple of 0.05. Always round DOWN (floor) so the sell
    price is never above BSE's allowed grid.
    """
    import math
    return max(tick, math.floor(price / tick) * tick)

_IST = ZoneInfo("Asia/Kolkata")
_MARKET_CLOSE = dt_time(15, 30)

# Kotak order statuses considered "still active on exchange"
# FIX #4: Inverted reconcile logic — we only act when status is TERMINAL,
# not when it's absent from a "known active" whitelist. This prevents false
# re-placement when an order is in a transient state (e.g. "modify pending").
_TERMINAL_ORDER_STATUSES = frozenset({
    "traded", "complete", "completed", "rejected", "cancelled",
})


class RealTrader:
    """Real trading engine using Kotak Neo API with exchange-level SL orders."""

    def __init__(self, kotak_trader=None, market_feed=None):
        self.kotak = kotak_trader
        self.market_feed = market_feed
        self._pending_orders: list[dict] = []
        self._open_positions: list[dict] = []
        self._ws_broadcast: Optional[Callable] = None
        self._on_trade_expired: Optional[Callable] = None

        # FIX #10: Symbol cache keyed by (trading_date, strike, opt_type) so it
        # auto-invalidates across days without any explicit eviction call.
        self._symbol_cache: dict[tuple, str] = {}

        # FIX #8: EOD guard — prevents repeated square_off_all within the window.
        self._eod_triggered: bool = False

        # Locks
        self._expiry_lock = asyncio.Lock()
        self._order_lock  = asyncio.Lock()
        self._close_lock  = asyncio.Lock()

        # Entry fill confirmation waiters (order_id -> Future)
        self._entry_fill_waiters: dict[str, asyncio.Future] = {}

        # Batched position-write cache — avoids per-tick DB writes in the hot path.
        # Keyed by position_id; values overwritten each tick, flushed every POS_FLUSH_INTERVAL_S.
        self._pos_write_cache: dict[int, dict] = {}
        self._pos_flush_task: Optional[asyncio.Task] = None

        # Mode guard — set by TradeManager; on_tick returns immediately if mode != 'real'
        self._active_mode: Optional[Callable] = None

    def set_active_mode_fn(self, fn: Callable):
        """fn() returns the current trading mode ('paper'/'real').
        Prevents real_trader.on_tick from placing exchange orders while in paper mode.
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
                log.exception("RealTrader broadcast error")

    @staticmethod
    def _iter_dicts(obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from RealTrader._iter_dicts(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from RealTrader._iter_dicts(item)

    @classmethod
    def _extract_broker_error(cls, payload) -> Optional[str]:
        if not isinstance(payload, (dict, list)):
            return None

        for d in cls._iter_dicts(payload):
            if d.get("status") == "error":
                return str(d.get("message") or "Unknown error")
            if d.get("error") or d.get("Error") or d.get("Error Message"):
                return str(d.get("error") or d.get("Error") or d.get("Error Message"))

            stat = str(d.get("stat", "")).strip().lower()
            if stat in ("not_ok", "not ok", "notok"):
                return str(d.get("errMsg") or d.get("message") or "Kotak returned Not_Ok")

        return None

    @classmethod
    def _extract_order_rows(cls, payload) -> list[dict]:
        if not isinstance(payload, (dict, list)):
            return []

        for d in cls._iter_dicts(payload):
            rows = d.get("data") if isinstance(d, dict) else None
            if not isinstance(rows, list):
                continue
            if not rows:
                continue
            if not all(isinstance(r, dict) for r in rows):
                continue
            if any(("ordSt" in r) or ("nOrdNo" in r) for r in rows):
                return rows

        return []

    @classmethod
    def _extract_order_id(cls, result: dict) -> str:
        """Extract the first non-empty broker order id from a nested payload."""
        if not isinstance(result, (dict, list)):
            return ""

        for d in cls._iter_dicts(result):
            oid = d.get("nOrdNo")
            if oid is not None and str(oid).strip():
                return str(oid).strip()

        return ""

    # ── Symbol Resolution ─────────────────────────────────────────────────────

    def _resolve_symbol(self, signal: dict) -> str:
        """Resolve the exact pTrdSymbol via search_scrip, with day-scoped cache.

        FIX #10: Cache key includes today's date so it auto-invalidates between
        trading sessions without any explicit flush call.
        """
        strike   = str(signal.get("strike", ""))
        opt_type = str(signal.get("option_type", "")).upper()
        today    = date.today()
        cache_key = (today, strike, opt_type)

        if cache_key in self._symbol_cache:
            return self._symbol_cache[cache_key]

        fallback = f"SENSEX{int(strike)}{opt_type}"

        # Try 1: search_scrip via Kotak API
        if self.kotak and self.kotak.is_authenticated:
            try:
                scrip = self.kotak.search_scrip(
                    symbol="SENSEX",
                    option_type=opt_type,
                    strike_price=strike,
                )
                log.info("search_scrip(%s): %s", cache_key, scrip)
                if scrip and isinstance(scrip, dict):
                    instruments = scrip.get("data", [])
                    if isinstance(instruments, list) and instruments:
                        resolved = instruments[0].get("pTrdSymbol", "")
                        if resolved:
                            self._symbol_cache[cache_key] = resolved
                            log.info("Resolved symbol via search_scrip: %s → %s", cache_key, resolved)
                            return resolved
            except Exception:
                log.exception("search_scrip failed for %s", cache_key)

        # Try 2: Look in market feed subscriptions (populated by contract_master)
        if self.market_feed:
            suffix = f"{strike}{opt_type}".upper()
            for _tk, info in self.market_feed._subscriptions.items():
                sym = (info.get("symbol") or "").upper()
                if sym.startswith("SENSEX") and sym.endswith(suffix):
                    self._symbol_cache[cache_key] = info["symbol"]
                    log.info("Resolved symbol via market_feed: %s → %s", cache_key, info["symbol"])
                    return info["symbol"]

        log.warning("Using fallback symbol: %s (search_scrip and market_feed both failed)", fallback)
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

        activation_points    = float(strategy.get("activationPoints") or DEFAULT_ACTIVATION_PTS)
        activation_sl_offset = float(strategy.get("activationSLOffset") or 0.0)
        trail_gap            = float(strategy.get("trailGap")         or DEFAULT_TRAIL_GAP)
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
            "activation_points":    activation_points,
            "activation_sl_offset": activation_sl_offset,
            "trail_gap":            trail_gap,
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

    async def _kotak_call_with_retry(
        self,
        fn,
        description: str,
        retries: int = MAX_ORDER_RETRIES,
        delay_s: float = ORDER_RETRY_DELAY_S,
        **kwargs,
    ) -> dict:
        """Call a Kotak API function with retry + error broadcasting."""
        for attempt in range(1, retries + 1):
            try:
                result = await asyncio.to_thread(fn, **kwargs)
                err = self._extract_broker_error(result)
                if err:
                    raise Exception(err)
                return result
            except Exception as e:
                log.error(
                    "%s failed (attempt %d/%d): %s",
                    description, attempt, retries, e,
                )
                if attempt < retries:
                    await asyncio.sleep(delay_s)
                else:
                    await self._broadcast("order_alert", {
                        "level":   "error",
                        "message": f"{description} FAILED after {retries} attempts: {e}",
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

            kotak_order_id = self._extract_order_id(result)
            if not kotak_order_id:
                raise Exception("Kotak place_order response missing nOrdNo")

            await db.update_trade(order["trade_id"], {"kotak_order_id": kotak_order_id})

            await self._broadcast("order_update", {
                "id":             order["trade_id"],
                "signal_id":      order.get("signal_id"),
                "status_note":    f"✅ BUY Placed — Order #{kotak_order_id}",
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

    # ── Poll Order Fill (consolidated) ───────────────────────────────────────

    async def _poll_order_fill(
        self,
        kotak_order_id: str,
        context: dict,
        label: str = "order",
    ) -> Optional[dict]:
        """Poll order_history until a terminal fill status is confirmed.

        FIX #9: Replaces the duplicated _verify_fill / _verify_exit_fill methods.
        Used for both entry and exit IOC fills.

        Returns:
            {"status": "traded", "fill_price": float, "fill_qty": int}
            or None if rejected / cancelled / timed-out.
        """
        if not kotak_order_id or not self.kotak or not self.kotak.is_authenticated:
            log.warning("_poll_order_fill: cannot verify — missing order_id or auth")
            return None

        trading_symbol = context.get("trading_symbol", "")

        for attempt in range(1, FILL_VERIFY_POLLS + 1):
            await asyncio.sleep(FILL_VERIFY_INTERVAL_S)
            try:
                hist = await asyncio.to_thread(
                    self.kotak.order_history, order_id=kotak_order_id
                )
                log.info(
                    "_poll_order_fill [%s] poll %d/%d for %s: raw=%s",
                    label, attempt, FILL_VERIFY_POLLS, kotak_order_id, hist,
                )
                if not hist or not isinstance(hist, dict):
                    continue

                hist_err = self._extract_broker_error(hist)
                if hist_err:
                    log.warning("_poll_order_fill poll %d: order_history error: %s", attempt, hist_err)
                    continue

                data_list = self._extract_order_rows(hist)
                if not data_list:
                    continue

                matching = [r for r in data_list if str(r.get("nOrdNo", "")).strip() == str(kotak_order_id)]
                latest = matching[0] if matching else data_list[0]
                status = str(latest.get("ordSt", "")).lower().strip()

                log.info(
                    "_poll_order_fill [%s] poll %d/%d: ordSt=%s avgPrc=%s fldQty=%s",
                    label, attempt, FILL_VERIFY_POLLS, status,
                    latest.get("avgPrc"), latest.get("fldQty"),
                )

                if status in ("traded", "complete", "completed"):
                    fill_price = float(
                        latest.get("avgPrc", 0) or latest.get("flPrc", 0) or
                        latest.get("prc", 0) or context.get("current_price", 0) or
                        context.get("price", 0) or 0
                    )
                    fill_qty = int(
                        latest.get("fldQty", 0) or latest.get("flQty", 0) or
                        latest.get("qty", 0) or context.get("quantity", 0) or 0
                    )

                    if fill_qty <= 0:
                        log.error(
                            "_poll_order_fill: non-positive fill qty for %s (order %s)",
                            trading_symbol, kotak_order_id,
                        )
                        return None

                    log.info(
                        "Fill VERIFIED [%s]: %s status=%s price=%.2f qty=%d (poll %d/%d)",
                        label, trading_symbol, status, fill_price, fill_qty,
                        attempt, FILL_VERIFY_POLLS,
                    )
                    return {"status": "traded", "fill_price": fill_price, "fill_qty": fill_qty}

                if status in ("rejected", "cancelled"):
                    reason = latest.get("rejRsn", "Unknown")
                    log.error(
                        "[%s] order %s for %s: %s — %s (poll %d/%d)",
                        label, status.upper(), trading_symbol, kotak_order_id, reason,
                        attempt, FILL_VERIFY_POLLS,
                    )
                    await self._broadcast("order_alert", {
                        "level":   "error",
                        "message": f"❌ {label.upper()} {status.upper()}: {trading_symbol} — {reason}",
                    })
                    return {"status": status, "reject_reason": reason}

                # "open", "open pending", "validation pending", etc. — keep polling

            except Exception:
                log.exception("_poll_order_fill [%s] poll %d failed for %s", label, attempt, kotak_order_id)

        log.error(
            "_poll_order_fill [%s]: unable to confirm fill for %s after %d polls — treating as UNFILLED",
            label, kotak_order_id, FILL_VERIFY_POLLS,
        )
        await self._broadcast("order_alert", {
            "level":   "warning",
            "message": (
                f"⚠️ {label.upper()} fill unconfirmed for {trading_symbol} "
                f"(order #{kotak_order_id}) — check order book manually"
            ),
        })
        return None

    def _resolve_entry_fill_waiter(self, order_id: str, payload: dict):
        fut = self._entry_fill_waiters.get(str(order_id))
        if fut and not fut.done():
            fut.set_result(payload)

    @staticmethod
    def _coerce_float(v, default=0.0):
        try:
            return float(v)
        except Exception:
            return default

    @staticmethod
    def _coerce_int(v, default=0):
        try:
            return int(float(v))
        except Exception:
            return default

    def _entry_fill_from_feed(self, event: dict, order: dict) -> Optional[dict]:
        status = str(event.get('ordSt', '')).lower().strip()

        if status in ('traded', 'complete', 'completed'):
            fill_price = (
                self._coerce_float(event.get('avgPrc'), 0.0)
                or self._coerce_float(event.get('flPrc'), 0.0)
                or self._coerce_float(event.get('prc'), 0.0)
                or self._coerce_float(order.get('price', 0), 0.0)
            )
            fill_qty = (
                self._coerce_int(event.get('fldQty'), 0)
                or self._coerce_int(event.get('flQty'), 0)
                or self._coerce_int(event.get('qty'), 0)
                or int(order.get('quantity', 0) or 0)
            )
            if fill_qty <= 0:
                fill_qty = int(order.get('quantity', 0) or 0)

            log.info(
                'Fill VERIFIED via order-feed: %s status=%s price=%.2f qty=%d',
                order.get('trading_symbol'), status, fill_price, fill_qty,
            )
            return {
                'status':          'traded',
                'fill_price':      fill_price,
                'fill_qty':        fill_qty,
                'confirm_source':  'feed',
            }

        if status in ('rejected', 'cancelled'):
            reason = event.get('rejRsn') or event.get('ordUsrMsg') or 'Unknown'
            log.warning(
                'Entry order %s for %s via order-feed: %s',
                status.upper(), order.get('trading_symbol'), reason,
            )
            return None

        return None

    async def _confirm_entry_fill(self, kotak_order_id: str, order: dict) -> Optional[dict]:
        """Confirm entry fill using order-feed first, polling fallback."""
        if not kotak_order_id:
            return None

        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._entry_fill_waiters[str(kotak_order_id)] = waiter
        poll_task = asyncio.create_task(self._poll_order_fill(kotak_order_id, order, label="entry"))

        try:
            try:
                done, _ = await asyncio.wait(
                    {waiter, poll_task},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=FILL_CONFIRM_TIMEOUT_S,
                )
            except Exception:
                log.exception("_confirm_entry_fill: confirmation wait failed for %s", kotak_order_id)
                return None

            if not done:
                log.warning(
                    "_confirm_entry_fill: timeout after %.1fs for %s (%s)",
                    FILL_CONFIRM_TIMEOUT_S, kotak_order_id, order.get("trading_symbol"),
                )
                await self._broadcast("order_alert", {
                    "level": "warning",
                    "message": (
                        f"⚠️ Fill confirmation timeout ({FILL_CONFIRM_TIMEOUT_S:.1f}s) for "
                        f"{order.get('trading_symbol')} (order #{kotak_order_id})"
                    ),
                })
                return None

            if waiter in done:
                feed_payload = waiter.result() or {}
                feed_event   = feed_payload.get('event', {}) if isinstance(feed_payload, dict) else {}
                feed_result  = self._entry_fill_from_feed(feed_event, order)
                if not poll_task.done():
                    poll_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await poll_task
                return feed_result

            poll_result = await poll_task
            if isinstance(poll_result, dict) and poll_result.get('status') == 'traded':
                poll_result.setdefault('confirm_source', 'poll')
            return poll_result

        finally:
            key = str(kotak_order_id)
            current = self._entry_fill_waiters.get(key)
            if current is waiter:
                self._entry_fill_waiters.pop(key, None)
            if not poll_task.done():
                poll_task.cancel()
                with suppress(asyncio.CancelledError):
                    await poll_task

    # ── Software SL — store trigger level only (no exchange order) ───────────

    async def _place_sl_order(self, pos: dict, trigger_price: float) -> Optional[str]:
        """Software SL: store the SL trigger level in memory + DB.

        Exchange DAY SL orders (order_type=SL, validity=DAY) require option
        writing margin (~₹1.43L per lot) because Kotak RMS treats them as a
        potential short position.  Retail option buyers don't have that margin.

        Instead we monitor price on every tick and fire an IOC SELL in
        _process_position_tick when price ≤ trailing_sl.  This is identical
        to how Zerodha GTT works — broker-side software trigger.

        We use "SW:<position_id>" as a sentinel sl_order_id so the rest of
        the code knows an SL is active without expecting a real exchange order.
        """
        sentinel = f"SW:{pos['id']}"
        pos["sl_order_id"] = sentinel
        try:
            await db.update_position(pos["id"], {
                "sl_order_id": sentinel,
                "trailing_sl": trigger_price,
            })
        except Exception:
            log.exception("_place_sl_order: DB update failed for %s", pos["trading_symbol"])

        log.info(
            "Software SL active: %s trigger=%.2f (sentinel=%s)",
            pos["trading_symbol"], trigger_price, sentinel,
        )
        await self._broadcast("order_update", {
            "id":          pos.get("trade_id"),
            "status_note": f"🛡 Software SL set @ {trigger_price:.2f}",
        })
        return sentinel

    # ── Update Software SL (Trail) ────────────────────────────────────────────

    async def _update_sl_order(self, pos: dict, new_trigger: float):
        """Software SL trail: just update trailing_sl in memory + DB.

        No exchange order to modify — the tick handler reads trailing_sl
        directly on every tick and fires an IOC SELL when triggered.
        """
        # Software SL — no exchange order to modify, just update the level
        # The sentinel sl_order_id ("SW:<id>") signals software SL is active.
        sl_order_id = pos.get("sl_order_id")
        if not sl_order_id:
            # SL not yet set — initialise it now
            await self._place_sl_order(pos, new_trigger)
            return

        log.info("Software SL trailed: %s → %.2f", pos["trading_symbol"], new_trigger)
        pos["trailing_sl"] = new_trigger
        try:
            await db.update_position(pos["id"], {"trailing_sl": new_trigger})
        except Exception:
            log.exception("_update_sl_order: DB trail update failed for %s", pos["trading_symbol"])

        await self._broadcast("order_update", {
            "id":          pos.get("trade_id"),
            "status_note": f"📈 SL trailed → {new_trigger:.2f}",
        })

    # ── Batched Position DB Writes ─────────────────────────────────────────────

    async def _flush_position_cache(self, position_id: int = None):
        """Flush cached position writes to DB.

        If position_id is given, flush only that position.
        Otherwise flush all cached entries.
        """
        if position_id is not None:
            fields = self._pos_write_cache.pop(position_id, None)
            if fields:
                try:
                    await db.update_position(position_id, fields)
                except Exception:
                    log.exception("_flush_position_cache: failed for position %d", position_id)
            return

        if not self._pos_write_cache:
            return

        items = list(self._pos_write_cache.items())
        self._pos_write_cache.clear()
        for pid, fields in items:
            try:
                await db.update_position(pid, fields)
            except Exception:
                log.exception("_flush_position_cache: batch write failed for position %d", pid)

    async def _delayed_flush_positions(self):
        """Coalesce position writes for POS_FLUSH_INTERVAL_S then flush."""
        await asyncio.sleep(POS_FLUSH_INTERVAL_S)
        await self._flush_position_cache()

    def _schedule_pos_flush(self):
        """Schedule a deferred flush if one isn't already pending."""
        if self._pos_flush_task is None or self._pos_flush_task.done():
            self._pos_flush_task = asyncio.create_task(self._delayed_flush_positions())

    # ── Fill Order ────────────────────────────────────────────────────────────

    async def _fill_order(self, order: dict, fill_price: float, filled_qty: int = None) -> dict:
        """Open a position after entry fill, place exchange SL order.

        FIX #3: If db.save_position raises after db.update_trade, the trade is
        marked 'fill_error' so it surfaces in the DB rather than being silently
        orphaned as status='filled' with no position row.
        """
        trade_id = order.get("trade_id")
        if filled_qty is None or filled_qty <= 0:
            raise ValueError(f"Invalid filled_qty for trade {trade_id}: {filled_qty}")
        qty = filled_qty

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
                "min_ltp":    order.get("min_ltp"),   # flush deferred min_ltp on fill
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
            "activation_points":    order.get("activation_points", DEFAULT_ACTIVATION_PTS),
            "activation_sl_offset": order.get("activation_sl_offset", DEFAULT_ACTIVATION_SL_OFFSET),
            "trail_gap":            order.get("trail_gap", DEFAULT_TRAIL_GAP),
            "sl_activated":      False,
            "exit_timer_mins":   order.get("exit_timer_mins", POSITION_TIMEOUT_MINS),
        }

        try:
            position_id = await db.save_position(trade_id, pos_data)
        except Exception:
            # FIX #3: Mark trade as fill_error so it's visible for manual review.
            log.exception("_fill_order: db.save_position failed — marking trade fill_error")
            try:
                await db.update_trade(trade_id, {"status": "fill_error"})
            except Exception:
                log.exception("_fill_order: could not set fill_error status on trade %d", trade_id)
            await self._broadcast("order_alert", {
                "level":   "error",
                "message": (
                    f"🚨 DB save_position FAILED for {order['trading_symbol']} "
                    f"(trade {trade_id}) — position NOT tracked. MANUAL ACTION REQUIRED."
                ),
            })
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

        # Activate software SL (no exchange order — tick handler fires IOC SELL)
        sl_order_id = await self._place_sl_order(position, initial_sl)
        position["sl_order_id"] = sl_order_id

        log.info(
            "Position opened | Real | Entry=%.2f | Initial SL=%.2f | Software SL=%s",
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_fresh_ltp(self, pos: dict) -> float:
        """Best available LTP for a position — market feed > current_price > entry_price."""
        if self.market_feed and hasattr(self.market_feed, "get_last_price"):
            with suppress(Exception):
                ltp = self.market_feed.get_last_price(pos.get("trading_symbol"))
                if ltp:
                    return ltp
        return pos.get("current_price") or pos.get("entry_price", 0)

    # ── Symbol Matching ───────────────────────────────────────────────────────

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
        # ── Mode guard: do nothing if we are not the active engine ──────────────
        if self._active_mode and self._active_mode() != "real":
            return
        now = datetime.now(timezone.utc)
        tick_symbol = data.get("symbol", "")

        # 1. Update open positions
        for pos in list(self._open_positions):
            # Allow 'close_failed' through so auto-heal fires on next tick
            if pos.get("status") not in ("open", "close_failed"):
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
        orders_to_fill = []
        async with self._expiry_lock:
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

                in_range = order["entry_low"] <= ltp <= order["entry_high"]
                if in_range:
                    if order.get("min_ltp") is None or ltp < order["min_ltp"]:
                        order["min_ltp"] = ltp
                        # DB write deferred to fill/expiry — removed from hot path (perf fix)
                        await self._broadcast("order_update", {
                            "id":          order["trade_id"],
                            "signal_id":   order.get("signal_id"),
                            "min_ltp":     ltp,
                            "status_note": f"Tracking bounce from {ltp}",
                        })

                bounce_points = order.get("bounce_points", DEFAULT_BOUNCE_POINTS)
                if (order.get("min_ltp") is not None and
                        ltp >= order["min_ltp"] + bounce_points and
                        ltp >= order["entry_low"] and
                        not order.get("in_flight")):
                    order["in_flight"] = True
                    if order in self._pending_orders:
                        self._pending_orders.remove(order)
                    orders_to_fill.append((order, ltp))

            for order in expired:
                if order in self._pending_orders:
                    self._pending_orders.remove(order)

        # Phase 2: Outside lock — BUY + verify (slow)
        for order, fill_ltp in orders_to_fill:
            async with self._order_lock:
                entry_result = await self._place_entry_order(order, fill_ltp)
                if entry_result:
                    kotak_oid = entry_result.get("kotak_order_id", "")

                    fill_data = await self._confirm_entry_fill(kotak_oid, order)

                    if fill_data and fill_data["status"] == "traded":
                        verified_price = fill_data["fill_price"]
                        verified_qty   = fill_data["fill_qty"]
                        await self._fill_order(order, verified_price, filled_qty=verified_qty)
                        log.info(
                            "Real order FILLED: %s @ %.2f qty=%d min_ltp=%s source=%s",
                            order["trading_symbol"], verified_price, verified_qty,
                            order["min_ltp"], fill_data.get('confirm_source', 'poll'),
                        )
                    else:
                        log.warning(
                            "BUY not confirmed for %s — attempting cancel on Kotak (order %s)",
                            order["trading_symbol"], kotak_oid,
                        )
                        cancelled = False
                        if kotak_oid and self.kotak and self.kotak.is_authenticated:
                            try:
                                cancel_result = await asyncio.to_thread(
                                    self.kotak.cancel_order, order_id=kotak_oid
                                )
                                log.info("Cancel result for %s: %s", kotak_oid, cancel_result)
                                cancel_err = self._extract_broker_error(cancel_result)
                                if cancel_err:
                                    log.warning("Cancel rejected for %s: %s", kotak_oid, cancel_err)
                                else:
                                    cancelled = True
                            except Exception:
                                log.exception("Cancel attempt failed for order %s", kotak_oid)

                        if cancelled:
                            log.info("Order %s cancelled on Kotak — marking trade expired", kotak_oid)
                            try:
                                await db.update_trade(order["trade_id"], {"status": "expired"})
                                if order.get("pending_order_id"):
                                    await db.delete_pending_order(order["pending_order_id"])
                            except Exception:
                                log.exception("cleanup after cancel failed")
                            await self._broadcast("order_update", {
                                "id":          order["trade_id"],
                                "signal_id":   order.get("signal_id"),
                                "status":      "expired",
                                "status_note": "BUY cancelled — order not filled",
                            })
                            await self._broadcast("order_alert", {
                                "level":   "warning",
                                "message": f"⚠️ BUY cancelled on Kotak: {order['trading_symbol']} (fill unconfirmed)",
                            })
                        else:
                            log.warning(
                                "Cannot cancel order %s — re-verifying fill for %s",
                                kotak_oid, order["trading_symbol"],
                            )
                            await asyncio.sleep(2)
                            fill_data_retry = await self._poll_order_fill(kotak_oid, order, label="entry_retry")

                            if fill_data_retry and fill_data_retry["status"] == "traded":
                                verified_price = fill_data_retry["fill_price"]
                                verified_qty   = fill_data_retry["fill_qty"]
                                await self._fill_order(order, verified_price, filled_qty=verified_qty)
                                log.info(
                                    "VERIFIED on retry: %s @ %.2f qty=%d [poll_retry]",
                                    order["trading_symbol"], verified_price, verified_qty,
                                )
                                await self._broadcast("order_alert", {
                                    "level":   "success",
                                    "message": f"✅ Fill VERIFIED (retry): {order['trading_symbol']} @ ₹{verified_price:.2f}",
                                })
                            else:
                                log.error(
                                    "CRITICAL: Order %s for %s — cannot cancel AND cannot verify. "
                                    "CHECK KOTAK APP IMMEDIATELY.",
                                    kotak_oid, order["trading_symbol"],
                                )
                                await self._broadcast("order_alert", {
                                    "level":   "error",
                                    "message": f"🚨 UNVERIFIED ORDER: {order['trading_symbol']} (#{kotak_oid}) — CHECK KOTAK APP NOW",
                                })
                                await self._broadcast("order_update", {
                                    "id":          order["trade_id"],
                                    "signal_id":   order.get("signal_id"),
                                    "status_note": f"🚨 Unverified — check Kotak order #{kotak_oid}",
                                })

    # ── Position Tick Processor ───────────────────────────────────────────────

    async def _process_position_tick(self, pos: dict, ltp: float):
        """Process a tick for an open position — signal_trail SL logic.

        Software SL: when ltp drops to/below trailing_sl we fire an IOC SELL
        immediately.  This replaces exchange DAY SL orders which require option
        writing margin (~₹1.43L) that retail buyers don't have.

        FIX #1: On activation, SL is anchored at entry_price + activation_points
        (the breakeven+ level), not at the current LTP which could be much higher
        and would cause immediate SL triggers on the next dip.
        """
        if ltp <= 0:
            return

        pos["current_price"] = ltp
        pos["pnl"]           = (ltp - pos["entry_price"]) * pos["quantity"]
        new_sl               = None

        entry_price          = pos["entry_price"]
        activation_points    = pos.get("activation_points", DEFAULT_ACTIVATION_PTS)
        activation_sl_offset = pos.get("activation_sl_offset", DEFAULT_ACTIVATION_SL_OFFSET)
        trail_gap            = pos.get("trail_gap", DEFAULT_TRAIL_GAP)

        if not pos.get("sl_activated") and ltp >= entry_price + activation_points:
            # FIX #1: Anchor initial SL at entry + activation_points (breakeven+), not at ltp.
            # activationSLOffset (default 0) lets user soften the anchor:
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

            await self._update_sl_order(pos, new_sl)

            await self._broadcast("position_update", {
                "id":          pos["id"],
                "trailing_sl": new_sl,
                "max_ltp":     pos.get("max_ltp", ltp),
                "status_note": f"SL trailed to {new_sl:.2f} [signal_trail]",
            })

        # ── Software SL trigger ────────────────────────────────────────────────
        # Fire immediately when price hits or breaks the SL level.
        # Guard: only fire if an SL is active and position is still open.
        trailing_sl = pos.get("trailing_sl", 0)

        # Auto-heal: if a previous close attempt was rejected at exchange level,
        # status is 'close_failed'. Retry close on every tick until it succeeds.
        if pos.get("status") == "close_failed":
            log.warning(
                "AUTO-HEAL: retrying close for %s (close_failed) ltp=%.2f",
                pos["trading_symbol"], ltp,
            )
            pos["status"] = "open"  # Reset so close_position accepts it
            await self.close_position(pos["id"], exit_price=ltp, exit_reason="software_sl")
            return

        if trailing_sl > 0 and ltp <= trailing_sl and pos.get("status") == "open":
            # Prevent re-entrant SL triggers: if close_position is already handling
            # this position, skip. This prevents the bug where a cancelled IOC SELL
            # resets status to "open" and the next tick fires another SELL.
            if pos.get("_sl_exit_pending"):
                return
            pos["_sl_exit_pending"] = True
            log.warning(
                "SOFTWARE SL HIT: %s ltp=%.2f ≤ sl=%.2f — firing IOC SELL @ LTP*0.90 (best bid)",
                pos["trading_symbol"], ltp, trailing_sl,
            )
            await self._broadcast("order_alert", {
                "level":   "warning",
                "message": f"🔴 SL Hit: {pos['trading_symbol']} @ {ltp:.2f} (SL={trailing_sl:.2f}) — exiting",
            })
            # Use 'software_sl' so close_position PLACES the IOC SELL.
            # (exit_reason='sl' skips the SELL — that was only for exchange-triggered SL.)
            await self.close_position(pos["id"], exit_price=ltp, exit_reason="software_sl")
            return  # Skip DB update — close_position handles it

        # Batch DB write — coalesce into cache, flush every POS_FLUSH_INTERVAL_S.
        # Critical writes (SL trail, close) bypass the cache and go directly to DB.
        self._pos_write_cache[pos["id"]] = {
            "current_price": ltp,
            "pnl":           pos["pnl"],
            "max_ltp":       pos.get("max_ltp", ltp),
            "trailing_sl":   pos["trailing_sl"],
            "sl_activated":  int(bool(pos.get("sl_activated", False))),
        }
        self._schedule_pos_flush()

        await self._broadcast("position_update", {
            "id":            pos["id"],
            "current_price": ltp,
            "pnl":           pos["pnl"],
        })

    # ── Close Position ────────────────────────────────────────────────────────

    async def close_position(self, position_id: int, exit_price: float = None, exit_reason: str = None) -> dict:
        async with self._close_lock:
            pos = None
            for p in self._open_positions:
                if p["id"] == position_id:
                    pos = p
                    break

            if pos is None:
                return {"status": "error", "message": "Position not found"}
            if pos.get("status") == "closed":
                return {"status": "error", "message": "Already closed"}
            if pos.get("status") == "closing":
                return {"status": "error", "message": "Close in progress"}

            # Discard any cached writes — close_position writes final state directly
            self._pos_write_cache.pop(position_id, None)

            pos["status"] = "closing"
            if exit_reason:
                pos["exit_reason"] = exit_reason

            price = exit_price or pos.get("current_price", pos["entry_price"])
            pnl = (price - pos["entry_price"]) * pos["quantity"]
            exchange_exit_confirmed = (exit_reason == "sl")
            db_sync_ok = False

            try:
                # 1. Software SL — no exchange SL order to cancel.
                #    Just clear the sentinel so reconcile doesn't check it.
                pos["sl_order_id"] = None

                # 2. Place Limit SELL IOC
                # Skip only if the exchange SL order already filled ('sl' exit reason
                # from the old exchange-SL path).  Software SL ('software_sl') always
                # needs to place the SELL — the bot fires the exit, not the exchange.
                if exit_reason != "sl":
                    try:
                        # For automated exits (SL, timer, EOD), retry IOC SELL
                        # with refreshed LTP until filled. In fast-crashing markets,
                        # the option may drop >10% between ticks, causing the first
                        # IOC SELL (at 10% below stale LTP) to get cancelled.
                        # Each retry refreshes LTP from pos["current_price"]
                        # (continuously updated by live ticks) and recalculates
                        # the sell price to stay within BSE's Operating Range.
                        exit_attempt = 0

                        while True:
                            exit_attempt += 1
                            if exit_reason in ("software_sl", "timer", "eod"):
                                approx_ltp = pos.get("current_price", pos.get("entry_price", 0))
                                # Round DOWN to BSE tick (0.05) — avoids RATE NOT MULTIPLE OF TICK rejection
                                sell_price = _round_to_tick(max(SL_LIMIT_PRICE, approx_ltp * EXIT_FLOOR_DISCOUNT))
                            else:
                                exit_slippage = float(pos.get("exit_slippage", DEFAULT_EXIT_SLIPPAGE))
                                approx_ltp    = pos.get("current_price", pos.get("entry_price", 0))
                                sell_price    = _round_to_tick(max(SL_LIMIT_PRICE, approx_ltp - exit_slippage))

                            log.info(
                                "EXIT SELL attempt %d: %s @ %.2f (ltp=%.2f, reason=%s)",
                                exit_attempt,
                                pos["trading_symbol"], sell_price, approx_ltp, exit_reason,
                            )

                            sell_result = await self._kotak_call_with_retry(
                                self.kotak.place_order,
                                f"SELL {pos['trading_symbol']}",
                                exchange_segment="bse_fo",
                                trading_symbol=pos["trading_symbol"],
                                transaction_type="S",
                                order_type="L",
                                quantity=pos["quantity"],
                                price=sell_price,
                                validity="IOC",
                            )
                            sell_order_id = self._extract_order_id(sell_result)
                            if not sell_order_id:
                                raise Exception("SELL place_order response missing nOrdNo")

                            sell_fill = await self._poll_order_fill(sell_order_id, pos, label="exit")

                            if sell_fill and sell_fill.get("status") == "traded":
                                exchange_exit_confirmed = True
                                fill_px = float(sell_fill.get("fill_price") or 0)
                                if fill_px > 0:
                                    price = fill_px
                                pnl = (price - pos["entry_price"]) * pos["quantity"]
                                await self._broadcast("order_update", {
                                    "id":          pos.get("trade_id"),
                                    "status_note": f"✅ SELL Filled @ {price:.2f} ({exit_reason}) [attempt {exit_attempt}]",
                                })
                                break  # Exit filled — done

                            elif sell_fill and sell_fill.get("status") == "rejected":
                                rej_reason = sell_fill.get("reject_reason", "") or ""
                                rej_lower  = rej_reason.lower()

                                # Check if the exchange is telling us the position is already gone
                                already_gone = any(k in rej_lower for k in _ALREADY_EXITED_REASONS)

                                if already_gone:
                                    # Safe to close in DB — exchange confirms no open holding
                                    log.warning(
                                        "EXIT SELL REJECTED (already exited) for %s (#%s): %s — closing in DB",
                                        pos["trading_symbol"], sell_order_id, rej_reason,
                                    )
                                    exchange_exit_confirmed = True
                                    await self._broadcast("order_alert", {
                                        "level":   "warning",
                                        "message": (
                                            f"⚠️ Exit SELL rejected for {pos['trading_symbol']} "
                                            f"(already closed on exchange: {rej_reason}) — closing in DB"
                                        ),
                                    })
                                    break

                                else:
                                    # Genuine price/circuit rejection — do NOT assume position is gone.
                                    # Log, alert, and break WITHOUT setting exchange_exit_confirmed.
                                    # The finally block will set status='close_failed' and the
                                    # next tick's auto-heal will retry.
                                    log.error(
                                        "EXIT SELL REJECTED (genuine) for %s (#%s): %s "
                                        "— NOT treating as closed. Will retry on next tick.",
                                        pos["trading_symbol"], sell_order_id, rej_reason,
                                    )
                                    await self._broadcast("order_alert", {
                                        "level":   "error",
                                        "message": (
                                            f"🚨 SL SELL rejected at exchange for {pos['trading_symbol']}: "
                                            f"{rej_reason} — auto-retry on next tick"
                                        ),
                                    })
                                    break  # exit the while loop; finally sets close_failed → auto-heal

                            else:
                                # Cancelled / unverified — retry with refreshed LTP (capped)
                                if exit_attempt >= MAX_EXIT_SELL_ATTEMPTS:
                                    log.error(
                                        "EXIT SELL: max retries (%d) reached for %s — marking close_failed",
                                        MAX_EXIT_SELL_ATTEMPTS, pos["trading_symbol"],
                                    )
                                    await self._broadcast("order_alert", {
                                        "level":   "error",
                                        "message": (
                                            f"🚨 Exit SELL cancelled {MAX_EXIT_SELL_ATTEMPTS}x for "
                                            f"{pos['trading_symbol']} — position left open, auto-retry on next tick"
                                        ),
                                    })
                                    break  # finally sets close_failed → auto-heal

                                log.warning(
                                    "EXIT SELL cancelled/unverified for %s — retrying (attempt %d/%d) with refreshed LTP",
                                    pos["trading_symbol"], exit_attempt, MAX_EXIT_SELL_ATTEMPTS,
                                )
                                await asyncio.sleep(0.2)
                                continue

                    except Exception:
                        log.exception("Failed to place/verify exit SELL for %s", pos["trading_symbol"])
                        await self._broadcast("order_alert", {
                            "level":   "error",
                            "message": f"❌ EXIT SELL FAILED for {pos['trading_symbol']} — MANUAL EXIT REQUIRED",
                        })
                        return {"status": "error", "message": "Exit SELL failed"}

                # 3. Update DB only after confirmed exit
                if not exchange_exit_confirmed:
                    return {"status": "error", "message": "Exchange exit not confirmed"}

                closed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                await db.update_position(position_id, {
                    "status":        "closed",
                    "current_price": price,
                    "pnl":           pnl,
                    "closed_at":     closed_at,
                    **({"exit_reason": exit_reason} if exit_reason else {}),
                })
                await db.update_trade(pos["trade_id"], {
                    "status":     "closed",
                    "pnl":        pnl,
                    "exit_price": price,
                    "closed_at":  closed_at,
                    **({"exit_reason": exit_reason} if exit_reason else {}),
                })
                db_sync_ok = True

                pos["status"] = "closed"
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
                    "closed_at":   closed_at,
                    "exit_reason": exit_reason,
                })

                return {"status": "closed", "pnl": pnl, "exit_price": price, "exit_reason": exit_reason}

            except Exception:
                log.exception("close_position: db update failed")
                await self._broadcast("order_alert", {
                    "level": "error",
                    "message": (
                        f"🚨 Exit confirmed but DB close failed for {pos.get('trading_symbol')} "
                        f"(position {position_id}) — service intervention required"
                    ),
                })
                return {"status": "error", "message": "Exit confirmed but DB sync failed"}
            finally:
                if pos and pos.get("status") == "closing" and not db_sync_ok and not exchange_exit_confirmed:
                    if exit_reason in ("software_sl", "timer", "eod"):
                        # Don't reset to "open" — prevents re-entrant SL triggers
                        pos["status"] = "close_failed"
                        log.error(
                            "CLOSE FAILED: %s — manual exit required (exit_reason=%s)",
                            pos.get("trading_symbol"), exit_reason,
                        )
                    else:
                        pos["status"] = "open"  # Only for manual UI exits
                # Always clear the SL guard
                if pos:
                    pos.pop("_sl_exit_pending", None)

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
            pos_status = pos.get("status")
            # Retry close_failed positions even if the timer hasn't elapsed — they're already stuck
            if pos_status == "close_failed":
                exit_price = pos.get("current_price", pos["entry_price"])
                log.warning("TIMEOUT-HEAL: retrying close_failed for %s @ %s", pos["trading_symbol"], exit_price)
                pos["status"] = "open"  # Reset so close_position accepts it
                await self.close_position(pos["id"], exit_price=exit_price, exit_reason="software_sl")
                continue
            if pos_status != "open":
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
        """Auto-close all positions before market close.

        FIX #8: _eod_triggered flag ensures square_off_all is called exactly
        once per session, even if check_eod is called on a tight loop.
        Flag resets at midnight via reset_eod_flag() — call that from your
        daily scheduler or on service startup.
        """
        if self._eod_triggered:
            return

        now_ist_dt   = datetime.now(_IST)
        market_close = now_ist_dt.replace(
            hour=_MARKET_CLOSE.hour, minute=_MARKET_CLOSE.minute, second=0, microsecond=0
        )
        eod_dt = market_close - timedelta(minutes=EOD_EXIT_MINUTES_BEFORE)

        if eod_dt.time() <= now_ist_dt.time() < _MARKET_CLOSE:
            if self._open_positions:
                log.warning("EOD: Auto-closing %d positions before market close", len(self._open_positions))
                self._eod_triggered = True
                await self.square_off_all(exit_reason="eod")

    def reset_eod_flag(self):
        """Reset the EOD trigger flag — call once per session (e.g. on startup or at midnight)."""
        self._eod_triggered = False
        log.info("EOD flag reset")

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

        if status in ('traded', 'complete', 'completed', 'rejected', 'cancelled'):
            self._resolve_entry_fill_waiter(order_id, {'status': status, 'event': data})

        # Software SL: exchange never sees an SL order — nothing to handle here
        # for SL triggers or rejections.  The tick handler fires exits directly.
        # Entry fill events are still processed via _resolve_entry_fill_waiter above.

    # ── Reconciliation ────────────────────────────────────────────────────────

    # ── Cancel a single pending order ─────────────────────────────────────────

    async def cancel_pending_order(self, trade_id: int) -> dict:
        """Cancel a single pending entry order by trade_id."""
        async with self._expiry_lock:
            target = None
            for order in self._pending_orders:
                if order.get("trade_id") == trade_id:
                    target = order
                    break

            if target is None:
                return {"status": "error", "message": "Pending order not found — may already be filled or expired"}

            self._pending_orders.remove(target)

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

        log.info("Pending order cancelled (real): trade_id=%d symbol=%s", trade_id, target.get("trading_symbol"))
        return {"status": "ok", "trade_id": trade_id, "trading_symbol": target.get("trading_symbol")}

    # ── Kill Switch ───────────────────────────────────────────────────────────

    async def square_off_all(self, exit_reason: str = "kill") -> dict:
        """Close all open positions and cancel all pending orders.

        FIX #7: Returns a 'failed' list so callers (EOD, kill switch) can see
        which positions did not close successfully instead of a misleadingly
        clean summary.
        """
        results = []
        failed  = []

        for pos in list(self._open_positions):
            if pos.get("status") != "open":
                continue
            price  = pos.get("current_price", pos.get("entry_price", 0))
            result = await self.close_position(pos["id"], exit_price=price, exit_reason=exit_reason)
            entry  = {"position_id": pos["id"], "symbol": pos.get("trading_symbol"), **result}
            if result.get("status") == "closed":
                results.append(entry)
            else:
                failed.append(entry)
                log.error(
                    "square_off_all: failed to close position %d (%s): %s",
                    pos["id"], pos.get("trading_symbol"), result.get("message"),
                )
                await self._broadcast("order_alert", {
                    "level":   "error",
                    "message": f"🚨 square_off_all: COULD NOT CLOSE {pos.get('trading_symbol')} — {result.get('message')}",
                })

        cancelled = 0
        async with self._expiry_lock:
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
            "KILL SWITCH (real): Closed %d, failed %d, cancelled %d pending",
            len(results), len(failed), cancelled,
        )
        return {
            "status":            "ok" if not failed else "partial",
            "positions_closed":  len(results),
            "positions_failed":  len(failed),
            "orders_cancelled":  cancelled,
            "results":           results,
            "failed":            failed,
        }

    # ── Rehydrate from DB ─────────────────────────────────────────────────────

    async def rehydrate_from_db(self):
        """Restore pending orders and open positions on startup.

        FIX #5: Before calling reconcile_orders, verify each restored position's
        sl_order_id against order_history. If the SL already triggered while the
        service was down, close the position as 'sl' rather than re-placing the
        SL on an already-exited position.
        """
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
                "entry_low":         t.get("entry_low", t.get("price", 0)),
                "entry_high":        t.get("entry_high", t.get("price", 0)),
                "strike":            t.get("strike", ""),
                "option_type":       t.get("option_type", ""),
                "created_at":        t.get("created_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "min_ltp":           t.get("min_ltp"),
                "sl_mode":           t.get("sl_mode", "signal_trail"),
                "signal_stoploss":   t.get("signal_stoploss"),
                "activation_points":    float(t.get("activation_points") or DEFAULT_ACTIVATION_PTS),
                "activation_sl_offset": float(t.get("activation_sl_offset") or DEFAULT_ACTIVATION_SL_OFFSET),
                "trail_gap":            float(t.get("trail_gap")          or DEFAULT_TRAIL_GAP),
                "bounce_points":     float(t.get("bounce_points")      or DEFAULT_BOUNCE_POINTS),
                "entry_logic":       t.get("entry_logic", "code"),
                "entry_label":       t.get("entry_label"),
                "entry_timer_mins":  int(t.get("entry_timer_mins") or ENTRY_TIMEOUT_MINS),
                "exit_timer_mins":   int(t.get("exit_timer_mins")  or POSITION_TIMEOUT_MINS),
                "entry_slippage":    float(t.get("entry_slippage") or DEFAULT_ENTRY_SLIPPAGE),
                "exit_slippage":     float(t.get("exit_slippage")  or DEFAULT_EXIT_SLIPPAGE),
                "signal_trail_initial_sl":        t.get("signal_trail_initial_sl", "telegram"),
                "signal_trail_initial_sl_points": float(t.get("signal_trail_initial_sl_points") or 5.0),
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
                "sl_mode":           p.get("sl_mode", "signal_trail"),
                "signal_stoploss":   p.get("signal_stoploss"),
                "activation_points":    p.get("activation_points") or DEFAULT_ACTIVATION_PTS,
                "activation_sl_offset": p.get("activation_sl_offset") or DEFAULT_ACTIVATION_SL_OFFSET,
                "trail_gap":            p.get("trail_gap")          or DEFAULT_TRAIL_GAP,
                "sl_activated":      bool(p.get("sl_activated", 0)),
                "exit_reason":       p.get("exit_reason"),
                "exit_timer_mins":   int(p.get("exit_timer_mins") or POSITION_TIMEOUT_MINS),
                "exit_slippage":     float(p.get("exit_slippage") or DEFAULT_EXIT_SLIPPAGE),
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

        # FIX #5: Verify each restored SL order before reconciling.
        # If an SL was triggered while the service was down, close the position
        # rather than silently re-placing an SL on a ghost position.
        if restored_positions and self.kotak and self.kotak.is_authenticated:
            await self._verify_sl_orders_on_startup()

    async def _verify_sl_orders_on_startup(self):
        """Check if any restored SL order was triggered while service was down.

        FIX #5 implementation: queries order_history for each open position's
        sl_order_id. If already traded/rejected/cancelled, handles it as if the
        order-feed event was received normally — closing the position for 'sl'
        or re-placing the SL.
        """
        for pos in list(self._open_positions):
            sl_order_id = pos.get("sl_order_id")
            if not sl_order_id:
                continue
            # Software SL: SW: sentinels are not broker orders — skip verification
            if str(sl_order_id).startswith("SW:"):
                continue

            try:
                hist = await asyncio.to_thread(
                    self.kotak.order_history, order_id=sl_order_id
                )
                rows = self._extract_order_rows(hist) if hist else []
                if not rows:
                    continue

                matching = [r for r in rows if str(r.get("nOrdNo", "")).strip() == str(sl_order_id)]
                latest = matching[0] if matching else rows[0]
                status = str(latest.get("ordSt", "")).lower().strip()

                if status in ("traded", "complete", "completed"):
                    fill_price = float(
                        latest.get("avgPrc", 0) or latest.get("flPrc", 0) or
                        pos.get("trailing_sl", 0) or pos.get("entry_price", 0)
                    )
                    log.warning(
                        "STARTUP: SL order %s already triggered for %s @ %.2f — closing position",
                        sl_order_id, pos["trading_symbol"], fill_price,
                    )
                    await self._broadcast("order_alert", {
                        "level":   "warning",
                        "message": f"⚠️ SL triggered while offline: {pos['trading_symbol']} @ ₹{fill_price:.2f}",
                    })
                    pos["sl_order_id"] = None
                    await self.close_position(pos["id"], exit_price=fill_price, exit_reason="sl")

                elif status in ("rejected", "cancelled"):
                    log.warning(
                        "STARTUP: SL order %s was %s for %s — will re-place via reconcile",
                        sl_order_id, status, pos["trading_symbol"],
                    )
                    pos["sl_order_id"] = None  # Force reconcile to re-place it

            except Exception:
                log.exception(
                    "_verify_sl_orders_on_startup: failed to check sl_order_id %s for %s",
                    sl_order_id, pos.get("trading_symbol"),
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