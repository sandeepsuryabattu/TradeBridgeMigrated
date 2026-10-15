"""
Autotrender inbound calls — ISOLATED ADD-ON (paper testing only).

Does NOT touch existing telegram/signal/trading paths:
- POST /api/autotrender/call stores a call, never auto-trades.
- POST /api/autotrender/calls/{id}/paper-trade explicitly routes ONE call
  through the existing manager.process_message pipeline (paper mode only).
- GET /api/autotrender/calls lists stored calls (persisted to data/autotrender_state.json).

Dashboard state (last chain/engine/ballot/decisions + calls) persists across
restarts; same-session snapshots reload on boot, yesterday's are ignored.
No DB migration: flat JSON files only.
"""
import json
import logging
import os
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
# Closed bars persist to data/candles.json so restarts reload the session.
_CANDLE_FILE = os.path.join(os.path.dirname(__file__), "..", "data",
                            "candles.json")
_candles = CandleBuilder(timeframe_secs=60, max_bars=500,
                         persist_path=_CANDLE_FILE)
# Dashboard memory: last chain/engine/ballot/decisions + call records.
# Saved every publish, reloaded on boot — restarts and after-hours show
# last-known state instead of emptiness.
_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data",
                           "autotrender_state.json")


def _save_state() -> None:
    """Atomic dump of dashboard state. Never raises."""
    try:
        tmp = _STATE_FILE + ".tmp"
        parent = os.path.dirname(_STATE_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        sched = {}
        for sym, st in (_sched or {}).items():
            try:
                sched[sym] = {"day": st.get("day"), "count": st.get("count", 0),
                              "regime": st.get("regime", "EARLY"),
                              "regime_dir": st.get("regime_dir"),
                              "regime_locked": bool(st.get("regime_locked")),
                              "regime_detail": st.get("regime_detail", []),
                              "scratches": st.get("scratches", 0),
                              "silent": bool(st.get("silent")),
                              "last_bias": st.get("last_bias"),
                              "last_ts": st.get("last_ts", 0.0)}
            except Exception:
                continue
        with open(tmp, "w") as f:
            json.dump({"live": _last_live, "calls": _calls[:50],
                       "next_id": _next_id, "sched": sched}, f, default=str)
        os.replace(tmp, _STATE_FILE)
    except Exception:
        log.exception("dashboard state save failed")


def _restore_state() -> None:
    """Reload dashboard memory on boot. Same-session only. Never raises."""
    global _next_id
    try:
        with open(_STATE_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        live = data.get("live") or {}
        at = str((live.get("engine") or {}).get("at", ""))
        today = _ist_now().strftime("%Y-%m-%d")
        if at[:10] != today:
            log.info("dashboard state: yesterday's snapshot ignored")
            return
        _last_live.update(live)
        _last_live["restored"] = True
        for d in (live.get("decisions") or []):
            if isinstance(d, dict):
                _tick_log.append(d)
        calls = data.get("calls") or []
        if isinstance(calls, list):
            _calls.extend(c for c in calls if isinstance(c, dict))
            del _calls[200:]
        # Regime era: a mid-day restart must NOT relock on a stale window.
        # Restore the locked label + kill state (same-session only).
        sched = data.get("sched") or {}
        if isinstance(sched, dict):
            for sym, snap in sched.items():
                if not isinstance(snap, dict) or snap.get("day") != today:
                    continue
                try:
                    S = _st(str(sym))
                    if S.get("day") is not None and S.get("regime_locked"):
                        continue  # live state already ahead; don't regress
                    S["day"] = snap.get("day")
                    S["count"] = int(snap.get("count", 0) or 0)
                    S["regime"] = snap.get("regime", "EARLY")
                    S["regime_dir"] = snap.get("regime_dir")
                    S["regime_locked"] = bool(snap.get("regime_locked"))
                    S["regime_detail"] = snap.get("regime_detail", [])
                    S["scratches"] = int(snap.get("scratches", 0) or 0)
                    S["silent"] = bool(snap.get("silent"))
                    S["last_bias"] = snap.get("last_bias")
                    S["last_ts"] = float(snap.get("last_ts", 0.0) or 0.0)
                except Exception:
                    continue
        try:
            _next_id = max(int(data.get("next_id", 1)), 1)
            for c in _calls:
                try:
                    _next_id = max(_next_id, int(c.get("id", 0)) + 1)
                except (TypeError, ValueError):
                    pass
        except (TypeError, ValueError):
            pass
        log.info("dashboard state: restored %d calls + last snapshot (%s)",
                 len(_calls), at)
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("dashboard state restore failed")


def _tick_handler(token, ltp, tick):
    """Fold live Kotak ticks into 1-min bars. Sync by design (feed supports it).

    Ticks arriving outside the symbol's session (post-close drips, stale
    replays) are ignored so they can't pollute the tape.
    """
    try:
        sym = (tick or {}).get("symbol", "")
        if sym and _in_session(sym):
            _candles.update(sym, ltp)
    except Exception:
        log.exception("autotrender tick handler failed")


def _sym_root(sym: str) -> str:
    s = (sym or "").upper()
    # Option/future symbols embed expiries+strikes (CRUDEOIL15OCT269000PE);
    # match the leading canonical root (CRUDEOIL).
    return s


def _in_session(sym: str, now: Optional[datetime] = None) -> bool:
    """Session gate per symbol root: MCX commodities trade 09:00-23:30,
    everything else follows the NSE derivatives session."""
    try:
        from .instruments import MCX_CANONICALS
    except Exception:
        MCX_CANONICALS = frozenset()
    root = _sym_root(sym)
    if any(root.startswith(c) for c in MCX_CANONICALS):
        return _in_market_hours(now, (9, 0), (23, 30))
    return _in_market_hours(now)


def _ensure_underlying_sub(cfg: Dict[str, Any]) -> None:
    """Index underlyings arrive via index subs; commodity books need their
    front-month FUT tick subscribed or bars never form. Idempotent."""
    try:
        sym = str(cfg.get("symbol", ""))
        if not sym or sym in ("NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX"):
            return
        if _manager is None:
            return
        feed = getattr(_manager, "market_feed", None)
        cm = getattr(_manager, "contract_master", None)
        if feed is None or cm is None:
            return
        resolved = cm.resolve({"kind": "FUT", "underlying": sym,
                               "exchange_segment": cfg.get("segment", "mcx_fo")})
        if not resolved or not resolved.get("instrument_token"):
            return
        token = str(resolved["instrument_token"])
        subs = getattr(feed, "_subscriptions", {}) or {}
        if token in subs and subs[token].get("symbol") != sym:
            # Already tracking this contract under its dated symbol (other
            # flows subscribe that way) — relabel to canonical so ticks
            # fold into this book's bars. Token routing is unaffected.
            try:
                subs[token]["symbol"] = sym
            except Exception:
                pass
        if token not in subs:
            # Subscribe under the CANONICAL symbol (CRUDEOIL), not the dated
            # futures symbol — ticks then fold into the book's bars. The
            # dated contract stays resolvable via the token everywhere else.
            feed.subscribe_instrument(
                token, sym,
                resolved.get("exchange_segment") or cfg.get("segment", "mcx_fo"))
            log.info("autotrender: subscribed %s futures for %s book",
                     sym, cfg.get("segment"))
    except Exception:
        log.exception("autotrender underlying subscribe failed")


# Track-only equity universe (verified 2026-10-15 liquidity scan).
# EQ feeds + candle books only — no scheduler entry, no fires. The tape
# accumulates here for future per-symbol calibration.
EQUITY_WATCH = ["HDFCBANK", "RELIANCE", "BHARTIARTL", "SBIN", "KOTAKBANK",
                "BSE", "ICICIBANK", "INFY", "AXISBANK", "TITAN", "TCS",
                "ITC", "LT", "M&M"]


def _ensure_watch_subs() -> None:
    """Subscribe the EQUITY_WATCH EQ feeds (idempotent)."""
    try:
        if _manager is None:
            return
        feed = getattr(_manager, "market_feed", None)
        cm = getattr(_manager, "contract_master", None)
        if feed is None or cm is None:
            return
        subs = getattr(feed, "_subscriptions", {}) or {}
        for sym in EQUITY_WATCH:
            try:
                resolved = cm.resolve({"kind": "EQ", "underlying": sym,
                                       "exchange_segment": "nse_cm"})
                if not resolved or not resolved.get("instrument_token"):
                    continue
                token = str(resolved["instrument_token"])
                if token not in subs:
                    feed.subscribe_instrument(token, sym, "nse_cm")
            except Exception:
                continue
    except Exception:
        log.exception("equity watch subscribe failed")


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
    _save_state()
    return record


@router.get("/universe")
async def universe(segment: str = "nse_fo") -> Dict[str, Any]:
    """List distinct option underlyings in a segment with contract counts
    and lot size. Read-only diagnostic for universe scans."""
    import re as _re
    try:
        if _manager is None:
            return {"ok": False, "error": "unwired"}
        cm = getattr(_manager, "contract_master", None)
        if cm is None:
            return {"ok": False, "error": "no contract-master"}
        pool = [c for c in (cm._segments.get(segment, []) or [])
                if cm._opt_of(c) in ("CE", "PE")]
        groups: Dict[str, dict] = {}
        for c in pool:
            sym = cm._sym_of(c) or ""
            m = _re.match(r"^(.*?)(\d{2}[A-Z])", sym)
            und = (m.group(1) if m else sym).upper().rstrip("-_ ")
            g = groups.setdefault(und, {"n": 0, "lots": set()})
            g["n"] += 1
            lot = (c.get("lLotSize", "") or c.get("pLotSize", "")
                   or c.get("lot_size", ""))
            if lot:
                g["lots"].add(str(lot))
        out = [{"underlying": u, "contracts": g["n"],
                "lots": sorted(g["lots"])} for u, g in groups.items()]
        out.sort(key=lambda r: -r["contracts"])
        return {"ok": True, "segment": segment,
                "n_underlyings": len(out), "underlyings": out}
    except Exception as e:
        log.exception("universe failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/chain-probe")
async def chain_probe(underlying: str = "CRUDEOIL", segment: str = "mcx_fo",
                      spot: float = 0, width: int = 5,
                      count: int = 0, expiry: str = "",
                      full: int = 0) -> Dict[str, Any]:
    """Read-only probe: run a chain snapshot for any underlying/segment
    (MCX evenings, BANKNIFTY checks). Returns source/rows/expiry/warnings
    plus sample rows — or the error string. Never trades, never raises."""
    from . import kotak_chain as chain
    try:
        if _manager is None:
            return {"ok": False, "error": "unwired"}
        kotak = getattr(_manager, "kotak", None)
        cm = getattr(_manager, "contract_master", None)
        if kotak is None or cm is None:
            return {"ok": False, "error": "no kotak/contract-master"}
        snap = chain.chain_snapshot(kotak.client, cm, underlying=underlying,
                                    segment=segment,
                                    spot=(spot or None), width=width,
                                    count=(count or 20),
                                    expiry_hint=(expiry or None))
        rows = snap.get("rows", [])
        sample = []
        strikes = []
        if rows:
            sample = [rows[0], rows[len(rows) // 2], rows[-1]]
            strikes = sorted(r.get("strike") for r in rows)
        expiries = []
        try:
            exp = getattr(kotak.client, "expiries", None)
            if exp:
                expiries = exp(exchange=segment, underlying=underlying.upper())
        except Exception:
            pass
        out = {"ok": True, "underlying": snap.get("underlying"),
               "segment": snap.get("segment"), "expiry": snap.get("expiry"),
               "spot": snap.get("spot"), "atm": snap.get("atm"),
               "n_rows": len(rows), "source": snap.get("source"),
               "warnings": snap.get("warnings", []),
               "quote_keys": snap.get("quote_keys", []),
               "strikes": strikes,
               "expiries": expiries,
               "sample": sample}
        if full:
            out["rows"] = rows
        return out
    except Exception as e:
        log.exception("chain probe failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/ticks")
async def live_ticks() -> Dict[str, Any]:
    """Read-only: latest live LTP per feed subscription (indices + legs).

    Independent of engine readiness — ticks flow from boot, so the dashboard
    header shows live prices even while candles warm up. Never trades.
    """
    ticks: List[Dict[str, Any]] = []
    try:
        feed = getattr(_manager, "market_feed", None)
        subs = getattr(feed, "_subscriptions", None) or {}
        for key, info in subs.items():
            if not isinstance(info, dict):
                continue
            try:
                ltp = float(info.get("ltp") or 0)
            except (TypeError, ValueError):
                continue
            if ltp <= 0:
                continue
            ticks.append({"key": str(key),
                          "symbol": str(info.get("symbol", key)),
                          "ltp": ltp,
                          "last_update": str(info.get("last_update") or "")})
    except Exception:
        log.exception("autotrender ticks failed")
    ticks.sort(key=lambda t: t["symbol"])
    return {"ticks": ticks}


@router.get("/candles")
async def live_candles(symbol: str = "NIFTY", n: int = 60) -> Dict[str, Any]:
    """Read-only: recent 1-min bars (closed + forming) for a symbol.

    The same tape the engine decides on — now visible on the dashboard.
    Never trades.
    """
    sym = (symbol or "NIFTY").upper().strip()
    n = max(1, min(n, 500))
    try:
        bars = _candles.get_candles(sym, n=n)
        return {"symbol": sym, "timeframe_secs": _candles.timeframe,
                "bars": bars, "count": len(bars)}
    except Exception:
        log.exception("autotrender candles failed")
        return {"symbol": sym, "bars": [], "count": 0}


@router.get("/calls")
async def list_calls(limit: int = 50) -> Dict[str, Any]:
    return {"calls": _calls[: max(1, min(limit, 200))]}


@router.get("/debug/memory")
async def debug_memory() -> Dict[str, Any]:
    """Memory-hunt endpoint: RSS + tracemalloc diff between calls.

    First hit starts tracing (baseline). Later hits return top growers.
    Temporary instrument — remove once the leak is found. Never trades.
    """
    import gc
    import resource
    import tracemalloc
    from collections import Counter
    try:
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        rss_kb = 0
    out: Dict[str, Any] = {"rss_mb": round(rss_kb / 1024, 1)}
    try:
        if not tracemalloc.is_tracing():
            tracemalloc.start(10)
            out["tracing"] = "started baseline — hit again in 20+ min"
            return out
        snap = tracemalloc.take_snapshot()
        prev = getattr(debug_memory, "_prev", None)
        setattr(debug_memory, "_prev", snap)
        if prev is None:
            out["tracing"] = "baseline set — hit again in 20+ min"
            return out
        top = []
        for stat in snap.compare_to(prev, "traceback")[:12]:
            frame = stat.traceback[0] if stat.traceback else None
            top.append({"size_kb": round(stat.size_diff / 1024, 1),
                        "count": stat.count_diff,
                        "where": f"{frame.filename.split('/')[-1]}:"
                                 f"{frame.lineno}" if frame else "?"})
        out["growers_kb"] = top
        counts = Counter(
            f"{str(getattr(type(o), '__module__', '?'))}."
            f"{str(getattr(type(o), '__name__', '?'))}"
            for o in gc.get_objects(generation=2)[:2000000])
        out["top_types"] = [[k, v] for k, v in counts.most_common(15)]
        out["gc_counts"] = list(gc.get_count())
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


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
    ns = _st("NIFTY")
    return {"symbols": out,
            "scheduler": {"running": _sched_task is not None,
                          "last_bias": ns.get("last_bias"),
                          "auto_today": ns.get("count", 0),
                          "last_chain_keys": ns.get("chain_keys", [])}}


@router.get("/live")
async def live_snapshot() -> Dict[str, Any]:
    """Live engine picture for the dashboard.

    Chain snapshot publishes on every in-hours tick (even without a trigger),
    so ready=True from the first successful snapshot. The "engine" block holds
    the why behind the latest tick — trend, trigger, fusion verdict, outcome.
    Non-dashboard books (MCX evenings) nest under symbols/ regardless of the
    NIFTY ready flag.
    """
    out = dict(_last_live)
    out["ready"] = bool(_last_live.get("ready"))
    out["scheduler_running"] = _sched_task is not None
    return out


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

SCHED_INTERVAL_SECS = 60        # evaluate every minute (live-test cadence)
SCHED_COOLDOWN_SECS = 900       # min gap between auto calls (same bias)
SCHED_MAX_PER_DAY = 20          # legacy safety cap (kept for compat)
SCHED_MAX_FIRES_PER_DAY = 3     # regime era: frequency never saved a bad regime read
SCHED_SCRATCH_FRAC = 0.0015     # adverse close excursion marking a scratch
SCHED_MAX_SCRATCHES = 2         # scratches -> silent rest of day (any regime)
REGIME_LOCK_BARS = 45           # morning label locks when the tape is this long
REGIME_RECHECK_SECS = 900       # re-verify a trend-day thesis this often
REGIME_MIN_DENSITY = 0.5        # bars per session-minute; thinner tapes are
                                # unreadable longer -> chop rules while EARLY
SCHED_SYMBOL = "NIFTY"
SCHED_WIDTH = 9                 # strikes each side of ATM for the snapshot
# No fresh entries after the cash closing auction takes over (15:15) — the
# engine keeps publishing its read, but fusion becomes exit-management only.
# Derivatives now trade to 15:40 (NSE Aug-2026 extension), so data flows on.
ENTRY_CUTOFF_HM = (15, 15)
CLOSE_HM = (15, 40)

# Engine universe. NIFTY is the primary book (dashboard); MCX crude runs
# evenings on the same v2 models with commodity-appropriate chain windows.
# lot=None means the engine default (NIFTY lot); dashboard=True publishes
# to the top-level dashboard shape, others nest under live["symbols"].
NIFTY_CFG = {"symbol": "NIFTY", "segment": "nse_fo",
             "open": (9, 15), "close": CLOSE_HM, "cutoff": ENTRY_CUTOFF_HM,
             "width": SCHED_WIDTH, "count": 20, "lot": None,
             "dashboard": True, "vix": True}
CRUDE_CFG = {"symbol": "CRUDEOIL", "segment": "mcx_fo",
             "open": (9, 0), "close": (23, 30), "cutoff": (23, 0),
             "width": 5, "count": 120, "lot": 100,
             "dashboard": False, "vix": False}
SYMBOL_CFGS = [NIFTY_CFG, CRUDE_CFG]

_sched_task = None
# Per-symbol scheduler state (cooldown/cap/prev-snapshot are per book).
_sched: Dict[str, Dict[str, Any]] = {}


def _st(sym: str) -> Dict[str, Any]:
    return _sched.setdefault(sym, {
        "day": None, "count": 0, "last_ts": 0.0, "last_bias": None,
        "last_breakout": False, "chain_keys": [], "prev_rows": [],
        "prev_vix": None, "log": _deque(maxlen=120),
        # Regime era: morning label + kill rules (reset daily below).
        "regime": "EARLY", "regime_dir": None, "regime_locked": False,
        "regime_checked_ts": 0.0, "scratches": 0, "silent": False,
        "fires": [],
    })
# Last successful live snapshot for the dashboard (GET /api/autotrender/live).
_last_live: Dict[str, Any] = {}
# Rolling per-tick decision log for the dashboard ballot feed (newest last).
from collections import deque as _deque
_tick_log: _deque = _deque(maxlen=120)


def _ballot(trend=None, trig=None, vol=None, pos=None, fused=None,
            v2=None, found=None, regime=None) -> List[Dict[str, Any]]:
    """Per-model votes for the latest tick: every indicator lists ALL its
    options plus the one it picked. [{model, options, vote, detail}] — never
    raises. The dashboard highlights vote within options."""
    rows: List[Dict[str, Any]] = []
    def _row(model, options, vote, detail=""):
        rows.append({"model": model, "options": options, "vote": vote,
                     "detail": detail})
    def _signed(x):
        try:
            return f"{int(x):+d}"
        except (TypeError, ValueError):
            return "—"
    try:
        rg = regime or {}
        _row("Regime", ["TREND day", "CHOP", "EARLY", "SILENT"],
             ("SILENT" if rg.get("silent") else
              {"TREND-DAY-CANDIDATE": "TREND day", "CHOP": "CHOP"}.get(
                  rg.get("label"), "EARLY")),
             f"{rg.get('label')} dir={rg.get('direction')} "
             f"scratches={rg.get('scratches', 0)}")
        tscore = int((trend or {}).get("score", 0))
        parts = (trend or {}).get("parts", {})
        _row("Trend EMA/VWAP", ["BULL", "BEAR", "FLAT"],
             "BULL" if tscore > 0 else ("BEAR" if tscore < 0 else "FLAT"),
             f"score={tscore} {parts}")
        rsi = (found or {}).get("rsi")
        _row("RSI-14", ["BULL (>60)", "BEAR (<40)", "NEUTRAL"],
             ("BULL (>60)" if (rsi or 50) > 60 else
              ("BEAR (<40)" if (rsi or 50) < 40 else "NEUTRAL")),
             f"rsi={round(rsi, 1) if rsi is not None else '—'}")
        _row("Trigger", ["FIRED BULLISH", "FIRED BEARISH", "quiet"],
             (f"FIRED {(trig or {}).get('direction')}"
              if (trig or {}).get("fire") else "quiet"),
             str((trig or {}).get("reason", "")))
        _row("Volatility gate", ["OK", "CHOP"],
             "OK" if (vol or {}).get("ok") else "CHOP",
             str((vol or {}).get("reason", "")))
        v2 = v2 or {}
        st = v2.get("strength") or {}
        _row("ADX strength", ["TREND (≥15)", "RANGE"],
             "TREND (≥15)" if st.get("trending") else "RANGE",
             f"adx={st.get('adx')} di+={st.get('plus_di')} di-={st.get('minus_di')}")
        htf = v2.get("htf") or {}
        _row("HTF 15m", ["BULL", "BEAR", "FLAT (abstain)"],
             ({"BULL": "BULL", "BEAR": "BEAR"}.get(htf.get("bias"), "FLAT (abstain)")),
             f"tf_bars={htf.get('tf_bars')}")
        hx = v2.get("htf_exhaustion") or {}
        _row("HTF RSI", ["OVERBOUGHT (≥70)", "OVERSOLD (≤30)", "normal", "— (abstain)"],
             ({"OVERBOUGHT": "OVERBOUGHT (≥70)",
               "OVERSOLD": "OVERSOLD (≤30)"}.get(hx.get("extreme"),
                 "— (abstain)" if hx.get("rsi") is None else "normal")),
             f"rsi15={hx.get('rsi')}")
        ex = v2.get("exhaustion") or {}
        _row("VWAP stretch", ["CLIMAX", "ok"],
             "CLIMAX" if ex.get("stretched") else "ok",
             f"z={ex.get('z')} climax={ex.get('climax')}")
        et = v2.get("extension") or {}
        _row("Day extension", ["SPENT", "ok"],
             "SPENT" if et.get("vetoed") else "ok",
             f"ratio={et.get('ratio')}")
        _row("PCR positioning", ["+1", "0", "-1"],
             _signed((pos or {}).get("score")),
             str((pos or {}).get("reason", "")))
        peff = v2.get("positioning_eff") or {}
        if peff:
            _row("Positioning composite", ["+1", "0", "-1"],
                 _signed(peff.get("score")),
                 str(peff.get("reason", "")))
        stw = v2.get("supertrend") or {}
        _row("Supertrend", ["UP", "DOWN", "—"],
             str(stw.get("direction") or "—"),
             f"value={stw.get('value')}")
        fl = v2.get("oi_flow") or {}
        _row("OI flow", ["+1", "0", "-1", "waiting"],
             _signed(fl.get("score")) if fl else "waiting",
             str(fl.get("reason", "")) if fl else "needs trigger tick")
        sk = v2.get("iv_skew") or {}
        _row("IV skew", ["+1", "0", "-1", "waiting"],
             _signed(sk.get("score")) if sk else "waiting",
             str(sk.get("reason", "")) if sk else "needs trigger tick")
        sd = v2.get("straddle") or {}
        try:
            from .at_models import STRADDLE_CRUSH_PCT as _crush
        except Exception:
            _crush = 5.0
        _row("Straddle", ["EXPAND", "CRUSH", "—", "waiting"],
             (("CRUSH" if sd.get("expanding") is False and
               abs(sd.get("chg_pct") or 0) > _crush else
               ("EXPAND" if sd.get("expanding") else "—")) if sd else "waiting"),
             f"chg={sd.get('chg_pct')}%" if sd else "needs trigger tick")
        vx = v2.get("vix") or {}
        _row("VIX", ["panic", "calm", "normal", "unknown", "waiting"],
             str(vx.get("state", "—")) if vx else "waiting",
             f"vix={vx.get('vix')}" if vx else "needs trigger tick")
        f = fused or {}
        if not f:
            _row("DECISION", ["FIRED", "BLOCKED", "QUIET"], "QUIET",
                 f"no fusion — {str((trig or {}).get('reason', ''))}")
        else:
            _row("DECISION", ["FIRED", "BLOCKED", "QUIET"],
                 "FIRED" if f.get("fire") else "BLOCKED",
                 f"{f.get('direction')} score={f.get('score')} vetoes={f.get('vetoes', [])}")
    except Exception:
        log.exception("ballot build failed")
    return rows


def _publish_engine(moment, outcome: str, bars=None, found=None, trend=None,
                    trig=None, vol=None, pos=None, fused=None, v2=None,
                    S=None, pub=None, hist=None) -> None:
    """Record the why behind every tick (fire AND silence) for the dashboard.

    Chain data (when available) is published separately by _publish_chain;
    this block is updated on EVERY in-hours tick so "working but quiet" is
    distinguishable from "broken". S is the symbol's scheduler state;
    pub/hist route non-dashboard symbols to their nested block. Never raises.
    """
    try:
        from datetime import timezone as _tz
        from collections import deque as _dq
        if pub is None:
            pub = _last_live
        if hist is None:
            hist = _tick_log
        if S is None:
            S = {"last_bias": None, "last_ts": 0.0, "count": 0}
        now_ts = (moment or datetime.now(_tz.utc)).timestamp()
        remaining = 0
        if S.get("last_bias") and S.get("last_ts"):
            remaining = max(0, int(SCHED_COOLDOWN_SECS - (now_ts - S["last_ts"])))
        pub["engine"] = {
            "at": _ist_now(moment).isoformat(timespec="seconds"),
            "outcome": outcome,
            "bars": len(bars) if bars else 0,
            "rsi": (found or {}).get("rsi"),
            "trend": trend, "trigger": trig, "volatility": vol,
            "positioning": pos, "fusion": fused, "v2": v2,
            "ballot": _ballot(trend, trig, vol, pos, fused, v2, found,
                              regime={"label": S.get("regime", "EARLY"),
                                      "direction": S.get("regime_dir"),
                                      "scratches": S.get("scratches", 0),
                                      "silent": S.get("silent", False)}),
            "cooldown": {"last_bias": S.get("last_bias"),
                          "remaining_secs": remaining},
            "auto_today": S.get("count", 0),
            "regime": {"label": S.get("regime", "EARLY"),
                       "direction": S.get("regime_dir"),
                       "detail": S.get("regime_detail", []),
                       "scratches": S.get("scratches", 0),
                       "silent": S.get("silent", False)},
        }
        if not isinstance(hist, _dq):
            hist = _dq(list(hist or [])[-120:], maxlen=120)
        hist.append({
            "at": _ist_now(moment).isoformat(timespec="seconds"),
            "outcome": outcome,
            "fire": bool((fused or {}).get("fire")),
            "direction": (fused or {}).get("direction") or (trig or {}).get("direction"),
            "score": (fused or {}).get("score"),
            "vetoes": list((fused or {}).get("vetoes") or []),
        })
        pub["decisions"] = list(hist)
        if pub is _last_live:
            pub.pop("restored", None)
            _save_state()
    except Exception:
        log.exception("publish engine state failed")


def _publish_chain(moment, spot: float, snap: Dict[str, Any],
                   pub=None, spot_label: str = "NIFTY 50") -> None:
    """Cache the live chain picture for the dashboard.

    Called on EVERY successful snapshot — even when no trigger fires — so
    the dashboard lights up in quiet markets too. Non-dashboard symbols
    publish into their nested block via pub.
    """
    if pub is None:
        pub = _last_live
    summ = engine.summarize(snap["rows"])
    sr = engine.get_support_resistance(snap["rows"])
    pub.update({
        "ready": True, "at": _ist_now(moment).isoformat(timespec="seconds"),
        "spot": {"symbol": spot_label, "price": spot},
        "expiry": snap.get("expiry"), "atm": snap.get("atm"),
        "rows": snap["rows"], "pcr": summ["pcr"], "pcrChg": summ["pcrChg"],
        "support": sr["support"], "resistance": sr["resistance"],
        "maxPain": engine.max_pain(snap["rows"]),
        "straddle": engine.straddle(snap["rows"]),
        "quote_keys": snap.get("quote_keys", []),
        "chain_source": snap.get("source", "unknown"),
    })
    if pub is _last_live:
        pub.pop("restored", None)
        _save_state()


def _reset_scheduler_state(sym: Optional[str] = None) -> None:
    """Test-only: clear cooldown/cap state (cooldown + daily cap)."""
    global _sched
    if sym is None:
        _sched = {}
    else:
        _sched.pop(sym, None)
    _tick_log.clear()
    _last_live.clear()


def _ist_now(now: Optional[datetime] = None) -> datetime:
    from zoneinfo import ZoneInfo
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Kolkata"))


def _in_market_hours(now: Optional[datetime] = None,
                     open_hm: tuple = (9, 15),
                     close_hm: tuple = CLOSE_HM) -> bool:
    t = _ist_now(now).time()
    return (t.hour, t.minute) >= open_hm and (t.hour, t.minute) < close_hm


def _past_entry_cutoff(now: Optional[datetime] = None,
                       cutoff_hm: tuple = ENTRY_CUTOFF_HM) -> bool:
    t = _ist_now(now).time()
    return (t.hour, t.minute) >= cutoff_hm


def _update_regime(S: Dict[str, Any], bars, now_ts: float) -> Dict[str, Any]:
    """Lock the morning label at REGIME_LOCK_BARS, recheck trend theses.

    Before the lock the day is unreadable -> regime stays EARLY (old rules).
    UNDECIDED mornings are treated as chop (conservative). Never raises.
    """
    from . import day_regime as rg
    try:
        if not S.get("regime_locked") and len(bars) >= REGIME_LOCK_BARS:
            out = rg.classify(bars[:REGIME_LOCK_BARS])
            label = out["label"]
            if label == "UNDECIDED":
                label = "CHOP"
            S["regime"] = label
            S["regime_dir"] = {"UP": "BULLISH", "DOWN": "BEARISH"}.get(
                out["parts"].get("direction"))
            S["regime_locked"] = True
            S["regime_checked_ts"] = now_ts
            S["regime_detail"] = out["reasons"]
        elif (S.get("regime_locked") and S.get("regime") == "TREND-DAY-CANDIDATE"
                and now_ts - S.get("regime_checked_ts", 0.0) >= REGIME_RECHECK_SECS):
            out = rg.recheck("TREND-DAY-CANDIDATE", S.get("regime_dir"), bars)
            S["regime_checked_ts"] = now_ts
            if out["flip"]:
                S["regime"] = "CHOP"
                S["regime_dir"] = None
                S["regime_detail"] = [out["reason"]]
    except Exception:
        log.exception("regime update failed")
    return {"label": S.get("regime", "EARLY"), "direction": S.get("regime_dir")}


def _judge_fires(S: Dict[str, Any], bars) -> None:
    """Mark old fires scratched when price went against them (scratch rule).

    Two scratches silence the book for the day. Never raises.
    """
    try:
        closes = [b.get("c", 0) or 0 for b in bars]
        for f in S.get("fires", []):
            if f.get("judged"):
                continue
            idx, entry, direction = f["bar_idx"], f["entry"], f["direction"]
            window = closes[idx + 1: idx + 31]
            if not window:
                continue
            moves = ([c - entry for c in window] if direction == "BULLISH"
                     else [entry - c for c in window])
            adverse = min(moves)
            done = len(window) >= 30
            if adverse <= -abs(entry) * SCHED_SCRATCH_FRAC:
                f["judged"] = f["scratched"] = True
                S["scratches"] = S.get("scratches", 0) + 1
            elif done:
                f["judged"] = True
            if S.get("scratches", 0) >= SCHED_MAX_SCRATCHES:
                S["silent"] = True
    except Exception:
        log.exception("scratch judging failed")


def _reentry_pick(S: Dict[str, Any], bars) -> Optional[Dict[str, Any]]:
    """Most recent scratched fire whose level was reclaimed + held.

    One second chance per fire (reentered/is_reentry never re-qualify).
    Returns {"direction", "reason", "fire"} or None. Never raises.
    """
    from . import day_regime as rg
    try:
        for f in reversed(S.get("fires", [])):
            if not (f.get("judged") and f.get("scratched")):
                continue
            if f.get("reentered") or f.get("is_reentry"):
                continue
            setup = rg.reentry_setup(bars, f.get("entry"),
                                     f.get("direction"))
            if setup["setup"]:
                return {"direction": f["direction"],
                        "reason": setup["reason"], "fire": f}
    except Exception:
        log.exception("re-entry pick failed")
    return None


def _regime_gate(S: Dict[str, Any], trig, bars,
                 now_ts: float = 0.0,
                 open_hm: tuple = (9, 15)) -> Dict[str, Any]:
    """Regime-era entry permission (pure logic over S + trigger + tape).

    Returns {"allowed": bool, "reason": str, "squeeze": dict}.
    """
    from . import day_regime as rg
    direction = (trig or {}).get("direction")
    label = S.get("regime", "EARLY")
    if S.get("silent"):
        return {"allowed": False,
                "reason": f"silent: {S.get('scratches', 0)} scratches today",
                "squeeze": {}}
    if label == "EARLY":
        # Thin tape, unreadable morning: a bar every few minutes means the
        # "morning" never really formed — demand chop-grade proof (reclaim),
        # not free fusion. Dense tapes (a bar a minute) keep old behavior.
        # now_ts==0 (tests/legacy callers): density unknown -> old behavior.
        if now_ts:
            try:
                from datetime import timezone as _tz
                open_ts = _ist_now(datetime.fromtimestamp(now_ts, _tz.utc)
                                   if now_ts else None).replace(
                    hour=open_hm[0], minute=open_hm[1],
                    second=0, microsecond=0).timestamp()
                elapsed_min = max(1.0, (now_ts - open_ts) / 60.0)
            except Exception:
                elapsed_min = float(len(bars) or 1)
            density = len(bars) / elapsed_min
            if density < REGIME_MIN_DENSITY:
                rc = rg.reclaim(bars)
                if not rc["setup"] or rc["side"] != direction:
                    return {"allowed": False,
                            "reason": f"thin tape ({len(bars)} bars in "
                                      f"{elapsed_min:.0f}min): no {direction} reclaim",
                            "squeeze": rg.squeeze(bars)}
                return {"allowed": True,
                        "reason": f"thin tape A+: {rc['reason']}",
                        "squeeze": rg.squeeze(bars)}
        return {"allowed": True, "reason": "early session: day unreadable",
                "squeeze": {}}
    if label == "TREND-DAY-CANDIDATE":
        want = S.get("regime_dir")
        if want and direction != want:
            return {"allowed": False,
                    "reason": f"against day regime ({want} day, {direction} trigger)",
                    "squeeze": {}}
        return {"allowed": True,
                "reason": f"trend day {want}: confirmation allowed",
                "squeeze": {}}
    # CHOP (and anything else): reclaim-only. Enter the TEST, not the spike.
    rc, sq = rg.reclaim(bars), rg.squeeze(bars)
    if not rc["setup"] or rc["side"] != direction:
        return {"allowed": False,
                "reason": f"chop: no {direction} reclaim ({rc['reason']})",
                "squeeze": sq}
    extra = " + coiled" if sq.get("coiled") else ""
    return {"allowed": True,
            "reason": f"chop A+: {rc['reason']}{extra}", "squeeze": sq}


async def scheduler_tick(now: Optional[datetime] = None,
                         builder: Optional[CandleBuilder] = None,
                         cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One scheduler evaluation for one symbol book. Never raises."""
    from . import kotak_chain as chain
    cfg = cfg or NIFTY_CFG
    sym = str(cfg.get("symbol", "NIFTY"))
    S = _st(sym)
    b = builder or _candles
    moment = now or datetime.now(timezone.utc)
    if _manager is None:
        return {"action": "skip", "reason": "unwired"}
    if getattr(_manager, "mode", "paper") != "paper":
        return {"action": "skip", "reason": "not paper mode"}
    if getattr(_manager, "stop_trading", False):
        return {"action": "skip", "reason": "stop trading on"}
    if not _in_market_hours(moment, cfg.get("open", (9, 15)),
                            cfg.get("close", CLOSE_HM)):
        return {"action": "skip", "reason": "outside market hours"}

    day = _ist_now(moment).strftime("%Y-%m-%d")
    if S["day"] != day:
        S["day"], S["count"] = day, 0
        S["regime"], S["regime_dir"] = "EARLY", None
        S["regime_locked"], S["regime_checked_ts"] = False, 0.0
        S["scratches"], S["silent"], S["fires"] = 0, False, []
    # No count caps by design: the score bar (75+) is the only quota.
    # S["count"] is kept for stats only.

    _ensure_underlying_sub(cfg)
    bars = b.get_candles(sym, n=400)
    # Dashboard symbols publish top-level; others nest under live["symbols"].
    if cfg.get("dashboard"):
        pub, hist = _last_live, _tick_log
    else:
        pub = _last_live.setdefault("symbols", {}).setdefault(sym, {})
        hist = S["log"]
    if len(bars) < 20:
        _publish_engine(moment, f"warming up ({len(bars)}/20 bars)", bars=bars,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip", "reason": f"warming up ({len(bars)}/20 bars)"}
    # Regime era: lock the morning label, recheck trend theses, judge old
    # fires (scratch rule). All additive — EARLY sessions behave as before.
    regime = _update_regime(S, bars, moment.timestamp())
    _judge_fires(S, bars)
    try:
        found = engine.detect(bars)
    except Exception:
        log.exception("scheduler detect failed")
        _publish_engine(moment, "detect error", bars=bars,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip", "reason": "detect error"}
    # Free local models first (no Kotak calls): trend + trigger + volatility
    # plus the v2 tape models (strength/HTF/exhaustion/extension).
    from . import at_models as models
    vwap = models.session_vwap(bars)
    trend = models.trend_score(bars, vwap)
    trig = models.trigger_ok(found, bars)
    vol = models.volatility_gate(bars)
    strength = models.trend_strength(bars)
    htf = models.higher_tf_bias(bars)
    exh = models.exhaustion(bars, vwap)
    ext = models.extension_check(bars)
    htfx = models.htf_exhaustion(bars)
    stw = models.supertrend_witness(bars)
    v2ctx = {"strength": strength, "htf": htf, "exhaustion": exh,
             "extension": ext, "htf_exhaustion": htfx, "supertrend": stw}

    # Chain snapshot BEFORE the trigger gate: the dashboard must light up in
    # quiet markets too, and positioning needs rows even when we don't fire.
    kotak = getattr(_manager, "kotak", None)
    cm = getattr(_manager, "contract_master", None)
    if kotak is None or cm is None or not getattr(kotak, "is_authenticated", False):
        _publish_engine(moment, "kotak not authenticated", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol, v2=v2ctx,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip", "reason": "kotak not authenticated"}
    spot = bars[-1]["c"]
    try:
        snap = chain.chain_snapshot(kotak.client, cm, underlying=sym,
                                    segment=cfg.get("segment", "nse_fo"),
                                    spot=spot, width=cfg.get("width", 9),
                                    count=cfg.get("count", 20))
    except Exception:
        log.exception("scheduler chain snapshot failed")
        _publish_engine(moment, "chain snapshot failed", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol, v2=v2ctx,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip", "reason": "chain snapshot failed"}
    S["chain_keys"] = snap.get("quote_keys", [])
    _publish_chain(moment, spot, snap, pub=pub,
                   spot_label={"NIFTY": "NIFTY 50"}.get(sym, sym))

    # v2 positioning models run on EVERY snapshot tick — not just trigger
    # ticks — so the dashboard ballot always shows the full picture.
    # Only oi_flow needs a trigger direction; everything else is free.
    pos = models.positioning(snap["rows"], spot, trig["direction"])
    try:
        from datetime import date as _date
        dte = (_date.fromisoformat(str(snap.get("expiry"))) -
               _ist_now(moment).date()).days
    except Exception:
        dte = 5
    flow = models.oi_flow(S["prev_rows"], snap["rows"], spot, trig["direction"])
    skew = models.iv_skew(snap["rows"], spot, dte)
    strad = models.straddle_momentum(S["prev_rows"], snap["rows"])
    vix_now = None
    if cfg.get("vix"):
        try:
            feed = getattr(_manager, "market_feed", None)
            subs = getattr(feed, "_subscriptions", {}) or {}
            vix_now = float((subs.get("IDX:INDIAVIX") or {}).get("ltp") or 0) or None
        except Exception:
            vix_now = None
    vixr = models.vix_regime(vix_now, S["prev_vix"])
    S["prev_rows"] = snap["rows"]
    S["prev_vix"] = vix_now
    pos_eff = models.composite_positioning(pos, flow, skew, trig["direction"])
    v2ctx.update({"positioning": pos, "positioning_eff": pos_eff,
                  "oi_flow": flow, "iv_skew": skew,
                  "straddle": strad, "vix": vixr})

    if not trig["fire"]:
        # Re-entry: a scratched fire whose level was reclaimed + held earns
        # a second chance WITHOUT needing a fresh spike. The synthetic
        # trigger carries the direction; fusion, regime gate, cap and
        # cutoff below all still apply — only the spike requirement
        # (and cooldown) is waived.
        pick = _reentry_pick(S, bars)
        if pick is None:
            _publish_engine(moment, f"no trigger ({trig['reason']})", bars=bars,
                            found=found, trend=trend, trig=trig, vol=vol, pos=pos, v2=v2ctx,
                            S=S, pub=pub, hist=hist)
            return {"action": "skip", "reason": f"no trigger ({trig['reason']})"}
        trig = {"fire": True, "direction": pick["direction"],
                "reason": f"re-entry: {pick['reason']}", "_pick": pick}
    else:
        pick = _reentry_pick(S, bars)
        if pick is not None and pick["direction"] != trig["direction"]:
            pick = None
    if not vol["ok"]:
        _publish_engine(moment, f"volatility veto ({vol['reason']})", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol, pos=pos, v2=v2ctx,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip", "reason": f"volatility veto ({vol['reason']})"}

    # Regime gate: trend days confirm (one direction), chop days reclaim-only,
    # 3 fires/day, 2 scratches -> silent. Runs before fusion/cooldown.
    gate = _regime_gate(S, trig, bars, moment.timestamp(),
                        cfg.get("open", (9, 15)))
    if not gate["allowed"]:
        _publish_engine(moment, f"regime gate: {gate['reason']}", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol, pos=pos, v2=v2ctx,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip", "reason": f"regime gate: {gate['reason']}"}

    ts = moment.timestamp()
    # Cooldown suppresses same-drift repeats, but a FRESH breakout episode
    # jumps the queue once — new information, not a repeat. Persistent
    # breakouts (same episode as the last fired call) still wait.
    fresh_breakout = (trig["direction"] == "BULLISH" and "breakout=HIGH" in trig["reason"]) or \
                     (trig["direction"] == "BEARISH" and "breakout=LOW" in trig["reason"])
    if trig["direction"] == S["last_bias"] and ts - S["last_ts"] < SCHED_COOLDOWN_SECS \
            and not (fresh_breakout and not S["last_breakout"]) \
            and pick is None:
        _publish_engine(moment, "cooldown", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol, v2=v2ctx,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip", "reason": "cooldown"}

    # Positioning already computed above for every tick; fuse for the vote.
    fused_v1 = models.fuse(trend, trig, pos, vol)
    fused = models.fuse_v2(trend, trig, pos_eff, vol, strength, htf, exh, ext,
                           htfx, flow, skew, strad, vixr, stw)
    if not fused["fire"]:
        _publish_engine(moment, f"fusion blocked {fused['vetoes']} (score={fused['score']})",
                        bars=bars, found=found, trend=trend, trig=trig,
                        vol=vol, pos=pos, fused=fused, v2=v2ctx,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip",
                "reason": f"fusion blocked {fused['vetoes']} (score={fused['score']})"}
    direction = fused["direction"]

    # Session-entry rule: the read stays live, but no fresh entries past the
    # book's cutoff — follow-through can't be trusted near the bell.
    if _past_entry_cutoff(moment, cfg.get("cutoff", ENTRY_CUTOFF_HM)):
        _publish_engine(moment, "entry cutoff (exit-only zone)",
                        bars=bars, found=found, trend=trend, trig=trig,
                        vol=vol, pos=pos, fused=fused, v2=v2ctx,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip", "reason": "entry cutoff (exit-only zone)"}

    a = engine.atr(bars, 14)
    lv = engine.entry_exit(bars[-1], a, direction)
    if lv["entry"] is None:
        _publish_engine(moment, "no levels", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol,
                        pos=pos, fused=fused, v2=v2ctx,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip", "reason": "no levels"}
    lot = cfg.get("lot")
    oc_kwargs: Dict[str, Any] = {}
    if lot is not None:
        oc_kwargs["lot"] = lot
    details = engine.option_call_details(snap["rows"], spot, direction,
                                         lv["entry"], lv["sl"], lv["tgt"],
                                         **oc_kwargs)
    if details["side"] == "NO TRADE":
        _publish_engine(moment, "no strike", bars=bars,
                        found=found, trend=trend, trig=trig, vol=vol,
                        pos=pos, fused=fused, v2=v2ctx,
                        S=S, pub=pub, hist=hist)
        return {"action": "skip", "reason": "no strike"}
    try:
        model = AutotrenderCallIn(underlying=sym, side=details["side"],
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
                       "v1_score": fused_v1["score"],
                       "direction": direction, "trend": trend,
                       "trigger": trig["reason"], "positioning": pos,
                       "volatility": vol, "v2": v2ctx}
    S["last_ts"], S["last_bias"] = ts, direction
    S["last_breakout"] = fresh_breakout
    S["count"] += 1
    if pick is not None and pick["direction"] == direction:
        pick["fire"]["reentered"] = True
    S["fires"].append({"bar_idx": len(bars) - 1, "entry": spot,
                       "direction": direction, "judged": False,
                       "reentered": False,
                       "is_reentry": bool(pick is not None and
                                          pick["direction"] == direction)})
    _publish_engine(moment, f"generated {direction} {details['side']} {details['strike']}",
                    bars=bars, found=found, trend=trend, trig=trig, vol=vol,
                    pos=pos, fused=fused, v2=v2ctx,
                    S=S, pub=pub, hist=hist)
    return {"action": "generated", "call": record}


async def _scheduler_loop() -> None:
    import asyncio
    while True:
        try:
            await asyncio.sleep(SCHED_INTERVAL_SECS)
            try:
                _ensure_watch_subs()
            except Exception:
                pass
            for cfg in SYMBOL_CFGS:
                try:
                    result = await scheduler_tick(cfg=cfg)
                    if result.get("action") != "skip":
                        log.info("scheduler [%s]: %s", cfg.get("symbol"),
                                 result.get("action"))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("scheduler [%s] tick error",
                                  cfg.get("symbol"))
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


# Boot-time restore (runs once at import, after all module state exists).
_restore_state()
