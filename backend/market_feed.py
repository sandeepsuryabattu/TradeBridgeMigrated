"""
Market Feed — Subscribes to Kotak Neo websocket for real-time tick data.
Provides live LTP to paper and real trading engines.
Stores every tick for backtesting.

MIGRATION:
Uses the modern async SFeedWebSocket and OrderFeedWebSocket introduced in kotakneoapi v2.2.0.
Legacy callback methods (start() with setup_callbacks) have been replaced by async tasks.
"""
import asyncio
import logging
import urllib.request
import json
from typing import Callable
from datetime import datetime, timezone, time as dt_time, date as dt_date
from zoneinfo import ZoneInfo
import time

try:
    from neo_api_client.websocket.feed import WsToken, SFeedScrip
    from neo_api_client.websocket.orderfeed import OrderUpdate, PositionUpdate
except ImportError:
    pass

from . import database as db

log = logging.getLogger(__name__)

TICK_BUFFER_SIZE          = 50
HEARTBEAT_INTERVAL        = 30
HEARTBEAT_STALE_THRESHOLD = 60
RECONNECT_WAIT_S          = 15
MAX_RECONNECT_ATTEMPTS    = 5

_IST           = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN   = dt_time(9,  0)
_MARKET_CLOSE  = dt_time(15, 36)


