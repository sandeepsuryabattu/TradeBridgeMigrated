"""
Market Feed — Subscribes to Kotak Neo websocket for real-time tick data.
Provides live LTP to paper and real trading engines.
Stores every tick for backtesting.

PATCHES APPLIED:
 [1] Singleton _loop captured via get_running_loop() at start() time
 [2] _flush_tick_buffer uses stored _loop instead of deprecated get_event_loop()
 [3] REMOVED — our reconnect loop ripped out. SDK's run_forever(reconnect=5) handles reconnect.
 [4] stop() method for clean shutdown / pre-refresh teardown
 [5] REMOVED — no reconnect loop to backoff
 [6] REMOVED — no reconnect loop to lock
 [7] REMOVED — no reconnect loop to guard
 [8] Pending subs use a set-merge so reconnects never lose subscriptions
 [9] REMOVED — no reconnect loop waiting for open event
[10] Tick buffer flushed on disconnect so no ticks are lost
[11] Heartbeat watchdog — detects silent dead feed. On stale: force-closes WS so
     SDK's own reconnect=5 fires a fresh connection.
[12] SDK owns reconnect. _on_close is now OBSERVATION ONLY — no thread spawning.
[FIX #11] _heartbeat_watchdog uses ZoneInfo("Asia/Kolkata") — no hardcoded UTC offset
[FIX #19] _flush_pending_subs_when_ready: on timeout re-queues subs instead of discarding
[FIX #23] log.exception() used throughout — no bare except or log.error for exceptions
[FIX #26] _heartbeat_watchdog: weekend + NSE holiday awareness.
          _on_open sets _last_tick_time = time.time() which causes the watchdog to fire
          147s later on days with no market ticks (Sundays, holidays). Fix: check
          weekday >= 5 (Sat/Sun) or cached NSE holiday list before killing the feed.
          Holiday list fetched once per day and cached in _nse_holidays_cache.
"""
import asyncio
import logging
import threading
import time
import urllib.request
import json
from typing import Callable
from datetime import datetime, timezone, time as dt_time, date as dt_date
from zoneinfo import ZoneInfo

from . import database as db

log = logging.getLogger(__name__)

TICK_BUFFER_SIZE         = 50    # Flush to DB every N ticks
HEARTBEAT_INTERVAL       = 30    # Seconds between watchdog checks
HEARTBEAT_STALE_THRESHOLD = 120  # Seconds without a tick = dead feed

# [FIX #11] Market hours in IST — no hardcoded UTC offset
_IST           = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN   = dt_time(9,  0)   # 09:00 IST
_MARKET_CLOSE  = dt_time(15, 36)  # 15:36 IST (slight buffer after 15:30 close)


