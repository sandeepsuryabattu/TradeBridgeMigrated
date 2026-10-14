"""
Main FastAPI application — REST API + WebSocket for the trading platform.

PATCHES APPLIED:
 [1] exit_position broadcasts position_update (status:'closed') instead of new_trade
 [2] Kotak login/init/download run in executor to avoid blocking the event loop
 [3] kill_switch init payload explicitly includes strategy via get_status()
 [4] Strategy persisted to JSON file so it survives server restarts
 [5] set_strategy broadcasts settings_update to all connected clients
 [6] clear_data clears in-memory paper trader state (positions + orders)
 [7] CORS allow_credentials removed when allow_origins='*' (invalid combo)
 [8] daily_contract_refresh stops market feed before restarting
 [9] WebSocket init payload explicitly passes strategy via get_status()
[10] kotak_auto_login typo fixed
[11] Strategy loaded from disk on startup so manager.strategy is never stale
[12] compareMode added to StrategyRequest; get_trades supports date filter
[FIX #1 ] exit_position: removed duplicate WS broadcast — close_position() is single source
[FIX #4 ] kotak_login and complete_2fa HTTP endpoints wrapped in run_in_executor
[FIX #5 ] enqueue_tick: QueueFull caught and logged; warning at 80% capacity
[FIX #6 ] daily_contract_refresh: cancels orphan tick drain task before restart, recreates after
[FIX #9 ] telegram_health_check: exponential backoff + circuit breaker after 3 failures
[FIX #18] WS broadcast: iterate copy, catch all exceptions per-connection, discard dead sockets
[FIX #23] log.exception() used throughout — no bare except or log.error for exceptions
[FIX #24] entryTimerMins, exitTimerMins, signalTrailInitialSL, signalTrailInitialSLPoints added to strategy
[FIX #25] Fallback actions: reconnect-market-feed, reconnect-telegram, resubscribe-signals,
          restart-backend (pm2), clear-signal-tracker
[FIX #31] Self-healing reconnect callback wired to market_feed so the heartbeat watchdog
          can trigger a full stop→start→resubscribe cycle without manual intervention.
[FIX #32] Mode persistence: trading mode (paper/real) is now saved to mode.json and
          restored on startup. Previously, every pm2 restart reset mode to 'paper',
          requiring manual re-activation of real trading each morning.
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Config
from .trade_manager import TradeManager
from .telegram_listener import TelegramListener
from . import database as db
from .order_tune import validate_patch, apply_patch, tune_to_json, summary as tune_summary

if not Config.API_TOKEN:
    _log_no_token = logging.getLogger(__name__)
    _log_no_token.warning(
        "API_TOKEN not set — mutating endpoints are unauthenticated. "
        "Set API_TOKEN in .env to protect /api/kill, /api/mode, /api/clear, etc."
    )


async def require_api_token(
    x_api_token: Optional[str] = Header(None, alias="X-API-Token"),
) -> None:
    """Enforce API_TOKEN on mutating endpoints when configured.

    Empty API_TOKEN = auth disabled (dev/test/back-compat). When set,
    callers must send `X-API-Token` header. Query-param auth was removed
    to avoid leaking tokens into server logs, proxies, and browser history.
    """
    if not Config.API_TOKEN:
        return
    if not x_api_token or x_api_token != Config.API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [pid=%(process)d] [%(name)s] %(levelname)s: %(message)s",
)
# Noisy third-party chatter (telethon sync diffs, etc.) goes to stderr,
# which PM2 routes to error.log — keep it at WARNING so the error log
# only holds real problems. Rotation is capped by pm2-logrotate anyway.
for _noisy in ("telethon", "telethon.client.updates"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# ── Strategy persistence ──────────────────────────────────────────────────────
STRATEGY_FILE = os.path.join(os.path.dirname(__file__), "..", "strategy.json")

STRATEGY_DEFAULTS = {
    "lots":                      1,
    "activationPoints":          5.0,
    "activationSLOffset":         0.0,
    "trailGap":                  2.0,
    "bouncePoints":              5,
    "bufferEnabled":             False,
    "bufferPoints":              2.0,
    "entryTimerMins":            10,
    "exitTimerMins":             10,
    "signalTrailInitialSL":      "telegram",
    "signalTrailInitialSLPoints": 5.0,
}


STRATEGY_SCOPES = ("default", "NIFTY")


def _normalize_strategy(saved: dict) -> dict:
    """Merge a single scope dict over STRATEGY_DEFAULTS."""
    if not isinstance(saved, dict):
        return dict(STRATEGY_DEFAULTS)
    return {**STRATEGY_DEFAULTS, **saved}


def load_strategies() -> dict:
    """Load per-underlying strategies, falling back to defaults.

    On-disk format is {"default": {...}, "NIFTY": {...}}.
    Legacy flat files ({"lots": ...}) are migrated: both scopes start
    as a copy of the old default so behaviour is unchanged until the
    user saves a NIFTY override.
    """
    try:
        if os.path.exists(STRATEGY_FILE):
            with open(STRATEGY_FILE) as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    if "default" in saved or "NIFTY" in saved:
                        default = _normalize_strategy(saved.get("default", {}))
                        nifty = _normalize_strategy(
                            saved.get("NIFTY", saved.get("default", {}))
                        )
                        return {"default": default, "NIFTY": nifty}
                    # Legacy flat file → same config for both scopes
                    default = _normalize_strategy(saved)
                    return {"default": dict(default), "NIFTY": dict(default)}
    except Exception:
        log.exception("Could not load strategy from disk")  # [FIX #23]
    return {"default": dict(STRATEGY_DEFAULTS), "NIFTY": dict(STRATEGY_DEFAULTS)}


def load_strategy() -> dict:
    """Backward-compat: return the default scope only."""
    return load_strategies()["default"]


def save_strategies(strategies: dict):
    """Persist per-scope strategies to disk."""
    try:
        with open(STRATEGY_FILE, "w") as f:
            json.dump(strategies, f, indent=2)
    except Exception:
        log.exception("Could not save strategy to disk")  # [FIX #23]


def save_strategy(strategy: dict):
    """Backward-compat: save flat dict as the default scope."""
    try:
        current = load_strategies()
        current["default"] = _normalize_strategy(strategy)
        save_strategies(current)
    except Exception:
        log.exception("Could not save strategy to disk")  # [FIX #23]


# ── Mode persistence ──────────────────────────────────────────────────────────
# [FIX #32] Trading mode (paper/real) was not persisted — every pm2 restart
# reset mode to Config.TRADING_MODE (default: 'paper'). This caused real
# trading to silently stop working until a manual mode switch each morning.
MODE_FILE = os.path.join(os.path.dirname(__file__), "..", "mode.json")


def load_mode() -> str:
    """Load trading mode from disk, falling back to Config default."""
    try:
        if os.path.exists(MODE_FILE):
            with open(MODE_FILE) as f:
                saved = json.load(f)
                mode = saved.get("mode", "")
                if mode in ("paper", "real"):
                    return mode
    except Exception:
        log.exception("Could not load mode from disk")
    return Config.TRADING_MODE


def save_mode(mode: str):
    """Persist trading mode to disk."""
    try:
        with open(MODE_FILE, "w") as f:
            json.dump({"mode": mode}, f, indent=2)
    except Exception:
        log.exception("Could not save mode to disk")


# ── Global instances ──────────────────────────────────────────────────────────
manager  = TradeManager()
telegram = TelegramListener()

# [4][11] Load persisted strategies immediately on import
manager.strategies = load_strategies()
manager.strategy = manager.strategies["default"]
# Sync lot_size from persisted default strategy so it survives restarts
if manager.strategy.get("lots"):
    manager.lot_size = max(1, int(manager.strategy["lots"]))

# [FIX #32] Load persisted mode so real trading survives restarts
_persisted_mode = load_mode()
if _persisted_mode != manager.mode:
    manager.set_mode(_persisted_mode)
    log.info("Restored persisted trading mode: %s", _persisted_mode)

# Stop-trading flag — when True, incoming signals are NOT traded.
# Existing positions and pending orders continue to be managed normally.
_stop_trading: bool = False

# ── Tick pipeline — module-level so route handlers can reference them ─────────
# [FIX #30] enqueue_tick / tick_consumer were closures inside lifespan() and
# therefore invisible to route handlers (e.g. reconnect_market_feed), causing
# NameError on every reconnect attempt. Promoted to module-level.
SENSEX_INDEX_TOKENS: set[str] = {"1", "999901", "50060"}
tick_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
_main_loop: Optional[asyncio.AbstractEventLoop] = None
_tick_drain_task: list[asyncio.Task] = []


# Throttled drop counter so a full tick queue logs one line per minute
# instead of one line (with full tick payloads) per dropped tick.
_tick_drop_count: int = 0
_tick_drop_logged_at: float = 0.0


def enqueue_tick(token, ltp, data):
    """Thread-safe: push a tick onto the async queue from a Kotak SDK thread.

    Drops (latest-wins) when full — LTP consumers only need the newest tick.
    Never logs payloads: at tick rates, per-drop logging is what filled the
    disk with multi-GB error logs.
    """
    global _main_loop, _tick_drop_count, _tick_drop_logged_at
    if _main_loop is None:
        return
    try:
        _main_loop.call_soon_threadsafe(tick_queue.put_nowait, (token, ltp, data))
        qsize = tick_queue.qsize()
        if qsize > 4000:
            log.warning(
                "Tick queue at %d/5000 (%.0f%%) — consider increasing maxsize",
                qsize, qsize / 5000 * 100,
            )
    except RuntimeError:
        pass  # Event loop closed during shutdown
    except asyncio.QueueFull:
        _tick_drop_count += 1
        now = time.monotonic()
        if now - _tick_drop_logged_at >= 60:
            log.warning(
                "Tick queue full — dropping ticks (%d dropped in last 60s). "
                "Increase maxsize or reduce DB write frequency.",
                _tick_drop_count,
            )
            _tick_drop_count = 0
            _tick_drop_logged_at = now


async def tick_consumer():
    """Drain tick_queue and forward LTP events to WebSocket clients."""
    while True:
        try:
            token, ltp, data = await asyncio.wait_for(tick_queue.get(), timeout=1.0)
            symbol = data.get("symbol", "")
            if token in SENSEX_INDEX_TOKENS or symbol in ("SENSEX", "NIFTY", "BANKNIFTY", "FINNIFTY"):
                await ws_manager.broadcast({
                    "type": "index_ltp",
                    "data": {"symbol": symbol if symbol in ("SENSEX", "NIFTY", "BANKNIFTY", "FINNIFTY") else "SENSEX", "ltp": ltp},
                })
            elif symbol:
                await ws_manager.broadcast({
                    "type": "instrument_ltp",
                    "data": {"symbol": symbol, "ltp": ltp},
                })
            if symbol and ltp > 0:
                await db.update_signal_ltp(symbol, ltp)
            tick_queue.task_done()
        except asyncio.TimeoutError:
            pass
        except Exception:
            log.exception("Tick consumer error")


# ── WebSocket Connection Manager ──────────────────────────────────────────────
def _json_safe(obj):
    """Recursively convert datetime objects to ISO strings for JSON serialisation."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat().replace("+00:00", "Z")
    return obj


