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
from contextlib import suppress
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
SL_PROTECT_RETRIES       = 5
SL_PROTECT_RETRY_DELAY_S = 0.2
RECONCILE_INTERVAL_S     = 30
EOD_EXIT_MINUTES_BEFORE  = 5      # Close positions N minutes before market close
FILL_VERIFY_POLLS        = 5      # Number of order_history polls to confirm fill
FILL_VERIFY_INTERVAL_S   = 0.2    # Seconds between polls (~1s total)
FILL_CONFIRM_TIMEOUT_S   = 2.0    # Hard timeout for feed+poll confirmation race

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
        self._close_lock  = asyncio.Lock()

        # Entry fill confirmation waiters (order_id -> Future)
        # Feed-first confirmation uses this to resolve fills from order-feed events.
        self._entry_fill_waiters: dict[str, asyncio.Future] = {}

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
        """Resolve the exact pTrdSymbol via search_scrip, with day cache.

        Falls back to market feed subscriptions (which use contract_master) if
        search_scrip fails, and finally to a short fallback symbol.
        """
        strike = str(signal.get("strike", ""))
        opt_type = str(signal.get("option_type", "")).upper()
        cache_key = f"{strike}{opt_type}"

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
                result = fn(**kwargs)
                err = self._extract_broker_error(result)
                if err:
                    raise Exception(err)
                return result
            except Exception as e:
                log.error("%s failed (attempt %d/%d): %s", description, attempt, retries, e)
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

    # ── Verify IOC Fill ───────────────────────────────────────────────────────

    async def _verify_fill(self, kotak_order_id: str, order: dict) -> Optional[dict]:
        """Poll order_history to confirm IOC fill. Returns fill data or None.

        IOC orders resolve instantly on the exchange, but the place_order API
        only confirms acceptance — not fill.  This method polls order_history
        in a tight loop (~200ms × 5 = ~1s) to get the actual fill status.

        Returns:
            {"status": "traded", "fill_price": float, "fill_qty": int}
            or None if rejected / cancelled / timed-out / partial fill.
        """
        if not kotak_order_id or not self.kotak or not self.kotak.is_authenticated:
            log.warning("_verify_fill: cannot verify — missing order_id or auth")
            return None

        for attempt in range(1, FILL_VERIFY_POLLS + 1):
            await asyncio.sleep(FILL_VERIFY_INTERVAL_S)
            try:
                hist = self.kotak.order_history(order_id=kotak_order_id)
                log.info(
                    "_verify_fill poll %d/%d for %s: raw response = %s",
                    attempt, FILL_VERIFY_POLLS, kotak_order_id, hist,
                )
                if not hist or not isinstance(hist, dict):
                    continue

                hist_err = self._extract_broker_error(hist)
                if hist_err:
                    log.warning("_verify_fill poll %d: order_history error: %s", attempt, hist_err)
                    continue

                data_list = self._extract_order_rows(hist)
                if not data_list:
                    continue

                # Prefer entry matching this order id if present, else newest row.
                matching = [r for r in data_list if str(r.get("nOrdNo", "")).strip() == str(kotak_order_id)]
                latest = matching[0] if matching else data_list[0]
                status = str(latest.get("ordSt", "")).lower().strip()

                log.info(
                    "_verify_fill poll %d/%d: ordSt=%s, avgPrc=%s, fldQty=%s",
                    attempt, FILL_VERIFY_POLLS, status,
                    latest.get("avgPrc"), latest.get("fldQty"),
                )

                if status in ("traded", "complete", "completed"):
                    # avgPrc = actual exchange fill price, fldQty = filled quantity
                    fill_price = float(latest.get("avgPrc", 0) or latest.get("flPrc", 0) or 0)
                    fill_qty   = int(latest.get("fldQty", 0) or latest.get("flQty", 0) or 0)

                    if fill_price <= 0:
                        fill_price = float(latest.get("prc", 0)) or order.get("price", 0)
                    if fill_qty <= 0:
                        fill_qty = order["quantity"]

                    log.info(
                        "Fill VERIFIED: %s — status=%s, fill_price=%.2f, fill_qty=%d (poll %d/%d)",
                        order["trading_symbol"], status, fill_price, fill_qty,
                        attempt, FILL_VERIFY_POLLS,
                    )
                    return {
                        "status":     "traded",
                        "fill_price": fill_price,
                        "fill_qty":   fill_qty,
                    }

                if status in ("rejected", "cancelled"):
                    reason = latest.get("rejRsn", "Unknown")
                    log.error(
                        "BUY order %s for %s: %s — %s (poll %d/%d)",
                        status.upper(), order["trading_symbol"], kotak_order_id, reason,
                        attempt, FILL_VERIFY_POLLS,
                    )
                    await self._broadcast("order_alert", {
                        "level":   "error",
                        "message": f"❌ BUY {status.upper()}: {order['trading_symbol']} — {reason}",
                    })
                    return None

                # "open", "open pending", "validation pending", etc. — keep polling

            except Exception:
                log.exception("_verify_fill poll %d failed for %s", attempt, kotak_order_id)

        # Exhausted polls — IOC should have resolved by now, treat as failed
        log.error(
            "_verify_fill: unable to confirm fill for %s after %d polls — treating as UNFILLED",
            kotak_order_id, FILL_VERIFY_POLLS,
        )
        await self._broadcast("order_alert", {
            "level":   "warning",
            "message": (
                f"⚠️ BUY fill unconfirmed for {order['trading_symbol']} "
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
                'Fill VERIFIED via order-feed: %s — status=%s, fill_price=%.2f, fill_qty=%d',
                order.get('trading_symbol'), status, fill_price, fill_qty,
            )
            return {
                'status': 'traded',
                'fill_price': fill_price,
                'fill_qty': fill_qty,
                'confirm_source': 'feed',
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
        # Confirm entry fill using order-feed first, polling fallback.
        if not kotak_order_id:
            return None

        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._entry_fill_waiters[str(kotak_order_id)] = waiter
        poll_task = asyncio.create_task(self._verify_fill(kotak_order_id, order))

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
                feed_event = feed_payload.get('event', {}) if isinstance(feed_payload, dict) else {}
                feed_result = self._entry_fill_from_feed(feed_event, order)
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
            current = self._entry_fill_waiters.get(str(kotak_order_id))
            if current is waiter:
                self._entry_fill_waiters.pop(str(kotak_order_id), None)
            if not poll_task.done():
                poll_task.cancel()
                with suppress(asyncio.CancelledError):
                    await poll_task

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
                retries=SL_PROTECT_RETRIES,
                delay_s=SL_PROTECT_RETRY_DELAY_S,
                exchange_segment="bse_fo",
                trading_symbol=pos["trading_symbol"],
                transaction_type="S",
                order_type="SL",
                quantity=pos["quantity"],
                price=SL_LIMIT_PRICE,
                trigger_price=trigger_price,
                validity="DAY",
            )

            sl_order_id = self._extract_order_id(result)
            if not sl_order_id:
                raise Exception("Kotak SL place_order response missing nOrdNo")

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
            if not sl_order_id:
                await self._broadcast("order_alert", {
                    "level": "error",
                    "message": f"🚨 SL trail re-protect failed for {pos['trading_symbol']} — forcing exit",
                })
                await self.close_position(
                    pos["id"],
                    exit_price=pos.get("current_price", pos.get("entry_price", 0)),
                    exit_reason="sl_protect_fail",
                )
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

        if not sl_order_id:
            log.error("Initial SL placement failed for %s — attempting emergency exit", position["trading_symbol"])
            await self._broadcast("order_alert", {
                "level": "error",
                "message": f"🚨 SL protection unavailable for {position['trading_symbol']} — attempting emergency exit",
            })
            emergency = await self.close_position(position_id, exit_price=fill_price, exit_reason="sl_protect_fail")
            if emergency.get("status") != "closed":
                await self._broadcast("order_alert", {
                    "level": "error",
                    "message": f"🚨 Emergency exit could not be confirmed for {position['trading_symbol']} — MANUAL ACTION REQUIRED",
                })

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
            if pos.get("status") != "open":
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
        # Phase 1: Under _expiry_lock — expiry checks + bounce detection (fast)
        orders_to_fill = []  # (order, ltp) pairs needing Kotak BUY
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

                # Bounce-back entry logic
                # [FIX #28] Only track min_ltp while LTP is inside entry range.
                in_range = order["entry_low"] <= ltp <= order["entry_high"]
                if in_range:
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
                # [FIX #28] Fill guard: bounce must land at or above entry_low
                if (order.get("min_ltp") is not None and
                        ltp >= order["min_ltp"] + bounce_points and
                        ltp >= order["entry_low"]):
                    # Bounce confirmed — mark for fill OUTSIDE the lock
                    orders_to_fill.append((order, ltp))

            for order in expired:
                if order in self._pending_orders:
                    self._pending_orders.remove(order)

        # Phase 2: Outside _expiry_lock — BUY + verify (slow, ~1s per order)
        # _order_lock still serializes concurrent BUY attempts.
        for order, fill_ltp in orders_to_fill:
            async with self._order_lock:
                entry_result = await self._place_entry_order(order, fill_ltp)
                if entry_result:
                    kotak_oid = entry_result.get("kotak_order_id", "")

                    # Confirm fill: order-feed first, order_history polling fallback
                    fill_data = await self._confirm_entry_fill(kotak_oid, order)

                    if fill_data and fill_data["status"] == "traded":
                        verified_price = fill_data["fill_price"]
                        verified_qty   = fill_data["fill_qty"]
                        result = await self._fill_order(
                            order, verified_price, filled_qty=verified_qty,
                        )
                        confirm_source = fill_data.get('confirm_source', 'poll')
                        log.info(
                            "Real order FILLED (verified): %s @ %.2f qty=%d (Min was %s) [confirmation_source=%s]",
                            order["trading_symbol"], verified_price,
                            verified_qty, order["min_ltp"], confirm_source,
                        )
                    else:
                        # Fill not confirmed — SAFETY: try to cancel on Kotak
                        # If cancel succeeds → order wasn't filled, safe to expire
                        # If cancel fails → order was filled, MUST track it
                        log.warning(
                            "BUY not confirmed for %s — attempting cancel on Kotak (order %s)",
                            order["trading_symbol"], kotak_oid,
                        )
                        cancelled = False
                        if kotak_oid and self.kotak and self.kotak.is_authenticated:
                            try:
                                cancel_result = self.kotak.cancel_order(order_id=kotak_oid)
                                log.info("Cancel result for %s: %s", kotak_oid, cancel_result)
                                cancel_err = self._extract_broker_error(cancel_result)
                                if cancel_err:
                                    log.warning("Cancel rejected for %s: %s", kotak_oid, cancel_err)
                                else:
                                    cancelled = True
                            except Exception:
                                log.exception("Cancel attempt failed for order %s", kotak_oid)

                        if cancelled:
                            # Successfully cancelled — safe to mark expired
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
                            # Could NOT cancel — order is likely FILLED on exchange
                            # Re-verify: wait longer and poll order_history again
                            log.warning(
                                "Cannot cancel order %s — re-verifying fill for %s",
                                kotak_oid, order["trading_symbol"],
                            )
                            await asyncio.sleep(2)  # Give Kotak API time to settle
                            fill_data_retry = await self._verify_fill(kotak_oid, order)

                            if fill_data_retry and fill_data_retry["status"] == "traded":
                                verified_price = fill_data_retry["fill_price"]
                                verified_qty   = fill_data_retry["fill_qty"]
                                result = await self._fill_order(
                                    order, verified_price, filled_qty=verified_qty,
                                )
                                log.info(
                                    "VERIFIED on retry: %s @ %.2f qty=%d — position tracked, SL placed [confirmation_source=poll_retry]",
                                    order["trading_symbol"], verified_price, verified_qty,
                                )
                                await self._broadcast("order_alert", {
                                    "level":   "success",
                                    "message": f"✅ Fill VERIFIED (retry): {order['trading_symbol']} @ ₹{verified_price:.2f}",
                                })
                            else:
                                # Still can't verify — alert user, do NOT abandon
                                log.error(
                                    "CRITICAL: Order %s for %s — cannot cancel AND cannot verify. "
                                    "CHECK KOTAK APP IMMEDIATELY.",
                                    kotak_oid, order["trading_symbol"],
                                )
                                await self._broadcast("order_alert", {
                                    "level":   "error",
                                    "message": f"🚨 UNVERIFIED ORDER: {order['trading_symbol']} (#{kotak_oid}) — CHECK KOTAK APP NOW",
                                })
                                # Mark as needs_review, NOT expired — so it stays visible
                                await self._broadcast("order_update", {
                                    "id":          order["trade_id"],
                                    "signal_id":   order.get("signal_id"),
                                    "status_note": f"🚨 Unverified — check Kotak order #{kotak_oid}",
                                })

                # Remove from pending list (re-acquire lock briefly)
                async with self._expiry_lock:
                    if order in self._pending_orders:
                        self._pending_orders.remove(order)


    async def _verify_exit_fill(self, kotak_order_id: str, pos: dict) -> Optional[dict]:
        """Verify IOC SELL fill before marking a position closed."""
        if not kotak_order_id or not self.kotak or not self.kotak.is_authenticated:
            return None

        for _ in range(1, FILL_VERIFY_POLLS + 1):
            await asyncio.sleep(FILL_VERIFY_INTERVAL_S)
            try:
                hist = self.kotak.order_history(order_id=kotak_order_id)
                if not hist or not isinstance(hist, dict):
                    continue

                hist_err = self._extract_broker_error(hist)
                if hist_err:
                    continue

                rows = self._extract_order_rows(hist)
                if not rows:
                    continue

                matching = [r for r in rows if str(r.get('nOrdNo', '')).strip() == str(kotak_order_id)]
                latest = matching[0] if matching else rows[0]
                status = str(latest.get('ordSt', '')).lower().strip()

                if status in ('traded', 'complete', 'completed'):
                    fill_price = float(latest.get('avgPrc', 0) or latest.get('flPrc', 0) or latest.get('prc', 0) or pos.get('current_price', 0) or 0)
                    fill_qty = int(latest.get('fldQty', 0) or latest.get('flQty', 0) or latest.get('qty', 0) or pos.get('quantity', 0) or 0)
                    if fill_qty <= 0:
                        fill_qty = int(pos.get('quantity', 0) or 0)
                    return {
                        'status': 'traded',
                        'fill_price': fill_price,
                        'fill_qty': fill_qty,
                    }

                if status in ('rejected', 'cancelled'):
                    return None

            except Exception:
                log.exception('_verify_exit_fill failed for %s', kotak_order_id)

        return None

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
        pos = None
        async with self._close_lock:
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

            pos["status"] = "closing"
            if exit_reason:
                pos["exit_reason"] = exit_reason

        price = exit_price or pos.get("current_price", pos["entry_price"])
        pnl = (price - pos["entry_price"]) * pos["quantity"]
        exchange_exit_confirmed = (exit_reason == "sl")
        db_sync_ok = False

        try:
            # 1. Cancel exchange SL order (skip if SL already triggered — it's already filled)
            sl_order_id = pos.get("sl_order_id")
            if sl_order_id and exit_reason != "sl":
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
            if exit_reason in ("timer", "kill", "eod", "manual", "sl_protect_fail"):
                try:
                    sell_result = await self._kotak_call_with_retry(
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
                    sell_order_id = self._extract_order_id(sell_result)
                    if not sell_order_id:
                        raise Exception("SELL place_order response missing nOrdNo")

                    sell_fill = await self._verify_exit_fill(sell_order_id, pos)
                    if not sell_fill or sell_fill.get("status") != "traded":
                        await self._broadcast("order_alert", {
                            "level":   "error",
                            "message": f"❌ EXIT SELL UNVERIFIED for {pos['trading_symbol']} (#{sell_order_id}) — MANUAL EXIT REQUIRED",
                        })
                        await self._broadcast("order_update", {
                            "id":          pos.get("trade_id"),
                            "status_note": f"🚨 Exit unverified — check Kotak order #{sell_order_id}",
                        })
                        return {"status": "error", "message": "Exit SELL unverified", "order_id": sell_order_id}

                    exchange_exit_confirmed = True
                    price = float(sell_fill.get("fill_price", price) or price)
                    pnl = (price - pos["entry_price"]) * pos["quantity"]
                    await self._broadcast("order_update", {
                        "id":          pos.get("trade_id"),
                        "status_note": f"✅ SELL Filled @ {price:.2f} ({exit_reason})",
                    })
                except Exception:
                    log.exception("Failed to place/verify exit SELL for %s", pos["trading_symbol"])
                    await self._broadcast("order_alert", {
                        "level":   "error",
                        "message": f"❌ EXIT SELL FAILED for {pos['trading_symbol']} — MANUAL EXIT REQUIRED",
                    })
                    return {"status": "error", "message": "Exit SELL failed"}

            # 3. Update DB (only after exchange exit is confirmed)
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

            # 4. Only now mark in-memory closed and remove
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
                pos["status"] = "open"

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
            if pos.get("status") != "open":
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

        # Resolve entry-fill waiter from terminal order-feed events.
        if status in ('traded', 'complete', 'completed', 'rejected', 'cancelled'):
            self._resolve_entry_fill_waiter(order_id, {'status': status, 'event': data})

        # Check if this is an SL order that got triggered (traded)
        if status in ("traded", "complete", "completed"):
            for pos in list(self._open_positions):
                if pos.get("sl_order_id") == order_id and pos.get("status") == "open":
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
                if pos.get("sl_order_id") == order_id and pos.get("status") == "open":
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
                    else:
                        await self._broadcast("order_alert", {
                            "level": "error",
                            "message": f"🚨 SL re-protect failed for {pos['trading_symbol']} — forcing exit",
                        })
                        await self.close_position(
                            pos["id"],
                            exit_price=pos.get("current_price", pos.get("entry_price", 0)),
                            exit_reason="sl_protect_fail",
                        )
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

            orders = self._extract_order_rows(order_book)
            if not orders and isinstance(order_book, dict):
                raw_orders = order_book.get("data", [])
                if isinstance(raw_orders, list):
                    orders = raw_orders
            if not isinstance(orders, list):
                return

            active_order_ids = {
                str(o.get("nOrdNo", ""))
                for o in orders
                if str(o.get("ordSt", "")).lower() in (
                    "pending", "trigger pending", "open", "open pending", "validation pending"
                )
            }

            for pos in list(self._open_positions):
                if pos.get("status") != "open":
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
                    else:
                        await self._broadcast("order_alert", {
                            "level": "error",
                            "message": f"🚨 SL reconcile re-protect failed for {pos['trading_symbol']} — forcing exit",
                        })
                        await self.close_position(
                            pos["id"],
                            exit_price=pos.get("current_price", pos.get("entry_price", 0)),
                            exit_reason="sl_protect_fail",
                        )

        except Exception:
            log.exception("reconcile_orders failed")

    # ── Cancel a single pending order ─────────────────────────────────────────

    async def cancel_pending_order(self, trade_id: int) -> dict:
        """Cancel a single pending entry order by trade_id.

        Acquires _expiry_lock — same lock held by on_tick() during the entire
        pending-order loop (including _place_entry_order + _verify_fill).
        This guarantees no Kotak BUY can be placed for this order while we
        are cancelling it.
        """
        async with self._expiry_lock:
            target = None
            for order in self._pending_orders:
                if order.get("trade_id") == trade_id:
                    target = order
                    break

            if target is None:
                return {"status": "error", "message": "Pending order not found — may already be filled or expired"}

            # Remove from in-memory list while holding the lock
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

        log.info("Pending order cancelled (real): trade_id=%d symbol=%s", trade_id, target.get("trading_symbol"))
        return {"status": "ok", "trade_id": trade_id, "trading_symbol": target.get("trading_symbol")}

    # ── Kill Switch ───────────────────────────────────────────────────────────

    async def square_off_all(self, exit_reason: str = "kill") -> dict:
        results = []
        for pos in list(self._open_positions):
            if pos.get("status") != "open":
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
                "created_at":        t.get("created_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "min_ltp":           t.get("min_ltp"),
                "sl_mode":           "signal_trail",
                "signal_stoploss":   None,
                "activation_points": DEFAULT_ACTIVATION_PTS,
                "trail_gap":         DEFAULT_TRAIL_GAP,
                "bounce_points":     DEFAULT_BOUNCE_POINTS,
                "entry_logic":       "code",
                "entry_label":       t.get("entry_label"),
                "entry_timer_mins":  int(t.get("entry_timer_mins") or ENTRY_TIMEOUT_MINS),
                "exit_timer_mins":   int(t.get("exit_timer_mins") or POSITION_TIMEOUT_MINS),
                "entry_slippage":    float(t.get("entry_slippage") or DEFAULT_ENTRY_SLIPPAGE),
                "exit_slippage":     float(t.get("exit_slippage") or DEFAULT_EXIT_SLIPPAGE),
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
                "exit_timer_mins":   int(p.get("exit_timer_mins") or POSITION_TIMEOUT_MINS),
                "exit_slippage":     float(p.get("exit_slippage") or DEFAULT_EXIT_SLIPPAGE),
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