class MarketFeed:
    """Manages Kotak Neo websocket subscriptions for live market data."""

    def __init__(self, kotak_trader=None):
        self.kotak                = kotak_trader
        self._subscriptions:       dict[str, dict]  = {}
        self._tick_callbacks:      list[Callable]   = []
        self._raw_tick_callbacks:  list[Callable]   = []
        self._order_callbacks:     list[Callable]   = []
        self._running              = False
        self._tick_buffer:         list[dict]       = []
        self._loop:                asyncio.AbstractEventLoop | None = None
        self._pending_subs:        list[dict]       = []
        self._last_tick_time:      float            = 0.0
        self._heartbeat_thread:    threading.Thread | None = None
        self._started_once         = False

        # [FIX #26] NSE holiday cache — fetched once per day
        self._nse_holidays_cache:  set[str]         = set()   # "YYYY-MM-DD" strings
        self._nse_holidays_fetched_date: dt_date | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def add_tick_callback(self, callback: Callable):
        if callback not in self._tick_callbacks:
            self._tick_callbacks.append(callback)

    def remove_tick_callback(self, callback: Callable):
        if callback in self._tick_callbacks:
            self._tick_callbacks.remove(callback)

    def add_raw_tick_callback(self, callback: Callable):
        if callback not in self._raw_tick_callbacks:
            self._raw_tick_callbacks.append(callback)

    def add_order_callback(self, callback: Callable):
        if callback not in self._order_callbacks:
            self._order_callbacks.append(callback)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        """Set up websocket callbacks with Kotak Neo. Call ONCE after login.
        The SDK's run_forever(reconnect=5) handles all subsequent reconnects —
        do NOT call start() again on disconnect.
        """
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                pass

        if not self.kotak or not self.kotak.is_authenticated:
            log.warning("Cannot start market feed — Kotak not authenticated")
            return False

        if self._started_once:
            log.warning("start() called more than once — ignoring. SDK handles reconnect internally.")
            return False
        self._started_once = True

        try:
            self.kotak.setup_callbacks(
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open,
            )

            # Also subscribe order-feed so SL triggers/rejections are handled live.
            of_result = self.kotak.subscribe_order_feed()
            if isinstance(of_result, dict) and of_result.get("status") != "ok":
                log.warning("Order-feed subscribe issue: %s", of_result.get("message"))

            self._running = True
            log.info("Market feed: callbacks registered, SDK will maintain connection")

            if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_watchdog, daemon=True,
                )
                self._heartbeat_thread.start()
                log.info("Heartbeat watchdog started")

            return True
        except Exception:
            log.exception("Failed to start market feed")  # [FIX #23]
            return False

    def stop(self):
        """[4] Intentionally stop the market feed (no reconnect)."""
        self._running = False
        self._flush_tick_buffer()
        log.info("Market feed stopped intentionally")

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe_instrument(self, token: str, symbol: str, exchange_segment: str = "bse_fo"):
        token_str = str(token)
        if token_str not in self._subscriptions:
            self._subscriptions[token_str] = {
                "symbol":           symbol,
                "ltp":              0,
                "last_update":      None,
                "exchange_segment": exchange_segment,
            }
        if self.kotak and self.kotak.is_authenticated:
            sub_item = {"instrument_token": token_str, "exchange_segment": exchange_segment}
            if self._running:
                try:
                    self.kotak.subscribe(instrument_tokens=[sub_item])
                    log.info("Subscribed to %s (%s) on %s", symbol, token_str, exchange_segment)
                except Exception:
                    log.exception("Failed to subscribe to %s", symbol)  # [FIX #23]
            else:
                if sub_item not in self._pending_subs:
                    self._pending_subs.append(sub_item)
                log.info("Queued subscription for %s (%s) — WS not yet open", symbol, token_str)

    def subscribe_index(self, token: str, symbol: str):
        self.subscribe_instrument(token, symbol, exchange_segment="bse_cm")

    def subscribe_batch(self, tokens: list[dict]):
        if not self.kotak or not self.kotak.is_authenticated:
            return
        for item in tokens:
            tk = str(item["instrument_token"])
            self._subscriptions[tk] = {
                "symbol":           item.get("symbol", ""),
                "ltp":              0,
                "last_update":      None,
                "exchange_segment": item["exchange_segment"],
            }
        sub_list = [
            {
                "instrument_token": str(t["instrument_token"]),
                "exchange_segment": t["exchange_segment"],
            }
            for t in tokens
        ]
        if self._running:
            try:
                self.kotak.subscribe(instrument_tokens=sub_list)
                log.info("Batch-subscribed to %d instruments", len(sub_list))
            except Exception:
                log.exception("Batch subscribe failed")  # [FIX #23]
        else:
            existing = {(s["instrument_token"], s["exchange_segment"]) for s in self._pending_subs}
            for s in sub_list:
                if (s["instrument_token"], s["exchange_segment"]) not in existing:
                    self._pending_subs.append(s)
            log.info("Queued %d subscriptions — WS not yet open", len(sub_list))

    def unsubscribe_instrument(self, token: str):
        token_str = str(token)
        if token_str in self._subscriptions:
            seg = self._subscriptions[token_str].get("exchange_segment", "bse_fo")
            del self._subscriptions[token_str]
            if self.kotak:
                try:
                    self.kotak.unsubscribe([{"instrument_token": token_str, "exchange_segment": seg}])
                except Exception:
                    log.exception("Unsubscribe failed for token %s", token_str)  # [FIX #23]

    # ── Data Access ───────────────────────────────────────────────────────────

    def get_ltp(self, token: str) -> float:
        return self._subscriptions.get(str(token), {}).get("ltp", 0)

    def get_all_ticks(self) -> dict:
        return dict(self._subscriptions)

    # ── Kotak SDK Callbacks ───────────────────────────────────────────────────

    def _dispatch_order_event(self, payload: dict):
        if not isinstance(payload, dict):
            return
        if not self._order_callbacks:
            return
        for cb in self._order_callbacks:
            if self._loop and not self._loop.is_closed():
                asyncio.run_coroutine_threadsafe(cb({"data": payload}), self._loop)

    def _on_message(self, message):
        try:
            if isinstance(message, list):
                for tick in message:
                    self._process_tick(tick)
                return

            if not isinstance(message, dict):
                return

            # Order-feed payloads: either top-level {nOrdNo, ordSt,...}
            # or nested under {"data": {...}}.
            data = message.get("data")
            if isinstance(data, dict) and (data.get("nOrdNo") or data.get("ordSt")):
                self._dispatch_order_event(data)
                return
            if message.get("nOrdNo") or message.get("ordSt"):
                self._dispatch_order_event(message)
                return

            if isinstance(data, list):
                for tick in data:
                    self._process_tick(tick)
            else:
                self._process_tick(message)
        except Exception:
            log.exception("Error processing tick message")  # [FIX #23]

    def _on_error(self, error):
        log.error("Market feed WS error: %s", error)

    def _on_close(self, message):
        """[12] WS closed — observation only. SDK's run_forever(reconnect=5) will reconnect.
        Flush the tick buffer and update state. No thread spawning here.
        """
        log.warning("WS closed: %s — SDK will auto-reconnect in ~5s", message)
        self._running = False
        self._flush_tick_buffer()  # [10] Don't lose buffered ticks

    def _on_open(self, message):
        """WS opened (initial or SDK auto-reconnect) — update state and flush pending subs.

        CRITICAL: Do NOT call kotak.subscribe() directly here.
        The SDK's NeoWebSocket.on_hsm_message handles the 'cn' handshake and then
        calls subscribe_scripts() automatically for any items already in sub_list.
        Calling subscribe() here before is_hsw_open==1 risks spawning a second WS
        thread that overwrites the global ws reference.

        Instead: merge known subscriptions into _pending_subs and delegate to
        _flush_pending_subs_when_ready() which polls until is_hsw_open==1.
        """
        log.info("Market feed WS opened: %s", message)
        self._running = True
        self._last_tick_time = time.time()

        known_subs   = [
            {"instrument_token": tk, "exchange_segment": info.get("exchange_segment", "bse_fo")}
            for tk, info in self._subscriptions.items()
        ]
        existing_keys = {(s["instrument_token"], s["exchange_segment"]) for s in self._pending_subs}
        for s in known_subs:
            if (s["instrument_token"], s["exchange_segment"]) not in existing_keys:
                self._pending_subs.append(s)

        if self._pending_subs:
            thread = threading.Thread(target=self._flush_pending_subs_when_ready, daemon=True)
            thread.start()

    def _flush_pending_subs_when_ready(self):
        """Poll until NeoWebSocket.is_hsw_open==1, then send pending subscriptions.

        [FIX #19] On timeout: re-queue subs_to_flush back into _pending_subs so they
        are retried on the next _on_open instead of being silently discarded.
        Previously a timeout meant those instruments would never receive ticks — no
        retry, no alert, no re-queue. Now they are preserved for the next reconnect.
        """
        deadline = time.time() + 15  # wait up to 15 s for SDK handshake
        while time.time() < deadline:
            try:
                neo_ws = self.kotak.client.NeoWebSocket
                if neo_ws and neo_ws.is_hsw_open == 1:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            # [FIX #19] Timeout — re-queue pending subs instead of discarding
            subs_to_retry = list(self._pending_subs)
            # Don't clear _pending_subs — they stay for the next _on_open call
            log.warning(
                "NeoWebSocket did not reach is_hsw_open=1 within 15s — "
                "%d subscription(s) retained for next reconnect (not discarded)",
                len(subs_to_retry),
            )
            return

        if not self._pending_subs or not self.kotak:
            return

        subs_to_flush = list(self._pending_subs)
        self._pending_subs.clear()
        log.info("Flushing %d subscriptions (is_hsw_open=1 confirmed)...", len(subs_to_flush))
        try:
            self.kotak.subscribe(instrument_tokens=subs_to_flush)
            log.info("Flushed %d subscriptions successfully", len(subs_to_flush))
        except Exception:
            # [FIX #19] Also re-queue on subscribe() failure
            log.exception("Failed to flush subscriptions — re-queuing for next reconnect")  # [FIX #23]
            existing_keys = {(s["instrument_token"], s["exchange_segment"]) for s in self._pending_subs}
            for s in subs_to_flush:
                if (s["instrument_token"], s["exchange_segment"]) not in existing_keys:
                    self._pending_subs.append(s)

    # ── Holiday helpers [FIX #26] ─────────────────────────────────────────────

    def _is_market_holiday(self, today: dt_date) -> bool:
        """Return True if today is a weekend or an NSE trading holiday.

        Weekends (Sat=5, Sun=6) are checked locally — no network call needed.
        NSE holidays are fetched from Upstox once per calendar day and cached
        in _nse_holidays_cache so the watchdog thread never blocks on repeat calls.
        """
        # Weekends — Python weekday(): Mon=0 … Sat=5, Sun=6
        if today.weekday() >= 5:
            return True

        # Refresh holiday cache once per day
        if self._nse_holidays_fetched_date != today:
            self._fetch_nse_holidays()
            self._nse_holidays_fetched_date = today

        return today.isoformat() in self._nse_holidays_cache

    def _fetch_nse_holidays(self):
        """Fetch NSE trading holidays from Upstox (no auth required) and
        populate _nse_holidays_cache with 'YYYY-MM-DD' strings.
        Silently swallows any network/parse errors — on failure the cache
        stays empty and only weekend detection remains active.
        """
        url = "https://api.upstox.com/v2/market/holidays"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            if data.get("status") == "success":
                self._nse_holidays_cache = {
                    h["date"]
                    for h in data["data"]
                    if h.get("holiday_type") == "TRADING_HOLIDAY"
                    and "NSE" in h.get("closed_exchanges", [])
                }
                log.info(
                    "NSE holiday cache refreshed — %d holidays loaded",
                    len(self._nse_holidays_cache),
                )
            else:
                log.warning("Upstox holiday API returned non-success status: %s", data.get("status"))
        except Exception:
            log.exception("Could not fetch NSE holidays — weekend check still active")  # [FIX #23]

    # ── Heartbeat Watchdog ────────────────────────────────────────────────────

    def _heartbeat_watchdog(self):
        """[11][FIX #11][FIX #26] Periodically checks that ticks are still arriving.
        If feed goes silent for HEARTBEAT_STALE_THRESHOLD seconds during market hours,
        closes the WS so the SDK's own reconnect=5 kicks in fresh.

        [FIX #11] Market hours now use ZoneInfo("Asia/Kolkata") instead of a hardcoded
        UTC offset.

        [FIX #26] Weekend + holiday guard added BEFORE the stale-tick check.
        Root cause: _on_open sets _last_tick_time = time.time() so the watchdog
        always sees "N seconds since last tick" even on days with zero market
        activity (Sundays, public holidays). The old code only checked clock time
        against _MARKET_OPEN/_MARKET_CLOSE — which is within range on a Sunday
        morning — so it would fire and kill the WS every ~147s all day.
        Fix: skip the stale check entirely on weekends/holidays.
        """
        log.info("Heartbeat watchdog running")
        while True:
            time.sleep(HEARTBEAT_INTERVAL)

            if not self._running:
                continue  # SDK will reconnect; _on_open will set _running=True

            if self._last_tick_time == 0:
                continue  # No ticks received yet since startup

            # [FIX #26] Skip on weekends and NSE holidays — no ticks expected
            today_ist = datetime.now(_IST).date()
            if self._is_market_holiday(today_ist):
                log.debug(
                    "Heartbeat watchdog: market closed today (%s) — skipping stale check",
                    today_ist.isoformat(),
                )
                # Reset _last_tick_time so we don't accumulate stale seconds
                # across the holiday into tomorrow's first check
                self._last_tick_time = time.time()
                continue

            # [FIX #11] Use explicit IST timezone — never rely on server's local clock
            now_ist = datetime.now(_IST).time()
            if not (_MARKET_OPEN <= now_ist <= _MARKET_CLOSE):
                continue  # Outside market hours — stale feed is expected

            elapsed = time.time() - self._last_tick_time
            if elapsed > HEARTBEAT_STALE_THRESHOLD:
                log.warning(
                    "No ticks for %.0fs — feed appears dead. "
                    "Closing WS so SDK reconnect fires...",
                    elapsed,
                )
                self._running = False
                self._flush_tick_buffer()
                try:
                    from neo_api_client.HSWebSocketLib import ws as sdk_ws
                    if sdk_ws:
                        sdk_ws.close()
                        log.info("Forced WS close — SDK will reconnect in ~5s")
                except Exception:
                    log.exception("Could not force-close SDK WS")  # [FIX #23]
                # Reset timestamp so watchdog doesn't fire again immediately
                self._last_tick_time = time.time()

    # ── Tick Processing ───────────────────────────────────────────────────────

    def _process_tick(self, tick: dict):
        token   = str(tick.get("tk") or tick.get("instrument_token", ""))
        ltp_val = tick.get("ltp", tick.get("last_traded_price"))
        ltp     = float(ltp_val) if ltp_val is not None else 0

        if not token or token not in self._subscriptions:
            return

        if ltp > 0:
            self._subscriptions[token]["ltp"]    = ltp
            self._last_tick_time                 = time.time()

        self._subscriptions[token]["last_update"] = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

        self._tick_buffer.append({
            "instrument_token": token,
            "symbol":    self._subscriptions[token].get("symbol", ""),
            "ltp":       ltp,
            "volume":    tick.get("v",  tick.get("volume", 0)),
            "open":      tick.get("o",  tick.get("open",   0)),
            "high":      tick.get("h",  tick.get("high",   0)),
            "low":       tick.get("l",  tick.get("low",    0)),
            "close":     tick.get("c",  tick.get("close",  0)),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        if len(self._tick_buffer) >= TICK_BUFFER_SIZE:
            self._flush_tick_buffer()

        tick["symbol"] = self._subscriptions[token].get("symbol", "")

        if ltp > 0:
            for cb in self._raw_tick_callbacks:
                try:
                    cb(token, ltp, tick)
                except Exception:
                    log.exception("Raw tick callback error")  # [FIX #23]

        if ltp > 0:
            for cb in self._tick_callbacks:
                if self._loop and not self._loop.is_closed():
                    asyncio.run_coroutine_threadsafe(cb(token, ltp, tick), self._loop)

    def _flush_tick_buffer(self):
        """[2][10] Flush buffered ticks to database."""
        if not self._tick_buffer:
            return
        ticks_to_save = list(self._tick_buffer)
        self._tick_buffer.clear()
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(db.save_ticks_batch(ticks_to_save), self._loop)
        else:
            log.warning("Cannot flush %d ticks — event loop unavailable", len(ticks_to_save))