class ConnectionManager:
    """Manages active WebSocket connections with per-socket send locks."""

    def __init__(self):
        # Each socket gets its own asyncio.Lock so concurrent sends don't interleave
        self._connections: dict[WebSocket, asyncio.Lock] = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections[ws] = asyncio.Lock()
        log.info("WS client connected (%d total)", len(self._connections))

    def disconnect(self, ws: WebSocket):
        self._connections.pop(ws, None)
        log.info("WS client disconnected (%d total)", len(self._connections))

    async def send(self, ws: WebSocket, data: dict):
        lock = self._connections.get(ws)
        if not lock:
            return
        async with lock:
            try:
                await ws.send_json(_json_safe(data))
            except Exception:
                log.exception("WS send failed — removing dead connection")  # [FIX #23]
                self.disconnect(ws)

    async def broadcast(self, data: dict):
        """[FIX #18] Iterate a snapshot; catch per-connection exceptions; discard dead sockets."""
        safe_data = _json_safe(data)
        msg_type  = data.get("type", "?")
        if msg_type not in ("instrument_ltp", "index_ltp"):
            log.info("WS broadcast [%s] to %d clients", msg_type, len(self._connections))

        dead = []
        # Snapshot so disconnect() during iteration is safe
        for ws, lock in list(self._connections.items()):
            try:
                async with lock:
                    await ws.send_json(safe_data)
            except Exception:
                log.exception("WS broadcast failed for a client — marking dead")  # [FIX #23]
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def active(self):
        return list(self._connections.keys())


