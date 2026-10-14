"""
Autotrender inbound calls — ISOLATED ADD-ON (paper testing only).

Does NOT touch existing telegram/signal/trading paths:
- POST /api/autotrender/call stores a call, never auto-trades.
- POST /api/autotrender/calls/{id}/paper-trade explicitly routes ONE call
  through the existing manager.process_message pipeline (paper mode only).
- GET /api/autotrender/calls lists stored calls (in-memory, latest first).

No DB migration: in-memory list only. Restart clears it (paper-test scope).
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import database as db
from . import autotrender_engine as engine
from .candle_builder import CandleBuilder

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autotrender", tags=["autotrender"])

_calls: List[Dict[str, Any]] = []
_next_id = 1

# Wired from main.py (additive, no engine changes)
_manager = None
_broadcast = None

# Live-tick candle store for the engine (read-only; never trades by itself).
_candles = CandleBuilder(timeframe_secs=60, max_bars=500)


def _tick_handler(token, ltp, tick):
    """Fold live Kotak ticks into 1-min bars. Sync by design (feed supports it)."""
    try:
        sym = (tick or {}).get("symbol", "")
        if sym:
            _candles.update(sym, ltp)
    except Exception:
        log.exception("autotrender tick handler failed")


def set_manager(manager) -> None:
    global _manager
    _manager = manager
    # Attach once (add_tick_callback dedups). Survives feed restarts
    # (stop() preserves callbacks by default).
    try:
        feed = getattr(manager, "market_feed", None)
        if feed is not None:
            feed.add_tick_callback(_tick_handler)
    except Exception:
        log.exception("autotrender tick wiring failed — engine status stays empty")


def set_broadcast(broadcast_fn) -> None:
    global _broadcast
    _broadcast = broadcast_fn


class AutotrenderCallIn(BaseModel):
    underlying: str = Field(default="NIFTY", max_length=20)
    side: str = Field(default="BUY CE", max_length=20)  # BUY CE | BUY PE
    strike: int = Field(gt=0)
    entry_low: float = Field(gt=0)
    entry_high: float = Field(gt=0)
    stoploss: float = Field(gt=0)
    target: float = Field(gt=0)
    expiry: Optional[str] = None
    source: str = Field(default="autotrender-personal", max_length=64)


def _format_as_signal_text(call: Dict[str, Any]) -> str:
    """Render the call in the existing instrument-parser format.

    Example: AUTOTRENDER NIFTY 25800CE PRICE @ 125.4-130.6 STOPLOSS 110 TARGETS 160
    The existing parse_instrument_signal() already handles NIFTY index options.
    """
    side = str(call.get("side", "")).upper()
    opt = "PE" if "PE" in side else "CE"
    und = str(call.get("underlying", "NIFTY")).upper()
    strike = int(call.get("strike", 0))
    exp = str(call.get("expiry") or "").strip()
    exp_line = f"\n{exp} EXPIRY" if exp and "EXPIRY" not in exp.upper() else (f"\n{exp}" if exp else "")
    return (
        f"AUTOTRENDER\n{und}\n{strike}{opt}\n"
        f"PRICE @ {call.get('entry_low')}-{call.get('entry_high')}\n"
        f"STOPLOSS\n{call.get('stoploss')}\n"
        f"TARGETS\n{call.get('target')}{exp_line}"
    )


@router.post("/call")
async def receive_call(call: AutotrenderCallIn) -> Dict[str, Any]:
    """Store an external call and AUTO-FORWARD it to the paper engine.

    Safety guards (never disturb live trading):
    - Auto-forward runs ONLY when manager.mode == 'paper'.
    - Skipped when manager.stop_trading is set or trading is otherwise halted.
    - In real mode the call is stored + mirrored as a message, tradable later
      via POST /calls/{id}/paper-trade (which itself is paper-guarded).
    """
    side = call.side.upper().strip()
    if side not in ("BUY CE", "BUY PE"):
        raise HTTPException(status_code=422, detail="side must be 'BUY CE' or 'BUY PE'")
    if call.entry_high < call.entry_low:
        raise HTTPException(status_code=422, detail="entry_high < entry_low")
    return {"status": "ok", "call": await _ingest(call, auto_forward=True)}


async def _ingest(call: AutotrenderCallIn, auto_forward: bool = True) -> Dict[str, Any]:
    """Shared store → message-mirror → (guarded) paper-forward path."""
    global _next_id
    record = call.dict()
    record.update({
        "id": _next_id,
        "status": "received",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    _next_id += 1
    _calls.insert(0, record)
    del _calls[200:]  # cap memory
    log.info("Autotrender call stored id=%s %s %s%s", record["id"], record["underlying"], record["strike"], record["side"].replace("BUY ", ""))
    # Mirror into the Telegram Messages feed (message only — never trades).
    # Uses the same save + new_message broadcast as the telegram listener,
    # so the call simply appears in 📨 Telegram Messages with sender=Autotrender.
    try:
        text = _format_as_signal_text(record)
        msg_id = await db.save_message(text, sender="Autotrender", source="autotrender")
        record["bot_message_id"] = msg_id
        if _broadcast:
            try:
                await _broadcast({"type": "new_message", "data": {
                    "id": msg_id,
                    "raw_text": text,
                    "sender": "Autotrender",
                    "timestamp": record["created_at"],
                }})
            except Exception:
                log.exception("autotrender broadcast failed")
    except Exception:
        log.exception("autotrender message mirror failed (call still stored)")
    # Auto-forward: same pipeline as the manual paper-trade endpoint, but inline.
    # Paper-mode + stop-trading guards keep real trading untouched.
    if auto_forward and _manager is not None \
            and getattr(_manager, "mode", "paper") == "paper" \
            and not getattr(_manager, "stop_trading", False):
        try:
            result = await _manager.process_message(
                text=_format_as_signal_text(record),
                sender="Autotrender",
                timestamp=record["created_at"],
            )
            record["status"] = "sent_to_paper"
            record["paper_message_id"] = result.get("message_id")
            # Tag the signal row for per-model attribution (paper path only).
            try:
                sig = (result.get("signal") or {})
                if sig.get("id") and record.get("model"):
                    await db.update_signal(sig["id"], {
                        "model_version": record["model"].get("version"),
                        "model_score": record["model"].get("score"),
                    })
            except Exception:
                log.exception("autotrender model tagging failed")
            log.info("Autotrender call id=%s auto-forwarded to paper (message %s)",
                     record["id"], result.get("message_id"))
        except Exception:
            log.exception("autotrender auto-forward failed (call kept as received)")
    else:
        log.info("Autotrender call id=%s held as received (mode=%s) — no auto trade",
                 record["id"], getattr(_manager, "mode", "?") if _manager else "unwired")
    return record


@router.get("/calls")
async def list_calls(limit: int = 50) -> Dict[str, Any]:
    return {"calls": _calls[: max(1, min(limit, 200))]}


@router.get("/engine-status")
async def engine_status() -> Dict[str, Any]:
    """Read-only: candle warmth + bias preview per symbol. Never trades."""
    out = []
    for sym in _candles.symbols():
        bars = _candles.get_candles(sym, n=60)
        row: Dict[str, Any] = {"symbol": sym, "bars": len(bars),
                               "last": bars[-1]["c"] if bars else None}
        if len(bars) >= 20:
            try:
                found = engine.detect(bars)
                row.update({"bias": found["bias"], "rsi": round(found["rsi"], 1),
                            "breakout": found["breakout"], "ready": True})
            except Exception:
                log.exception("engine-status detect failed for %s", sym)
                row.update({"ready": False})
        else:
            row.update({"ready": False, "need": 20 - len(bars)})
        out.append(row)
    return {"symbols": out,
            "scheduler": {"running": _sched_task is not None,
                          "last_bias": _sched_last_bias,
                          "auto_today": _sched_count,
                          "last_chain_keys": _sched_last_chain_keys}}


@router.get("/live")
async def live_snapshot() -> Dict[str, Any]:
    """Live engine picture for the dashboard.

    Chain snapshot publishes on every in-hours tick (even without a trigger),
    so ready=True from the first successful snapshot. The "engine" block holds
    the why behind the latest tick — trend, trigger, fusion verdict, outcome.
    """
    if not _last_live.get("ready"):
        return {"ready": False, "scheduler_running": _sched_task is not None}
    return dict(_last_live)


@router.post("/calls/{call_id}/paper-trade")
async def paper_trade_call(call_id: int) -> Dict[str, Any]:
    """Explicit user action: route ONE stored call through the existing pipeline.

    Paper mode only. Blocked when manager.mode != 'paper' to protect real trading.
    Uses manager.process_message so dedup/contract-resolve/paper-fill logic is unchanged.
    """
    if _manager is None:
        raise HTTPException(status_code=500, detail="Autotrender bridge not wired")
    if getattr(_manager, "mode", "paper") != "paper":
        raise HTTPException(status_code=409, detail="Blocked: switch bot to paper mode first")
    rec = next((c for c in _calls if c.get("id") == call_id), None)
    if rec is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if rec.get("status") == "sent_to_paper":
        return {"status": "ok", "detail": "Already sent", "call": rec}
    # Local import to avoid circulars at module load
    from fastapi import Depends as _D  # noqa: F401 (kept for symmetry with main guards)
    text = _format_as_signal_text(rec)
    try:
        result = await _manager.process_message(
            text=text,
            sender="Autotrender",
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    except Exception:
        log.exception("autotrender paper-trade failed")
        raise HTTPException(status_code=500, detail="Paper-trade failed — see bot logs")
    rec["status"] = "sent_to_paper"
    try:
        sig = (result.get("signal") or {})
        if sig.get("id") and rec.get("model"):
            await db.update_signal(sig["id"], {
                "model_version": rec["model"].get("version"),
                "model_score": rec["model"].get("score"),
            })
    except Exception:
        log.exception("autotrender model tagging failed")
    return {"status": "ok", "call": rec, "result": {"message_id": result.get("message_id")}}


# ── Autonomous scheduler (in-bot live generation) ──────────────────────────
# Generates NIFTY option calls from live Kotak ticks + quotes and ingests them
# through the SAME guarded _ingest path as external calls. Paper-mode,
# stop-trading, market-hours, cooldown and daily-cap guards apply. Nothing
# here can place real orders: _ingest only reaches process_message, and the
# engine's paper_trader fills only when manager.mode == 'paper'.

SCHED_INTERVAL_SECS = 300       # evaluate every 5 minutes
SCHED_COOLDOWN_SECS = 900       # min gap between auto calls (same bias)
SCHED_MAX_PER_DAY = 20          # safety cap on auto calls per IST day
SCHED_SYMBOL = "NIFTY"
SCHED_WIDTH = 9                 # strikes each side of ATM for the snapshot

_sched_task = None
_sched_last_ts: float = 0.0
_sched_last_bias: Optional[str] = None
_sched_last_breakout: bool = False
_sched_day: Optional[str] = None
_sched_count: int = 0
_sched_last_chain_keys: List[str] = []
# Last successful live snapshot for the dashboard (GET /api/autotrender/live).
_last_live: Dict[str, Any] = {}


def _publish_engine(moment, outcome: str, bars=None, found=None, trend=None,
                    trig=None, vol=None, pos=None, fused=None) -> None:
    """Record the why behind every tick (fire AND silence) for the dashboard.

    Chain data (when available) is published separately by _publish_chain;
    this block is updated on EVERY in-hours tick so "working but quiet" is
    distinguishable from "broken". Never raises.
    """
    try:
        from datetime import timezone as _tz
        now_ts = (moment or datetime.now(_tz.utc)).timestamp()
        remaining = 0
        if _sched_last_bias and _sched_last_ts:
            remaining = max(0, int(SCHED_COOLDOWN_SECS - (now_ts - _sched_last_ts)))
        _last_live["engine"] = {
            "at": _ist_now(moment).isoformat(timespec="seconds"),
            "outcome": outcome,
            "bars": len(bars) if bars else 0,
            "rsi": (found or {}).get("rsi"),
            "trend": trend, "trigger": trig, "volatility": vol,
            "positioning": pos, "fusion": fused,
            "cooldown": {"last_bias": _sched_last_bias,
                         "remaining_secs": remaining},
            "auto_today": _sched_count,
        }
    except Exception:
        log.exception("publish engine state failed")


def _publish_chain(moment, spot: float, snap: Dict[str, Any]) -> None:
    """Cache the live chain picture for the dashboard.

    Called on EVERY successful snapshot — even when no trigger fires — so
    the dashboard lights up in quiet markets too.
    """
    summ = engine.summarize(snap["rows"])
    sr = engine.get_support_resistance(snap["rows"])
    _last_live.update({
        "ready": True, "at": _ist_now(moment).isoformat(timespec="seconds"),
        "spot": {"symbol": "NIFTY 50", "price": spot},
        "expiry": snap.get("expiry"), "atm": snap.get("atm"),
        "rows": snap["rows"], "pcr": summ["pcr"], "pcrChg": summ["pcrChg"],
        "support": sr["support"], "resistance": sr["resistance"],
        "maxPain": engine.max_pain(snap["rows"]),
        "straddle": engine.straddle(snap["rows"]),
        "quote_keys": snap.get("quote_keys", []),
        "chain_source": snap.get("source", "unknown"),
    })


def _reset_scheduler_state() -> None:
    """Test-only: clear cooldown/cap state (cooldown + daily cap)."""
    global _sched_last_ts, _sched_last_bias, _sched_day, _sched_count, _sched_last_chain_keys
    global _sched_last_breakout
    _sched_last_ts = 0.0
    _sched_last_bias = None
    _sched_last_breakout = False
    _sched_day = None
    _sched_count = 0
    _sched_last_chain_keys = []
    _last_live.clear()


def _ist_now(now: Optional[datetime] = None) -> datetime:
    from zoneinfo import ZoneInfo
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Kolkata"))


def _in_market_hours(now: Optional[datetime] = None) -> bool:
    t = _ist_now(now).time()
    return (t.hour, t.minute) >= (9, 15) and (t.hour, t.minute) < (15, 30)


async def scheduler_tick(now: Optional[datetime] = None,
                         builder: Optional[CandleBuilder] = None) -> Dict[str, Any]:
    """One scheduler evaluation. Returns {"action": ...} — never raises."""
    global _sched_day, _sched_count, _sched_last_ts, _sched_last_bias
    global _sched_last_breakout, _sched_last_chain_keys
    from . import kotak_chain as chain
    b = builder or _candles
    moment = now or datetime.now(timezone.utc)
    if _manager is None:
        return {"action": "skip", "reason": "unwired"}
    if getattr(_manager, "mode", "paper") != "paper":
        return {"action": "skip", "reason": "not paper mode"}
    if getattr(_manager, "stop_trading", False):
        return {"action": "skip", "reason": "stop trading on"}
    if not _in_market_hours(moment):
        return {"action": "skip", "reason": "outside market hours"}

    day = _ist_now(moment).strftime("%Y-%m-%d")
    if _sched_day != day:
        _sched_day, _sched_count = day, 0
    if _sched_count >= SCHED_MAX_PER_DAY:
        return {"action": "skip", "reason": "daily cap reached"}

    bars = b.get_candles(SCHED_SYMBOL, n=60)
    if len(bars) < 20:
        _publish_engine(moment, f"warming up ({len(bars)}/20 bars)", bars=bars)
        return {"action": "skip", "reason": f"warming up ({len(bars)}/20 bars)"}
    try:
        found = engine.detect(bars)
    except Exception:
        log.exception("scheduler detect failed")
        _publish_engine(moment, "detect error", bars=bars)
        return {"action": "skip", "reason": "detect error"}
    # Free local models first (no Kotak calls): trend + trigger + volatility.
    from . import at_models as models
    trend = models.trend_score(bars, models.session_vwap(bars))
    trig = models.trigger_ok(found, bars)
    vol = models.volatility_gate(bars)

    # Chain snapshot BEFORE the trigger gate: the dashboard must light up in
    # quiet markets too, and positioning needs rows even when we don't fire.
    kotak = getattr(_manager, "kotak", None)
    cm = getattr(_manager, "contract_master", None)
    if kotak is None or cm is None or not getattr(kotak, "is_authenticated", False):
        _publish_engine(moment, "kotak not authenticated", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol)
        return {"action": "skip", "reason": "kotak not authenticated"}
    spot = bars[-1]["c"]
    try:
        snap = chain.chain_snapshot(kotak.client, cm, underlying=SCHED_SYMBOL,
                                    spot=spot, width=SCHED_WIDTH)
    except Exception:
        log.exception("scheduler chain snapshot failed")
        _publish_engine(moment, "chain snapshot failed", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol)
        return {"action": "skip", "reason": "chain snapshot failed"}
    _sched_last_chain_keys = snap.get("quote_keys", [])
    _publish_chain(moment, spot, snap)

    if not trig["fire"]:
        _publish_engine(moment, f"no trigger ({trig['reason']})", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol)
        return {"action": "skip", "reason": f"no trigger ({trig['reason']})"}
    if not vol["ok"]:
        _publish_engine(moment, f"volatility veto ({vol['reason']})", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol)
        return {"action": "skip", "reason": f"volatility veto ({vol['reason']})"}

    ts = moment.timestamp()
    # Cooldown suppresses same-drift repeats, but a FRESH breakout episode
    # jumps the queue once — new information, not a repeat. Persistent
    # breakouts (same episode as the last fired call) still wait.
    fresh_breakout = (trig["direction"] == "BULLISH" and "breakout=HIGH" in trig["reason"]) or \
                     (trig["direction"] == "BEARISH" and "breakout=LOW" in trig["reason"])
    if trig["direction"] == _sched_last_bias and ts - _sched_last_ts < SCHED_COOLDOWN_SECS \
            and not (fresh_breakout and not _sched_last_breakout):
        _publish_engine(moment, "cooldown", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol)
        return {"action": "skip", "reason": "cooldown"}

    # Positioning needs live chain rows; fuse everything for the final vote.
    pos = models.positioning(snap["rows"], spot, trig["direction"])
    fused = models.fuse(trend, trig, pos, vol)
    if not fused["fire"]:
        _publish_engine(moment, f"fusion blocked (score={fused['score']})",
                        bars=bars, found=found, trend=trend, trig=trig,
                        vol=vol, pos=pos, fused=fused)
        return {"action": "skip",
                "reason": f"fusion blocked (score={fused['score']})"}
    direction = fused["direction"]

    a = engine.atr(bars, 14)
    lv = engine.entry_exit(bars[-1], a, direction)
    if lv["entry"] is None:
        _publish_engine(moment, "no levels", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol,
                        pos=pos, fused=fused)
        return {"action": "skip", "reason": "no levels"}
    details = engine.option_call_details(snap["rows"], spot, direction,
                                         lv["entry"], lv["sl"], lv["tgt"])
    if details["side"] == "NO TRADE":
        _publish_engine(moment, "no strike", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol,
                        pos=pos, fused=fused)
        return {"action": "skip", "reason": "no strike"}
    try:
        model = AutotrenderCallIn(underlying=SCHED_SYMBOL, side=details["side"],
                                  strike=int(details["strike"]),
                                  entry_low=details["entryLow"],
                                  entry_high=details["entryHigh"],
                                  stoploss=details["sl"], target=details["tgt"],
                                  source="autotrender-engine")
    except Exception:
        log.exception("scheduler call validation failed")
        return {"action": "skip", "reason": "validation failed"}

    record = await _ingest(model, auto_forward=True)
    record["model"] = {"version": fused["version"], "score": fused["score"],
                       "direction": direction, "trend": trend,
                       "trigger": trig["reason"], "positioning": pos,
                       "volatility": vol}
    _sched_last_ts, _sched_last_bias = ts, direction
    _sched_last_breakout = fresh_breakout
    _sched_count += 1
    _publish_engine(moment, f"generated {direction} {details['side']} {details['strike']}",
                    bars=bars, found=found, trend=trend, trig=trig, vol=vol,
                    pos=pos, fused=fused)
    return {"action": "generated", "call": record}


async def _scheduler_loop() -> None:
    import asyncio
    while True:
        try:
            await asyncio.sleep(SCHED_INTERVAL_SECS)
            result = await scheduler_tick()
            if result.get("action") != "skip":
                log.info("scheduler: %s", result.get("action"))
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("scheduler loop error")


def start_scheduler() -> bool:
    """Create the background task (idempotent). Called from lifespan startup."""
    global _sched_task
    if _sched_task is not None:
        return True
    try:
        import asyncio
        _sched_task = asyncio.create_task(_scheduler_loop())
        log.info("autotrender scheduler started (every %ss)", SCHED_INTERVAL_SECS)
        return True
    except Exception:
        log.exception("scheduler start failed")
        return False


async def stop_scheduler() -> None:
    global _sched_task
    task, _sched_task = _sched_task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except Exception:
            pass