class MarketFeed:
    """Manages Kotak Neo async websocket subscriptions for live market and order data."""

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
        self._started_once         = False
        self._session_expired       = False

        self._reconnect_callback:  Callable | None  = None
        self._last_close_time:     float            = 0.0
        self._reconnect_attempts:  int              = 0
        self._needs_relogin:       bool             = False

        self._nse_holidays_cache:  set[str]         = set()
        self._nse_holidays_fetched_date: dt_date | None = None
        
        # Async Tasks
        self._sfeed_task: asyncio.Task | None = None
        self._orderfeed_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._sfeed_ws = None

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

    def set_reconnect_callback(self, callback: Callable):
        self._reconnect_callback = callback

    def add_order_callback(self, callback: Callable):
        if callback not in self._order_callbacks:
            self._order_callbacks.append(callback)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        """Set up async websocket tasks with Kotak Neo. Call ONCE after login."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        if not self.kotak or not self.kotak.is_authenticated:
            log.warning("Cannot start market feed — Kotak not authenticated")
            return False

        if self._started_once:
            log.warning("start() called more than once — ignoring.")
            return False
        
        self._started_once = True
        self._session_expired = False
        self._running = True
        
        self._sfeed_task = self._loop.create_task(self._run_sfeed())
        self._orderfeed_task = self._loop.create_task(self._run_orderfeed())
        self._watchdog_task = self._loop.create_task(self._heartbeat_watchdog())
        
        log.info("Market feed: Async tasks started")
        return True

    def stop(self):
        """Intentionally stop the market feed tasks."""
        self._running = False
        self._flush_tick_buffer()
        
        if self._sfeed_task:
            self._sfeed_task.cancel()
        if self._orderfeed_task:
            self._orderfeed_task.cancel()
        if self._watchdog_task:
            self._watchdog_task.cancel()

        self._tick_callbacks.clear()
        self._raw_tick_callbacks.clear()
        self._order_callbacks.clear()

        log.info("Market feed stopped intentionally")

    # ── WebSocket Tasks ───────────────────────────────────────────────────────

    async def _run_sfeed(self):
        """Main loop for SFeed WebSocket."""
        while self._running:
            if self._session_expired:
                await asyncio.sleep(5)
                continue
            
            try:
                async with self.kotak.client.create_websocket() as ws:
                    self._sfeed_ws = ws
                    self._last_close_time = 0.0
                    self._reconnect_attempts = 0
                    
                    # Flush pending subscriptions
                    tokens_to_sub = []
                    for s in self._pending_subs:
                        tokens_to_sub.append(WsToken(s["exchange_segment"], str(s["instrument_token"]).strip()))
                    self._pending_subs.clear()
                    
                    # Plus known active subs
                    for tk, info in self._subscriptions.items():
                        tokens_to_sub.append(WsToken(info.get("exchange_segment", "bse_fo"), str(tk).strip()))
                        
                    # Deduplicate tokens
                    tokens_to_sub = list(set(tokens_to_sub))
                        
                    if tokens_to_sub:
                        await ws.subscribe_scrips(tokens_to_sub)
                        log.info(f"Subscribed to {len(tokens_to_sub)} tokens on SFeed connect")
                    
                    async for message in ws:
                        if not self._running:
                            break
                        
                        # Duck typing to handle both SFeedScrip and SFeedIndex, or unexpected types
                        if hasattr(message, "instrument_token") and hasattr(message, "last_traded_price"):
                            tick = {
                                "tk": str(getattr(message, "instrument_token", "")),
                                "ltp": float(getattr(message, "last_traded_price", 0) or 0),
                                "v": int(getattr(message, "volume_traded_today", 0) or 0),
                                "o": float(getattr(message, "open_price", 0) or 0),
                                "h": float(getattr(message, "high_price", 0) or 0),
                                "l": float(getattr(message, "low_price", 0) or 0),
                                "c": float(getattr(message, "close_price", 0) or 0),
                            }
                            self._process_tick(tick)
                        else:
                            log.debug(f"SFeed unhandled message type: {type(message)} -> {message}")
                            
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    log.error(f"SFeed websocket exception: {e}")
                    self._last_close_time = time.time()
                await asyncio.sleep(5)
        self._sfeed_ws = None

    async def _run_orderfeed(self):
        """Main loop for OrderFeed WebSocket."""
        while self._running:
            if self._session_expired:
                await asyncio.sleep(5)
                continue
                
            try:
                async with self.kotak.client.create_order_feed() as ws:
                    log.info("OrderFeed websocket connected")
                    async for message in ws:
                        if not self._running:
                            break
                        
                        if isinstance(message, OrderUpdate):
                            # Ensure we output in the dict format the legacy code expects
                            try:
                                data = message.model_dump().get("data", {})
                            except AttributeError:
                                data = message.data.model_dump() if hasattr(message.data, "model_dump") else getattr(message, "data", {})
                                
                            if "order_id" in data and "nOrdNo" not in data:
                                data["nOrdNo"] = data["order_id"]
                            if "order_status" in data and "ordSt" not in data:
                                data["ordSt"] = data["order_status"]
                                
                            payload = {"data": data}
                            self._dispatch_order_event(payload)
                            
                        elif isinstance(message, PositionUpdate):
                            pass
                            
                        elif isinstance(message, dict):
                            self._dispatch_order_event(message)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    log.error(f"OrderFeed websocket exception: {e}")
                await asyncio.sleep(5)

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
        
        sub_item = {"instrument_token": token_str, "exchange_segment": exchange_segment}
        if self._running and self._sfeed_ws:
            tk_obj = WsToken(exchange_segment, token_str)
            if self._loop and not self._loop.is_closed():
                self._loop.create_task(self._sfeed_ws.subscribe_scrips([tk_obj]))
                log.info("Subscribed to %s (%s) on %s", symbol, token_str, exchange_segment)
        else:
            if sub_item not in self._pending_subs:
                self._pending_subs.append(sub_item)
            log.info("Queued subscription for %s (%s)", symbol, token_str)

    def subscribe_index(self, token: str, symbol: str):
        self.subscribe_instrument(token, symbol, exchange_segment="bse_cm")

    def subscribe_batch(self, tokens: list[dict]):
        for item in tokens:
            tk = str(item["instrument_token"])
            self._subscriptions[tk] = {
                "symbol":           item.get("symbol", ""),
                "ltp":              0,
                "last_update":      None,
                "exchange_segment": item["exchange_segment"],
            }
            
        if self._running and self._sfeed_ws:
            tk_objs = [WsToken(t["exchange_segment"], str(t["instrument_token"])) for t in tokens]
            if self._loop and not self._loop.is_closed():
                self._loop.create_task(self._sfeed_ws.subscribe_scrips(tk_objs))
                log.info("Batch-subscribed to %d instruments", len(tk_objs))
        else:
            for s in tokens:
                s_item = {"instrument_token": str(s["instrument_token"]), "exchange_segment": s["exchange_segment"]}
                if s_item not in self._pending_subs:
                    self._pending_subs.append(s_item)
            log.info("Queued %d subscriptions", len(tokens))

    def unsubscribe_instrument(self, token: str):
        token_str = str(token)
        if token_str in self._subscriptions:
            seg = self._subscriptions[token_str].get("exchange_segment", "bse_fo")
            del self._subscriptions[token_str]
            if self._running and self._sfeed_ws and self._loop and not self._loop.is_closed():
                tk_obj = WsToken(seg, token_str)
                self._loop.create_task(self._sfeed_ws.unsubscribe_scrips([tk_obj]))

    # ── Data Access ───────────────────────────────────────────────────────────

    def get_ltp(self, token: str) -> float:
        return self._subscriptions.get(str(token), {}).get("ltp", 0)

    def get_all_ticks(self) -> dict:
        return dict(self._subscriptions)

    # ── Events ────────────────────────────────────────────────────────────────

    def _dispatch_order_event(self, payload: dict):
        if not isinstance(payload, dict):
            return
        if not self._order_callbacks:
            return
        for cb in self._order_callbacks:
            if self._loop and not self._loop.is_closed():
                # payload format historically expected: {"data": {...}} or flat dict
                if "data" not in payload and ("nOrdNo" in payload or "ordSt" in payload):
                    payload = {"data": payload}
                # The callback might be async or sync depending on implementation
                # The real_trader handle_order_feed is usually sync but runs in thread, or async.
                # In main.py it uses asyncio.run_coroutine_threadsafe. Since we are in async task:
                if asyncio.iscoroutinefunction(cb):
                    self._loop.create_task(cb(payload))
                else:
                    try:
                        cb(payload)
                    except Exception as e:
                        log.error(f"Error in order callback: {e}")

    # ── Holiday helpers ───────────────────────────────────────────────────────

    def _is_market_holiday(self, today: dt_date) -> bool:
        if today.weekday() >= 5:
            return True
        if self._nse_holidays_fetched_date != today:
            self._fetch_nse_holidays()
            self._nse_holidays_fetched_date = today
        return today.isoformat() in self._nse_holidays_cache

    def _fetch_nse_holidays(self):
        url = "https://api.upstox.com/v2/market/holidays"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if data.get("status") == "success":
                self._nse_holidays_cache = {
                    h["date"] for h in data["data"]
                    if h.get("holiday_type") == "TRADING_HOLIDAY" and "NSE" in h.get("closed_exchanges", [])
                }
        except Exception:
            pass

    # ── Heartbeat Watchdog ────────────────────────────────────────────────────

    def _trigger_reconnect(self):
        if not self._reconnect_callback:
            return
        if not self._loop or self._loop.is_closed():
            return
        if self._reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
            log.error("Max reconnect attempts reached.")
            return

        self._reconnect_attempts += 1
        try:
            self._loop.create_task(self._reconnect_callback())
        except Exception:
            log.exception("Failed to schedule reconnect callback")

    async def _heartbeat_watchdog(self):
        """Async watchdog task."""
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)

            today_ist = datetime.now(_IST).date()
            if self._is_market_holiday(today_ist):
                self._last_tick_time = time.time()
                continue

            now_ist = datetime.now(_IST).time()

            if not (_MARKET_OPEN <= now_ist <= _MARKET_CLOSE):
                if not self._session_expired:
                    self._session_expired = True
                    self._flush_tick_buffer()
                    log.info("Session expired — WS shutdown until market opens")
                    # Restarting tasks will cause them to wait
                    if self._sfeed_task: self._sfeed_task.cancel()
                    if self._orderfeed_task: self._orderfeed_task.cancel()
                    self._sfeed_task = self._loop.create_task(self._run_sfeed())
                    self._orderfeed_task = self._loop.create_task(self._run_orderfeed())
                continue

            if self._session_expired:
                self._session_expired = False
                self._needs_relogin = True
                self._last_close_time = time.time()
                self._trigger_reconnect()
                continue

            if self._last_close_time > 0 and (time.time() - self._last_close_time) > RECONNECT_WAIT_S:
                if not self._sfeed_ws:
                    self._trigger_reconnect()
                    self._last_close_time = time.time()
                continue

            if self._last_tick_time == 0:
                continue

            elapsed = time.time() - self._last_tick_time
            if elapsed > HEARTBEAT_STALE_THRESHOLD:
                log.warning("No ticks for %.0fs — triggering full reconnect", elapsed)
                self._last_close_time = time.time()
                self._flush_tick_buffer()
                
                # Restart WS
                if self._sfeed_task: self._sfeed_task.cancel()
                if self._orderfeed_task: self._orderfeed_task.cancel()
                self._sfeed_task = self._loop.create_task(self._run_sfeed())
                self._orderfeed_task = self._loop.create_task(self._run_orderfeed())
                
                await asyncio.sleep(RECONNECT_WAIT_S)
                if not self._sfeed_ws:
                    self._trigger_reconnect()
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
                    pass

        if ltp > 0:
            for cb in self._tick_callbacks:
                if self._loop and not self._loop.is_closed():
                    if asyncio.iscoroutinefunction(cb):
                        self._loop.create_task(cb(token, ltp, tick))
                    else:
                        try:
                            cb(token, ltp, tick)
                        except Exception:
                            pass

    def _flush_tick_buffer(self):
        if not self._tick_buffer:
            return
        ticks_to_save = list(self._tick_buffer)
        self._tick_buffer.clear()
        if self._loop and not self._loop.is_closed():
            self._loop.create_task(db.save_ticks_batch(ticks_to_save))