ws_manager = ConnectionManager()


def _today_ist() -> str:
    IST = ZoneInfo("Asia/Kolkata")
    return datetime.now(IST).strftime("%Y-%m-%d")


# ── App Lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "Starting trading platform — mode=%s, kotak_env=%s",
        Config.TRADING_MODE, Config.kotak_env(),
    )
    await db.init_db()
    await manager.paper_trader.rehydrate_from_db()
    try:
        await manager.real_trader.rehydrate_from_db()
    except Exception:
        log.exception("Real trader rehydrate failed on startup")
    manager.set_ws_broadcast(ws_manager.broadcast)
    async def _gated_process_message(text, sender, timestamp):
        """Gate process_message on the stop_trading flag.
        Message is still saved/parsed (visible in UI) but no trade
        is placed when _stop_trading is True.
        """
        manager.stop_trading = _stop_trading
        return await manager.process_message(text=text, sender=sender, timestamp=timestamp)

    telegram.set_callback(_gated_process_message)
    asyncio.create_task(telegram.start())

    # Background: check order/position timeouts every 10 s
    async def timeout_checker():
        while True:
            try:
                await manager.paper_trader.check_timeouts()
                await manager.real_trader.check_timeouts()   # [FIX #29]
                try:
                    await manager.real_trader.check_feed_health()
                except Exception:
                    log.exception("Feed health check error")
            except Exception:
                log.exception("Timeout checker error")  # [FIX #23]
            await asyncio.sleep(10)

    asyncio.create_task(timeout_checker())

    # ── Shared tick pipeline (lifespan scope) ────────────────────────────────
    # [FIX #30] tick pipeline is now module-level — just capture the running loop
    global _main_loop, _tick_drain_task
    _main_loop = asyncio.get_running_loop()

    async def kotak_auto_login():
        if not any(Config.kotak_env().values()):
            log.info("Skipping Kotak auto-login — env not configured.")
            return
        if manager.kotak.is_authenticated and manager.kotak.session_active:
            log.info("Skipping Kotak auto-login — session already active.")
            return

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, manager.initialize_kotak)
        except Exception:
            log.exception("Kotak initialize failed")  # [FIX #23]
            return

        log.info("Kotak Neo client initialized — attempting auto-login...")
        try:
            manager.kotak_login_state      = "logging_in"
            manager.kotak_last_login_error = None

            login_result = await loop.run_in_executor(None, manager.login_kotak)

            if login_result.get("status") == "ok":
                manager.kotak_login_state = "logged_in"
                log.info("Kotak Neo auto-login successful")

                manager.market_feed.start()
                _wire_feed_callbacks()

                # [FIX #31] Wire self-healing reconnect callback
                async def _auto_reconnect_feed():
                    """Full stop→start→resubscribe cycle, called by watchdog thread.

                    [FIX #33] Now re-logins to Kotak when the watchdog detects the
                    token has expired (overnight). Uses the unified
                    _restart_feed_with_resubscribe() helper so callbacks can never
                    be lost on stop().
                    """
                    log.info("[FIX #31] Auto-reconnect triggered by watchdog")
                    try:
                        # [FIX #33] Re-login if token expired (overnight / midnight IST)
                        needs_relogin = manager.market_feed._needs_relogin
                        if needs_relogin or not manager.kotak.is_token_valid():
                            log.info("[FIX #33] Token expired — re-logging in before reconnect")
                            loop = asyncio.get_running_loop()
                            try:
                                await loop.run_in_executor(None, manager.initialize_kotak)
                                def _force_relogin():
                                    manager.kotak.is_authenticated = False
                                    manager.kotak.session_active = False
                                    return manager.login_kotak()
                                login_result = await loop.run_in_executor(None, _force_relogin)
                                if login_result.get("status") == "ok":
                                    log.info("[FIX #33] Re-login successful before reconnect")
                                    manager.market_feed._needs_relogin = False
                                else:
                                    log.error("[FIX #33] Re-login FAILED: %s", login_result.get("message"))
                            except Exception:
                                log.exception("[FIX #33] Re-login exception during reconnect")

                        await _restart_feed_with_resubscribe("watchdog")
                        log.info("[FIX #31] Auto-reconnect complete — feed should resume")
                        await ws_manager.broadcast({"type": "status_update", "data": {"market_feed": True, "auto_reconnect": True}})
                    except Exception:
                        log.exception("[FIX #31] Auto-reconnect failed")

                _restart_feed_with_resubscribe._cb = _auto_reconnect_feed  # type: ignore[attr-defined]
                _wire_feed_callbacks(_auto_reconnect_feed)

                task = asyncio.create_task(tick_consumer())
                _tick_drain_task.clear()
                _tick_drain_task.append(task)
                log.info("Tick consumer task started")

                cached = await loop.run_in_executor(None, manager.contract_master.load_cached)
                if cached:
                    log.info("Pre-loaded %d contracts from cached CSV", len(manager.contract_master.get_all()))

                await asyncio.sleep(3)
                # v3 indices channel (by name) — numeric scrip tokens stream
                # empty pictures for indices (SENSEX header stuck at 0).
                manager.market_feed.subscribe_index_name()
                manager.market_feed.subscribe_index_name(
                    key="SENSEX-BSE", name="SENSEX", exchange_segment="bse_cm")
                manager.market_feed.subscribe_index_name(
                    key="NIFTY", name="Nifty 50", symbol="NIFTY")
                manager.market_feed.subscribe_index_name(
                    key="BANKNIFTY", name="Nifty Bank", symbol="BANKNIFTY")
                manager.market_feed.subscribe_index_name(
                    key="INDIAVIX", name="India VIX", symbol="INDIAVIX")
                await loop.run_in_executor(None, manager.download_contracts)
                await manager.resubscribe_recent_signals(limit=100)

                # [FIX #29] Rehydrate real trader state + background tasks on auto-login
                await manager.real_trader.rehydrate_from_db()
                manager._start_real_background_tasks()
            else:
                manager.kotak_login_state      = "login_failed"
                manager.kotak_last_login_error = login_result.get("message")
                log.warning("Kotak auto-login failed: %s", login_result.get("message"))
        except Exception:
            manager.kotak_login_state      = "login_failed"
            manager.kotak_last_login_error = "Unexpected error — see logs"
            log.exception("Kotak auto-login error")  # [FIX #23]

    asyncio.create_task(kotak_auto_login())

    # ── Daily contract refresh ────────────────────────────────────────────────
    IST         = ZoneInfo("Asia/Kolkata")
    REFRESH_TIME = dt_time(8, 50)

    async def daily_contract_refresh():
        """[FIX #6] Refreshes contracts at 08:50 IST."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                now_ist = datetime.now(IST)
                target  = datetime.combine(now_ist.date(), REFRESH_TIME, tzinfo=IST)
                if now_ist >= target:
                    target += timedelta(days=1)
                wait_secs = (target - now_ist).total_seconds()
                log.info(
                    "Next contract master refresh at %s (%.1fh from now)",
                    target.strftime("%Y-%m-%d %H:%M IST"), wait_secs / 3600,
                )
                await asyncio.sleep(wait_secs)

                if not Config.KOTAK_CONSUMER_KEY:
                    continue

                log.info("08:50 IST — refreshing contract master...")
                try:
                    await loop.run_in_executor(None, manager.initialize_kotak)
                except Exception:
                    log.exception("daily_contract_refresh: initialize_kotak failed")

                def force_relogin():
                    manager.kotak.is_authenticated = False
                    manager.kotak.session_active   = False
                    return manager.login_kotak()

                login_result = await loop.run_in_executor(None, force_relogin)

                if login_result.get("status") == "ok":
                    if _tick_drain_task:
                        old_task = _tick_drain_task[0]
                        old_task.cancel()
                        try:
                            await old_task
                        except asyncio.CancelledError:
                            pass
                        _tick_drain_task.clear()
                        log.info("daily_contract_refresh: old tick drain task cancelled")

                    async def _auto_reconnect_feed_daily():
                        await _restart_feed_with_resubscribe("watchdog-post-daily-refresh")
                    _restart_feed_with_resubscribe._cb = _auto_reconnect_feed_daily  # type: ignore[attr-defined]
                    _wire_feed_callbacks(_auto_reconnect_feed_daily)
                    await _restart_feed_with_resubscribe("daily-refresh")

                    new_task = asyncio.create_task(tick_consumer())
                    _tick_drain_task.clear()
                    _tick_drain_task.append(new_task)
                    log.info("daily_contract_refresh: tick pipeline restarted")

                    await loop.run_in_executor(None, manager.download_contracts)
                    await manager.resubscribe_recent_signals(limit=100)
                    manager.real_trader.reset_eod_flag()
                    log.info("Daily contract master refresh complete")
                else:
                    log.warning("Daily refresh login failed: %s", login_result.get("message"))
            except Exception:
                log.exception("Daily contract refresh error")  # [FIX #23]
                await asyncio.sleep(3600)

    asyncio.create_task(daily_contract_refresh())

    # ── Telegram health check ─────────────────────────────────────────────────
    async def telegram_health_check():
        """[FIX #9] Exponential backoff + circuit breaker after 3 consecutive failures."""
        consecutive_failures = 0
        MAX_FAILURES         = 3
        BASE_BACKOFF_SECS    = 60
        MAX_BACKOFF_SECS     = 600

        while True:
            await asyncio.sleep(BASE_BACKOFF_SECS)

            if not (telegram.is_running and telegram.client):
                continue

            try:
                await telegram.client.get_me()
                if consecutive_failures > 0:
                    log.info("Telegram health check recovered after %d failure(s)", consecutive_failures)
                consecutive_failures = 0

            except Exception:
                consecutive_failures += 1
                backoff = min(BASE_BACKOFF_SECS * (2 ** (consecutive_failures - 1)), MAX_BACKOFF_SECS)
                log.exception(
                    "Telegram health check failed (attempt %d/%d) — reconnecting in %ds",
                    consecutive_failures, MAX_FAILURES, backoff,
                )

                if consecutive_failures >= MAX_FAILURES:
                    log.error(
                        "ALERT: Telegram health check failed %d consecutive times — "
                        "manual intervention may be required. Backing off %ds.",
                        consecutive_failures, backoff,
                    )

                try:
                    await telegram.stop()
                except Exception:
                    log.exception("telegram_health_check: stop() failed")
                try:
                    await telegram.start()
                    log.info("Telegram reconnect succeeded")
                except Exception:
                    log.exception("Telegram reconnect failed")

                await asyncio.sleep(backoff)

    asyncio.create_task(telegram_health_check())

    log.info("Trading platform started")
    try:
        from .autotrender import start_scheduler
        start_scheduler()
    except Exception:
        log.exception("Autotrender scheduler start failed — bot continues without it")
    yield

    try:
        from .autotrender import stop_scheduler
        await stop_scheduler()
    except Exception:
        log.exception("Autotrender scheduler stop failed")
    await telegram.stop()
    manager.market_feed.shutdown()

    if _tick_drain_task:
        _tick_drain_task[0].cancel()

    await db.close_connections()
    log.info("Trading platform stopped")


# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Telegram Kotak Trader",
    description="Telegram signal → Kotak Neo trading bridge",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Token", "Authorization"],
)


