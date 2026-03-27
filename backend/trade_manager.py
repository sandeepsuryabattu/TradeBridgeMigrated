"""
Trade Manager — Central orchestrator that routes parsed signals to Paper or Real trading engine.
Handles mode switching, duplicate detection, and market hours validation.

PATCHES APPLIED:
 [1] _cancel_pending_duplicate dead code removed; inline logic consolidated
 [2] get_status() now includes strategy and lot_size directly
 [4] entryAvgPick added to default strategy dict in __init__
 [5] _is_market_open() uses IST timezone explicitly instead of naive local time
 [6] complete_2fa offloads download_contracts to executor to avoid blocking event loop
 [8] Redundant int() casts removed in resubscribe_recent_signals
 [9] set_ws_broadcast comment clarified
[10] new_message broadcast uses raw_text key for consistency with DB schema
[11] compareMode added — spawns all 5 entry strategies simultaneously for comparison
[14] signal_trail SL mode added to strategy defaults with activationPoints and trailGap
[FIX #3 ] compare mode no longer bypasses dedup — open-position check
          both run unconditionally before the compare/live branch split
[FIX #16] all datetime calls use explicit UTC (via _utc_now()) — no more naive datetimes
[FIX #23] bare except: pass / except Exception as e: log.error replaced with log.exception()
[FIX #25] stop_trading flag added — when True, message/signal is saved and broadcast but
          no trade is placed. Flag is set by main.py before every process_message call.
"""
import asyncio
import logging
from datetime import datetime, time, timezone
from typing import Optional, Callable, Any, Dict
from zoneinfo import ZoneInfo

from .config import Config
from .signal_parser import parse_signal
from .paper_trader import PaperTrader
from .real_trader import RealTrader
from .kotak_trader import KotakTrader
from .market_feed import MarketFeed
from .contract_master import ContractMaster
from . import database as db

log = logging.getLogger(__name__)

# [5] Explicit IST timezone — never rely on server's local clock
IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN  = time(9, 15)
MARKET_CLOSE = time(15, 30)