def _wire_feed_callbacks(auto_reconnect_fn=None):
    """Single source of truth for (re)attaching feed callbacks.

    market_feed.stop() preserves callbacks by default, but every
    (re)start path must still ensure all 4 callbacks are attached exactly
    once (add_* dedups). Call this after every start().
    """
    manager.market_feed.add_tick_callback(manager.paper_trader.on_tick)
    manager.market_feed.add_tick_callback(manager.real_trader.on_tick)
    manager.market_feed.add_raw_tick_callback(enqueue_tick)
    manager.market_feed.add_order_callback(manager.real_trader.handle_order_feed)
    if auto_reconnect_fn is not None:
        manager.market_feed.set_reconnect_callback(auto_reconnect_fn)


async def _restart_feed_with_resubscribe(reason: str):
    """Full stop → start → rewire → resubscribe cycle shared by all reconnect paths."""
    log.info("feed restart (%s): stopping", reason)
    try:
        manager.market_feed.stop()
    except Exception:
        log.exception("feed restart (%s): stop() failed", reason)
    await asyncio.sleep(2)
    manager.market_feed._started_once = False
    manager.market_feed._session_expired = False
    manager.market_feed.start()
    _wire_feed_callbacks(getattr(_restart_feed_with_resubscribe, "_cb", None))
    if _tick_drain_task:
        old = _tick_drain_task[0]
        if old.done():
            _tick_drain_task.clear()
    if not _tick_drain_task:
        new_task = asyncio.create_task(tick_consumer())
        _tick_drain_task.clear()
        _tick_drain_task.append(new_task)
    await asyncio.sleep(3)
    # v3 indices channel (by name) — see note at auto-login site above.
    manager.market_feed.subscribe_index_name()
    manager.market_feed.subscribe_index_name(
        key="SENSEX-BSE", name="SENSEX", exchange_segment="bse_cm")
    manager.market_feed.subscribe_index_name(
        key="NIFTY", name="Nifty 50", symbol="NIFTY")
    manager.market_feed.subscribe_index_name(
        key="BANKNIFTY", name="Nifty Bank", symbol="BANKNIFTY")
    manager.market_feed.subscribe_index_name(
        key="INDIAVIX", name="India VIX", symbol="INDIAVIX")
    await manager.resubscribe_recent_signals(limit=100)
    log.info("feed restart (%s): complete", reason)


@app.middleware("http")
async def add_cache_control(request, call_next):
    """Force browsers to revalidate shell + assets on every load — no stale cache.

    index.html carries the ?v= bundle pins; if the shell itself caches,
    phones keep loading retired JS forever (live bug: stuck Tune modal).
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# ── Pydantic Models ───────────────────────────────────────────────────────────
class ModeRequest(BaseModel):
    mode: str
    totp: Optional[str] = None
    force: bool = False

class OTPRequest(BaseModel):
    otp: Optional[str] = None

class TestSignalRequest(BaseModel):
    text: str
    sender: str = "Test"

class LotSizeRequest(BaseModel):
    lots: int

class ClearRequest(BaseModel):
    date: Optional[str] = None   # "YYYY-MM-DD" clears that day; None clears all

class StopTradingRequest(BaseModel):
    enabled: bool

class StrategyRequest(BaseModel):
    scope:                    str             = "default"  # "default" | "NIFTY"
    lots:                      int             = 1
    activationPoints:          Optional[float] = 5.0
    activationSLOffset:        Optional[float] = 0.0
    trailGap:                  Optional[float] = 2.0
    bouncePoints:              Optional[int]   = 5
    bufferEnabled:             bool            = False
    bufferPoints:              Optional[float] = 2.0
    entryTimerMins:            int             = 10
    exitTimerMins:             int             = 10
    signalTrailInitialSL:      str             = "telegram"
    signalTrailInitialSLPoints: Optional[float] = 5.0
    entrySlippage:             Optional[float] = 1.0
    exitSlippage:              Optional[float] = 1.0
    # Snap-level SL
    slMode:                    str             = "signal_trail"  # "signal_trail" | "snap_levels"
    snapLevels:                Optional[list]  = None  # [{snapPts, offset}, x3]
    snapTrailAfterL3:          bool            = False
    snapTrailGap:              Optional[float] = 3.0


# ── REST Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    status = manager.get_status()
    status["telegram"]    = telegram.is_running
    status["ws_clients"]  = len(ws_manager.active)
    status["stop_trading"] = _stop_trading
    return status


@app.get("/api/messages")
async def get_messages(
    limit: int           = Query(100, ge=1, le=500),
    date:  Optional[str] = Query(None),
):
    if date is None:
        date = _today_ist()
    elif date == "all":
        date = None
    return await db.get_messages(limit=limit, date=date)


@app.get("/api/signals")
async def get_signals(
    limit: int           = Query(100, ge=1, le=500),
    date:  Optional[str] = Query(None),
):
    if date is None:
        date = _today_ist()
    elif date == "all":
        date = None
    return await db.get_signals(limit=limit, date=date)


@app.get("/api/trades")
async def get_trades(
    mode:  Optional[str] = None,
    limit: int           = Query(200, ge=1, le=1000),
    date:  Optional[str] = Query(None, description="YYYY-MM-DD or 'all'. Defaults to today (IST)."),
):
    if date is None:
        date = _today_ist()
    elif date == "all":
        date = None
    return await db.get_trades(mode=mode, limit=limit, date=date)


@app.get("/api/debug/master-symbols")
async def debug_master_symbols(underlying: str = "GOLD", segment: str = "mcx_fo"):
    """Read-only: list master rows matching an underlying, with row expiry,
    option flag, token presence and lot — for diagnosing bad resolutions."""
    cm = manager.contract_master
    und = underlying.upper()
    rows = []
    for c in cm._segments.get(segment, []):
        sym = cm._sym_of(c)
        if cm._und_matches(sym, und):
            rows.append({
                "symbol":    sym,
                "expiry":    str(c.get("pExpiryDate", "") or c.get("expiry_date", "") or c.get("expiry", "")),
                "opt":       cm._opt_of(c),
                "has_token": bool(str(c.get("pSymbol", "") or c.get("pInstrumentToken", "") or "")),
                "lot":       str(c.get("lLotSize", "") or c.get("pLotSize", "")),
            })
    return {"segment": segment, "underlying": und, "count": len(rows), "rows": rows}


@app.get("/api/positions")
async def get_positions(mode: Optional[str] = None, status: Optional[str] = "open", date: Optional[str] = Query(None)):
    # Empty string from query param ?status= means "all" (open + today's closed)
    if status is not None and status.strip() == "":
        status = None
    return await db.get_positions(mode=mode, status=status, date=date)


@app.get("/api/pnl")
async def get_pnl():
    return manager.paper_trader.get_pnl_summary()


@app.get("/api/performance")
async def get_performance(mode: Optional[str] = "paper", days: int = Query(30, ge=1, le=365),
                          ledger: int = Query(200, ge=0, le=2000),
                          source: Optional[str] = Query(None)):
    """Read-only performance analytics over closed trades (IST days)."""
    return await db.get_performance(mode=mode or "paper", days=days,
                                    ledger_limit=ledger, source=source or None)


@app.post("/api/mode", dependencies=[Depends(require_api_token)])
async def set_mode(req: ModeRequest):
    # TOTP gate: require valid code when switching to real trading
    if req.mode == "real":
        secret = Config.KOTAK_TOTP_SECRET
        if secret:
            import pyotp
            if not req.totp or not pyotp.TOTP(secret).verify(req.totp, valid_window=1):
                return {"status": "error", "message": "Invalid TOTP code"}

    # Safety gate: block real → paper/other when real trades are active
    if manager.mode == "real" and req.mode != "real":
        open_pos  = len(manager.real_trader.get_open_positions())
        pending   = len(manager.real_trader.get_pending_orders())
        if (open_pos + pending) > 0 and not req.force:
            return {
                "status":          "blocked",
                "message":         "Active real trades exist",
                "open_positions":  open_pos,
                "pending_orders":  pending,
            }
        if req.force and (open_pos + pending) > 0:
            log.warning("Force mode switch: killing %d positions + %d pending orders", open_pos, pending)
            await manager.real_trader.square_off_all(exit_reason="mode_switch")
            _today = _today_ist()
            await ws_manager.broadcast({"type": "init", "data": {
                "status":    manager.get_status(),
                "messages":  await db.get_messages(limit=200, date=None),
                "signals":   await db.get_signals(limit=200, date=None),
                "trades":    await db.get_trades(limit=50, date=_today),
                "positions": await db.get_positions(status=None),
            }})

    result = manager.set_mode(req.mode)
    save_mode(req.mode)  # [FIX #32] Persist mode to disk so it survives restarts
    await ws_manager.broadcast({"type": "mode_change", "data": result})
    return result


@app.post("/api/positions/{position_id}/exit", dependencies=[Depends(require_api_token)])
async def exit_position(position_id: int):
    """[FIX #1] close_position() already broadcasts position_update — no duplicate here."""
    if manager.mode == "real":
        result = await manager.real_trader.close_position(position_id, exit_reason="manual")
    else:
        result = await manager.paper_trader.close_position(position_id, exit_reason="user")
    return result


class StopLossRequest(BaseModel):
    stop_loss: float


@app.post("/api/positions/{position_id}/stop-loss", dependencies=[Depends(require_api_token)])
async def set_position_stop_loss(position_id: int, req: StopLossRequest):
    """Manual SL edit from the position card — tap snaps to LTP−1, steppers nudge.
    Takes effect on the next tick. Only confirmed responses repaint the UI."""
    if req.stop_loss is None or req.stop_loss <= 0:
        return {"status": "error", "message": "SL must be above 0"}
    if manager.mode == "real":
        result = await manager.real_trader.set_position_stop_loss(position_id, req.stop_loss)
    else:
        result = await manager.paper_trader.set_position_stop_loss(position_id, req.stop_loss)
    return result


@app.post("/api/orders/{trade_id}/cancel", dependencies=[Depends(require_api_token)])
async def cancel_pending_order(trade_id: int):
    """Cancel a single pending entry order.
    Routes to the active engine's cancel_pending_order() which acquires
    _expiry_lock — preventing race conditions with on_tick() fill logic.
    """
    if manager.mode == "real":
        result = await manager.real_trader.cancel_pending_order(trade_id)
    else:
        result = await manager.paper_trader.cancel_pending_order(trade_id)
    return result


class TuneRequest(BaseModel):
    trade_id: int
    lots: Optional[int] = None
    bouncePoints: Optional[float] = None
    entryLow: Optional[float] = None
    entryHigh: Optional[float] = None
    activationPoints: Optional[float] = None
    activationSLOffset: Optional[float] = None
    trailGap: Optional[float] = None
    entryTimerMins: Optional[int] = None
    exitTimerMins: Optional[int] = None
    signalTrailInitialSL: Optional[str] = None
    signalTrailInitialSLPoints: Optional[float] = None
    slMode: Optional[str] = None
    snapLevels: Optional[list] = None
    snapTrailAfterL3: Optional[bool] = None
    snapTrailGap: Optional[float] = None


def _find_pending_order(trade_id: int):
    """Find a live pending order in either engine (mode may have flipped)."""
    for engine in (manager.paper_trader, manager.real_trader):
        for order in list(engine._pending_orders):
            if order.get("trade_id") == trade_id:
                return engine, order
    return None, None


@app.post("/api/pending/tune", dependencies=[Depends(require_api_token)])
async def tune_pending_order(req: TuneRequest):
    """Retune ONE pending trade's strategy snapshot (lots, bounce, trail, timers).

    Only pending orders — filled/open positions keep their original mode.
    Validates the order is still pending at apply time (fill race safe).
    """
    clean, errors = validate_patch(req.dict(exclude={"trade_id"}))
    if errors:
        return {"status": "error", "message": "; ".join(errors)}
    engine, order = _find_pending_order(req.trade_id)
    if order is None:
        return {"status": "error",
                "message": "Order not pending (filled, expired or cancelled?) — tune applies to pending entries only"}
    if str(order.get("status")) != "pending":
        return {"status": "error", "message": f"Order is {order.get('status')} — tune applies to pending entries only"}
    try:
        tune = apply_patch(order, clean)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception:
        log.exception("tune_pending_order: apply failed")
        return {"status": "error", "message": "Tune failed — see logs"}
    order["tune_json"] = tune_to_json(tune)
    try:
        await db.update_trade(req.trade_id, {"quantity": order["quantity"],
                                             "price": order.get("price"),
                                             "notes": order.get("notes"),
                                             "tune_json": order["tune_json"]})
        if order.get("pending_order_id"):
            await db.update_pending_order(order["pending_order_id"],
                                          {"quantity": order["quantity"],
                                           "price": order.get("price"),
                                           "notes": order.get("notes"),
                                           "tune_json": order["tune_json"]})
    except Exception:
        log.exception("tune_pending_order: db persist failed")
        return {"status": "error", "message": "Tune applied in memory but DB persist failed"}
    await ws_manager.broadcast({"type": "order_update", "data": {
        "id": req.trade_id,
        "signal_id": order.get("signal_id"),
        "status": "pending",
        "status_note": f"Tuned: {tune_summary(tune, order)}",
        "quantity": order["quantity"],
        "price": order.get("price"),
        "notes": order.get("notes"),
        "tune": tune,
        "tune_json": order["tune_json"],
    }})
    return {"status": "ok", "trade_id": req.trade_id, "tune": tune,
            "quantity": order["quantity"], "price": order.get("price"),
            "notes": order.get("notes"), "summary": tune_summary(tune, order)}


@app.get("/api/balance")
async def get_balance():
    """Fetch Kotak account balance/margin limits."""
    if not manager.kotak.is_authenticated:
        return {"status": "error", "message": "Not authenticated"}
    try:
        result = manager.kotak.get_limits()
        return result
    except Exception:
        log.exception("get_balance failed")
        return {"status": "error", "message": "Failed to fetch balance"}


@app.post("/api/kill", dependencies=[Depends(require_api_token)])
async def kill_switch():
    if manager.mode == "real":
        result = await manager.real_trader.square_off_all()
    else:
        result = await manager.paper_trader.square_off_all()
    _today  = _today_ist()
    await ws_manager.broadcast({"type": "init", "data": {
        "status":    manager.get_status(),
        "messages":  await db.get_messages(limit=200, date=None),
        "signals":   await db.get_signals(limit=200, date=None),
        "trades":    await db.get_trades(limit=50, date=_today),
        "positions": await db.get_positions(status=None),
    }})
    return result


@app.get("/api/settings")
async def get_settings():
    return {
        "lot_size": manager.lot_size,
        "strategy": manager.strategy,
        "strategies": getattr(manager, "strategies", {"default": manager.strategy}),
    }


@app.post("/api/settings/lot-size", dependencies=[Depends(require_api_token)])
async def set_lot_size(req: LotSizeRequest):
    result = manager.set_lot_size(req.lots)
    await ws_manager.broadcast({"type": "settings_update", "data": result})
    return result


@app.post("/api/settings/strategy", dependencies=[Depends(require_api_token)])
async def set_strategy(req: StrategyRequest):
    data = req.dict()
    raw_scope = str(data.pop("scope", "default") or "default").upper()
    scope = "NIFTY" if raw_scope == "NIFTY" else "default"
    strategies = getattr(manager, "strategies", None) or {"default": manager.strategy}
    strategies[scope] = {**STRATEGY_DEFAULTS, **data}
    manager.strategies = strategies
    if scope == "default":
        manager.strategy = strategies["default"]
        if req.lots and req.lots != manager.lot_size:
            manager.set_lot_size(req.lots)
    else:
        # Keep default alias untouched; NIFTY lots live inside its own scope.
        manager.strategy = strategies.get("default", manager.strategy)
    save_strategies(manager.strategies)
    await ws_manager.broadcast({"type": "settings_update", "data": {
        "strategy": manager.strategy,
        "strategies": manager.strategies,
        "lot_size": manager.lot_size,
        "scope": scope,
    }})
    return {"status": "ok", "strategy": strategies[scope], "scope": scope,
            "strategies": strategies}


# ── Kotak Auth ────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def kotak_login():
    """[FIX #4] Blocking SDK calls run in executor — event loop stays responsive."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, manager.initialize_kotak)
    return await loop.run_in_executor(None, manager.login_kotak)


@app.post("/api/auth/2fa")
async def kotak_2fa(req: OTPRequest):
    """[FIX #4] complete_2fa internally runs download_contracts in executor (already patched)."""
    return await manager.complete_2fa(req.otp)


# ── Test & Maintenance ────────────────────────────────────────────────────────

@app.post("/api/clear", dependencies=[Depends(require_api_token)])
async def clear_data(req: ClearRequest):
    """Clear dashboard data.
    req.date = "YYYY-MM-DD" → clears only that day's records.
    req.date = None          → clears everything (all history).
    In-memory state (pending orders, open positions) is only wiped on full clear.
    """
    await db.clear_all_data(date=req.date)
    if req.date is None:
        # Full clear — reset in-memory state too
        manager.clear_duplicates()  # no-op now, dedup is DB-backed
        manager.paper_trader._pending_orders.clear()
        if hasattr(manager.paper_trader, "_open_positions"):
            manager.paper_trader._open_positions.clear()
        if hasattr(manager.paper_trader, "_positions"):
            manager.paper_trader._positions.clear()
    msg = f"Data for {req.date} cleared" if req.date else "All dashboard data cleared"
    return {"status": "ok", "message": msg}


@app.post("/api/stop-trading", dependencies=[Depends(require_api_token)])
async def set_stop_trading(req: StopTradingRequest):
    """Toggle the stop-trading flag.
    When enabled=True, incoming Telegram signals are ignored (not traded).
    Existing positions and pending orders continue to be managed normally.
    """
    global _stop_trading
    _stop_trading = req.enabled
    manager.stop_trading = req.enabled   # propagate to TradeManager if it checks this flag
    log.info("Stop trading set to %s", _stop_trading)
    await ws_manager.broadcast({"type": "stop_trading_update", "data": {"enabled": _stop_trading}})
    return {"status": "ok", "enabled": _stop_trading}


@app.post("/api/test-signal", dependencies=[Depends(require_api_token)])
async def test_signal(req: TestSignalRequest):
    if _stop_trading:
        return {"status": "ignored", "reason": "Stop trading is enabled"}
    return await manager.process_message(
        text=req.text,
        sender=req.sender,
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


# ── Fallback Action Endpoints [FIX #25] ──────────────────────────────────────

@app.post("/api/reconnect-market-feed", dependencies=[Depends(require_api_token)])
async def reconnect_market_feed():
    """Restart the LTP WebSocket market feed if it has dropped (unified helper)."""
    try:
        async def _cb():
            await _restart_feed_with_resubscribe("watchdog-post-manual")
        _restart_feed_with_resubscribe._cb = _cb  # type: ignore[attr-defined]
        _wire_feed_callbacks(_cb)
        await _restart_feed_with_resubscribe("manual")
        log.info("reconnect_market_feed: market feed restarted successfully")
        await ws_manager.broadcast({"type": "status_update", "data": {"market_feed": True}})
        return {"status": "ok", "message": "Market feed reconnected and subscriptions restored"}
    except Exception:
        log.exception("reconnect_market_feed: failed")
        return {"status": "error", "message": "Market feed reconnect failed — see logs"}


@app.post("/api/reconnect-telegram", dependencies=[Depends(require_api_token)])
async def reconnect_telegram():
    """Stop and restart the Telegram bot listener."""
    try:
        log.info("reconnect_telegram: stopping Telegram...")
        try:
            await telegram.stop()
        except Exception:
            log.exception("reconnect_telegram: stop() failed")

        await asyncio.sleep(2)
        log.info("reconnect_telegram: restarting Telegram...")
        asyncio.create_task(telegram.start())

        log.info("reconnect_telegram: Telegram restart initiated")
        return {"status": "ok", "message": "Telegram reconnect initiated — status will update in a few seconds"}
    except Exception:
        log.exception("reconnect_telegram: failed")
        return {"status": "error", "message": "Telegram reconnect failed — see logs"}


@app.post("/api/resubscribe-signals", dependencies=[Depends(require_api_token)])
async def resubscribe_signals():
    """Re-send instrument subscriptions for recent signals to the market feed.
    Lightweight fix for LTP showing '--' on signal cards even though the market
    feed is running — subscriptions can silently drop after a WS reconnect.
    Does NOT restart the feed; just re-sends the subscription list.
    """
    try:
        await manager.resubscribe_recent_signals(limit=100)
        active_subs = len(manager.market_feed._subscriptions)
        log.info("resubscribe_signals: done (%d total subscriptions active)", active_subs)
        return {
            "status":  "ok",
            "message": f"Re-subscribed recent signals ({active_subs} instruments active)",
        }
    except Exception:
        log.exception("resubscribe_signals: failed")
        return {"status": "error", "message": "Re-subscribe failed — see logs"}


@app.post("/api/restart-backend", dependencies=[Depends(require_api_token)])
async def restart_backend():
    """Trigger a PM2 restart of this backend process.
    Fires after a 1.5s delay so the HTTP response can flush first.
    WARNING: causes ~10s downtime; open positions resume being managed after restart.
    NOTE: command is allowlisted (no user input) to avoid shell injection.
    """
    import subprocess

    async def _delayed_restart():
        await asyncio.sleep(1.5)
        try:
            subprocess.Popen(["pm2", "restart", "kotak-trader"])
            log.info("restart_backend: pm2 restart triggered")
        except Exception:
            log.exception("restart_backend: pm2 restart failed")

    asyncio.create_task(_delayed_restart())
    return {
        "status":  "ok",
        "message": "PM2 restart triggered — expect ~10s downtime. The page will reconnect automatically.",
    }


@app.post("/api/clear-signal-tracker", dependencies=[Depends(require_api_token)])
async def clear_signal_tracker():
    """No-op — dedup is now fully DB-backed via open-position and pending-order checks."""
    manager.clear_duplicates()
    log.info("clear_signal_tracker: no-op — dedup is now DB-backed")
    return {
        "status":  "ok",
        "message": "Signal dedup is now DB-backed — no in-memory tracker to clear. Signals are only blocked if an open position exists for that strike.",
        "cleared": 0,
    }


# ── Autotrender bridge (ISOLATED ADD-ON — no existing routes touched) ──
try:
    from .autotrender import router as _autotrender_router
    from .autotrender import set_manager as _at_set_manager
    from .autotrender import set_broadcast as _at_set_broadcast
    app.include_router(_autotrender_router)
    _at_set_manager(manager)
    _at_set_broadcast(ws_manager.broadcast)
except Exception:
    log.exception("Autotrender bridge wiring failed — bot continues without it")


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        today = _today_ist()
        await ws_manager.send(ws, {
            "type": "init",
            "data": {
                "status":    manager.get_status(),
                "messages":  await db.get_messages(limit=200, date=today),
                "signals":   await db.get_signals(limit=200, date=today),
                "trades":    await db.get_trades(limit=50, date=today),
                "positions": await db.get_positions(status=None),
            },
        })

        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws_manager.send(ws, {"type": "pong"})
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        log.exception("WS handler error")  # [FIX #23]
        ws_manager.disconnect(ws)


# ── Mount frontend static assets ─────────────────────────────────────────────
# Autotrender dashboard bundle (built from autotrender-personal with
# --base=/autotrender/). Must mount BEFORE "/" or the root mount swallows it.
AT_DIR = os.path.join(FRONTEND_DIR, "autotrender")
if os.path.isdir(AT_DIR):
    app.mount("/autotrender", StaticFiles(directory=AT_DIR, html=True), name="autotrender")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")