def _utc_now() -> str:
    """[FIX #16] Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TradeManager:
    """Orchestrates the full signal → trade pipeline."""

    def __init__(self):
        self.mode = Config.TRADING_MODE  # 'paper' or 'real'
        self.kotak = KotakTrader()
        self.market_feed = MarketFeed(self.kotak)
        self.paper_trader = PaperTrader(self.market_feed)
        self.real_trader = RealTrader(self.kotak, self.market_feed)
        self.market_feed.add_order_callback(self.real_trader.handle_order_feed)
        self.contract_master = ContractMaster()
        self.lot_size = int(Config.DEFAULT_LOT_SIZE)

        # Wire mode guard — prevents wrong engine from firing on ticks
        self.paper_trader.set_active_mode_fn(lambda: self.mode)
        self.real_trader.set_active_mode_fn(lambda: self.mode)

        # Background task handles for reconciliation + EOD
        self._real_reconcile_task: Optional[asyncio.Task] = None
        self._real_eod_task: Optional[asyncio.Task] = None

        # [FIX #25] Stop-trading flag — when True, signals are saved/broadcast
        # but no trade is placed. Set by main.py before every process_message call.
        self.stop_trading: bool = False

        # Strategy defaults — simplified to bounce-back entry + signal_trail SL
        self.strategy: Dict[str, Any] = {
            "lots":             1,
            "activationPoints": 5.0,
            "trailGap":         2.0,
            "bouncePoints":     5,
            "bufferEnabled":    False,
            "bufferPoints":     2.0,
        }

        self._ws_broadcast: Optional[Callable] = None
        self.kotak_login_state: str = "idle"
        self.kotak_last_login_error: Optional[str] = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def set_lot_size(self, lots: int):
        self.lot_size = max(1, int(lots))
        log.info("Lot size updated to %d", self.lot_size)
        return {"status": "ok", "lot_size": self.lot_size}

    def set_ws_broadcast(self, broadcast_fn):
        """Set the WebSocket broadcast function for real-time frontend updates."""
        self._ws_broadcast = broadcast_fn
        self.paper_trader.set_ws_broadcast(broadcast_fn)
        self.real_trader.set_ws_broadcast(broadcast_fn)

    async def _broadcast(self, event_type: str, data: dict):
        if self._ws_broadcast:
            try:
                await self._ws_broadcast({"type": event_type, "data": data})
            except Exception:
                log.exception("Broadcast error in TradeManager")  # [FIX #23]

    # ── Signal Processing Pipeline ────────────────────────────────────────────

    async def process_message(self, text: str, sender: str = "", timestamp: str = ""):
        """Full pipeline: receive → parse → deduplicate → execute → broadcast."""

        # 1. Save raw message
        msg_id = await db.save_message(text, sender=sender)

        # [10] raw_text key matches what db.get_messages() returns on page load
        await self._broadcast("new_message", {
            "id":        msg_id,
            "raw_text":  text,
            "sender":    sender,
            "timestamp": timestamp or _utc_now(),
        })

        # 2. Parse signal
        signal      = parse_signal(text)
        signal_dict = signal.to_dict()

        # 3. Save parsed signal
        signal_id            = await db.save_signal(msg_id, signal_dict)
        signal_dict["id"]           = signal_id
        signal_dict["message_id"]   = msg_id

        # Attach live LTP if available
        initial_ltp = 0
        if self.market_feed and self.contract_master:
            op_type = signal_dict.get("option_type") or ""
            lookup  = self.contract_master.lookup(str(signal_dict.get("strike") or ""), op_type)
            if lookup:
                initial_ltp = self.market_feed.get_ltp(lookup["instrument_token"])

        signal_dict["created_at"] = _utc_now()
        signal_dict["live_ltp"]   = initial_ltp
        await self._broadcast("new_signal", signal_dict)

        if signal.status != "valid":
            log.info("Signal ignored: %s", signal.reason)
            return {"message_id": msg_id, "signal": signal_dict, "trade": None}

        # Subscribe to market feed for valid signals
        if self.market_feed and self.contract_master:
            op_type = signal_dict.get("option_type") or ""
            lookup  = self.contract_master.lookup(str(signal_dict.get("strike") or ""), op_type)
            if lookup:
                token  = lookup["instrument_token"]
                symbol = lookup.get(
                    "trading_symbol",
                    f"SENSEX{signal_dict['strike']}{signal_dict['option_type']}",
                )
                self.market_feed.subscribe_instrument(token, symbol)
                log.info("Subscribed market feed for %s (token: %s)", symbol, token)
                # Attach contract lot_size so paper_trader can use it
                try:
                    signal_dict["contract_lot_size"] = int(float(lookup.get("lot_size") or 0)) or None
                except (ValueError, TypeError):
                    signal_dict["contract_lot_size"] = None
            else:
                log.warning(
                    "Could not find instrument token for %s %s — ticks may not arrive",
                    signal_dict.get("strike"), signal_dict.get("option_type"),
                )

        # [FIX #25] Stop-trading gate — signal saved and broadcast above,
        # but we skip ALL trade execution when the flag is set.
        # We DO save a trade row with status='stopped' so it appears in the
        # trade log, and we broadcast order_update so the signal card shows
        # 'stopped' status and the countdown timer is suppressed.
        # We do NOT add to _processed_signals so the signal can be traded
        # once trading is re-enabled (no dedup block on the next arrival).
        if self.stop_trading:
            log.info(
                "Stop trading enabled — signal %s %s received but not traded",
                signal.strike, signal.option_type,
            )
            # Save a minimal trade row so it appears in the log
            trading_symbol = f"SENSEX{signal.strike}{signal.option_type}"
            stopped_trade_data = {
                "mode":             self.mode,
                "trading_symbol":   trading_symbol,
                "transaction_type": "B",
                "order_type":       "L",
                "quantity":         self.lot_size,
                "price":            signal_dict.get("entry_low", 0),
                "status":           "stopped",
                "notes":            "Trading stopped — signal not executed",
            }
            stopped_trade_id = await db.save_trade(signal_id, stopped_trade_data)
            stopped_trade_data["trade_id"] = stopped_trade_id
            stopped_trade_data["id"]       = stopped_trade_id
            stopped_trade_data["signal_id"] = signal_id
            stopped_trade_data["created_at"] = _utc_now()

            # Broadcast the trade row to the log
            await self._broadcast("new_trade", stopped_trade_data)

            # Update signal card to show 'stopped' and suppress timer
            await self._broadcast("order_update", {
                "id":          stopped_trade_id,
                "signal_id":   signal_id,
                "status":      "stopped",
                "status_note": "Trading stopped",
            })

            return {
                "message_id": msg_id,
                "signal":     signal_dict,
                "trade":      stopped_trade_data,
                "skipped":    "stop_trading",
            }

        # ── [FIX #3] Dedup: block only if open position exists for this strike ──

        # 4a. Open-position duplicate check
        open_positions = await db.get_positions(mode=None, status="open")  # [SAFETY] cross-mode check
        for pos in open_positions:
            if (str(pos.get("strike")) == str(signal.strike) and
                    str(pos.get("option_type", "")).upper() == signal.option_type.upper()):
                log.info(
                    "Signal skipped — position already open for %s-%s",
                    signal.strike, signal.option_type,
                )
                return {
                    "message_id": msg_id,
                    "signal":     signal_dict,
                    "trade":      None,
                    "skipped":    "position_open",
                }

        # [1] Cancel any existing pending order for this strike before placing a new one
        sig_suffix    = f"{signal.strike}{signal.option_type}".upper()
        pending_trades = [t for t in await db.get_trades(mode=self.mode) if t.get("status") == "pending"]
        order_replaced = False
        replaced_signal_id = None

        for order in pending_trades:
            order_sym = order.get("trading_symbol", "").upper()
            if order_sym.endswith(sig_suffix):
                try:
                    await db.update_trade(order["id"], {"status": "replaced"})
                except Exception:
                    log.exception("process_message: failed to mark order as replaced")
                # Remove from the active engine's pending list
                if self.mode == "real":
                    self.real_trader._pending_orders = [
                        po for po in self.real_trader._pending_orders
                        if po.get("trade_id") != order["id"]
                    ]
                else:
                    self.paper_trader._pending_orders = [
                        po for po in self.paper_trader._pending_orders
                        if po.get("trade_id") != order["id"]
                    ]
                log.info("Cancelled old pending order %d for %s", order["id"], sig_suffix)
                replaced_signal_id = order.get("signal_id")
                await self._broadcast("order_update", {
                    "id":          order["id"],
                    "signal_id":   replaced_signal_id,
                    "status":      "replaced",
                    "status_note": "Replaced by newer signal",
                })
                order_replaced = True
                break

        # 4b. (removed) — in-memory sig_hash dedup removed; DB checks above are sufficient

        # 5. Market hours check (blocks real trades only, warns for paper)
        if not self._is_market_open():
            if self.mode == "real":
                log.warning("Market is closed — skipping real trade")
                return {
                    "message_id": msg_id,
                    "signal":     signal_dict,
                    "trade":      None,
                    "skipped":    "market_closed",
                }
            else:
                log.info("Market is closed but paper mode active — proceeding")

        # 6. Execute trade
        trade_result: Dict[str, Any] = await self._execute_trade(signal_dict, signal_id)

        order_dict    = trade_result.get("order")
        order_sym_out = order_dict.get("trading_symbol", "") if isinstance(order_dict, dict) else ""

        # Build a clean broadcast payload — only include fields that belong on a trade row.
        # Spreading **trade_result directly caused ghost rows because the result dict
        # contains top-level keys (signal_id, skipped, compare_mode etc.) that have no
        # trade_id/id anchor, making the frontend insert them as new blank rows.
        broadcast_payload = {
            **(order_dict if isinstance(order_dict, dict) else {}),
            **{k: v for k, v in trade_result.items() if k != "order"},
            "signal_id":      signal_id,
            "trading_symbol": trade_result.get("trading_symbol") or order_sym_out,
        }

        await self._broadcast("new_trade", broadcast_payload)

        return {"message_id": msg_id, "signal": signal_dict, "trade": trade_result}

    # ── Trade Execution ───────────────────────────────────────────────────────

    async def _execute_trade(self, signal: dict, signal_id: int) -> dict:
        if self.mode == "paper":
            return await self._execute_paper(signal, signal_id)
        elif self.mode == "real":
            return await self._execute_real(signal, signal_id)
        return {"status": "error", "message": f"Unknown mode: {self.mode}"}

    async def _execute_paper(self, signal: dict, signal_id: int) -> dict:
        """Execute via paper trading engine — bounce-back entry + signal_trail SL."""
        try:
            result = await self.paper_trader.place_order(
                signal, signal_id,
                lot_size=self.lot_size,
                strategy=self.strategy,
            )
            log.info("Paper trade placed: %s", result)
            return result
        except Exception:
            log.exception("Paper trade error")
            return {"status": "error", "message": "Paper trade failed — see logs"}

    async def _execute_real(self, signal: dict, signal_id: int) -> dict:
        """Execute via RealTrader — bounce-back entry + exchange SL."""
        if not self.kotak.is_authenticated:
            return {"status": "error", "message": "Kotak Neo not authenticated"}
        try:
            result = await self.real_trader.place_order(
                signal, signal_id,
                lot_size=self.lot_size,
                strategy=self.strategy,
            )
            log.info("Real trade placed: %s", result)
            return result
        except Exception:
            log.exception("Real trade error")
            return {"status": "error", "message": "Real trade failed — see logs"}

    # ── Market Feed ───────────────────────────────────────────────────────────

    async def resubscribe_recent_signals(self, limit: int = 20):
        """Re-subscribe to market feed for recently processed signals."""
        if not self.market_feed or not self.contract_master:
            return

        recent_signals = await db.get_signals(limit=limit)
        subs_count = 0
        for sig in recent_signals:
            if sig.get("status") == "valid" and sig.get("strike") and sig.get("option_type"):
                lookup = self.contract_master.lookup(str(sig["strike"]), sig["option_type"])
                if lookup:
                    token  = lookup["instrument_token"]
                    symbol = lookup.get("trading_symbol", f"SENSEX{sig['strike']}{sig['option_type']}")
                    if str(token) not in self.market_feed._subscriptions:
                        self.market_feed.subscribe_instrument(token, symbol)
                        subs_count += 1
        if subs_count:
            log.info("Re-subscribed to %d recent signals from database", subs_count)

    # ── Mode Management ───────────────────────────────────────────────────────

    def set_mode(self, mode: str) -> dict:
        if mode not in ("paper", "real"):
            return {"status": "error", "message": "Mode must be 'paper' or 'real'"}
        old_mode  = self.mode
        self.mode = mode
        log.info("Trading mode changed: %s → %s", old_mode, mode)

        # Swap tick callbacks for the active engine
        if self.market_feed:
            if old_mode == "paper" and mode == "real":
                self.market_feed.remove_tick_callback(self.paper_trader.on_tick)
                self.market_feed.add_tick_callback(self.real_trader.on_tick)
            elif old_mode == "real" and mode == "paper":
                self.market_feed.remove_tick_callback(self.real_trader.on_tick)
                self.market_feed.add_tick_callback(self.paper_trader.on_tick)

        return {"status": "ok", "old_mode": old_mode, "new_mode": mode}

    # ── Kotak Auth ────────────────────────────────────────────────────────────

    def initialize_kotak(self) -> dict:
        return {"initialized": self.kotak.initialize()}

    def login_kotak(self) -> dict:
        return self.kotak.login()

    async def complete_2fa(self, otp: Optional[str] = None) -> dict:
        result = self.kotak.complete_2fa(otp)
        if result.get("status") == "ok":
            self.market_feed.start()
            self.market_feed.add_tick_callback(self.paper_trader.on_tick)
            # Also register real trader tick callback (harmless if not in real mode
            # — it just won't have any pending orders / positions to process)
            self.market_feed.add_tick_callback(self.real_trader.on_tick)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.download_contracts)
            await self.resubscribe_recent_signals(limit=20)
            # Rehydrate real trader state from DB
            await self.real_trader.rehydrate_from_db()
            # Start background tasks for real trading
            self._start_real_background_tasks()
        return result

    def download_contracts(self):
        if self.kotak.is_authenticated:
            try:
                success = self.contract_master.download(self.kotak)
                if success:
                    log.info(
                        "Contract master loaded: %d SENSEX contracts",
                        len(self.contract_master.get_all()),
                    )
                else:
                    log.warning("Contract master download failed — using fallback symbol construction")
            except Exception:
                log.exception("download_contracts failed")

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _is_market_open(self) -> bool:
        """[5] Check market hours using explicit IST timezone."""
        now_ist = datetime.now(IST).time()
        return MARKET_OPEN <= now_ist <= MARKET_CLOSE

    def get_status(self) -> dict:
        """[2] Return full system status including lot_size and strategy."""
        kotak_status = self.kotak.get_status()
        if self.kotak_login_state != "idle":
            kotak_status["login_state"] = self.kotak_login_state
        if self.kotak_last_login_error:
            kotak_status["last_error"] = self.kotak_last_login_error

        return {
            "mode":         self.mode,
            "market_open":  self._is_market_open(),
            "kotak":        kotak_status,
            "market_feed":  self.market_feed.is_running,
            "telegram":     True,   # overwritten by main.py with live value
            "paper_trader": self.paper_trader.get_pnl_summary(),
            "real_trader":  self.real_trader.get_pnl_summary(),
            "lot_size":     self.lot_size,
            "strategy":     self.strategy,
        }

    def clear_duplicates(self):
        """No-op — kept for API compatibility. Dedup is now fully DB-backed."""
        pass

    # ── Background Tasks ──────────────────────────────────────────────────────

    def _start_real_background_tasks(self):
        """Launch reconciliation and EOD-check loops for real trading."""
        if self._real_reconcile_task is None or self._real_reconcile_task.done():
            self._real_reconcile_task = asyncio.ensure_future(self._reconcile_loop())
        if self._real_eod_task is None or self._real_eod_task.done():
            self._real_eod_task = asyncio.ensure_future(self._eod_loop())

    async def _reconcile_loop(self):
        """Every 30s, verify exchange SL orders are intact."""
        while True:
            try:
                await asyncio.sleep(30)
                if self.mode == "real" and self.kotak.is_authenticated:
                    await self.real_trader.reconcile_orders()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("reconcile_loop error")

    async def _eod_loop(self):
        """Every 60s, check for EOD auto-close."""
        while True:
            try:
                await asyncio.sleep(60)
                if self.mode == "real":
                    await self.real_trader.check_eod()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("eod_loop error")