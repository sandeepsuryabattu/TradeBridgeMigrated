"""
Candle builder — accumulates live Kotak LTP ticks into OHLC bars.

Ticks carry LTP but no reliable per-tick volume, so `v` counts ticks in the
bar (activity proxy). The engine's volume-spike check therefore means an
activity spike — documented, not silent.

Closed bars persist to disk (JSON, atomic write) so a bot restart intraday
reloads the same session instead of rewarming from zero. Bars older than
today (IST) are dropped on load — yesterday's tape must never poison today's
trend/trigger math.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_KEEP_BARS = 150  # per symbol on disk — plenty for the 60-bar engine window

# Single-tick jump limits (fraction of last accepted LTP). A 2-sec NIFTY
# tick never legitimately jumps 0.3%; reconnect replays do (2026-10-14:
# stale 22491 print inside a 22650 market). Derivatives/VIX get room.
_INDEX_JUMP_LIMIT = 0.003
_VIX_JUMP_LIMIT = 0.05
_DEFAULT_JUMP_LIMIT = 0.10
_GAP_MULT = 2  # idle longer than this × timeframe marks the next bar gapped


def _jump_limit(symbol: str) -> float:
    s = (symbol or "").upper()
    if any(ch.isdigit() for ch in s):
        return _DEFAULT_JUMP_LIMIT  # derivative leg
    if "VIX" in s:
        return _VIX_JUMP_LIMIT
    return _INDEX_JUMP_LIMIT


def _today_ist_midnight_epoch() -> float:
    try:
        from zoneinfo import ZoneInfo
        now_ist = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata"))
        midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()
    except Exception:
        return time.time() - 24 * 3600  # fail-closed: keep only last 24h


class CandleBuilder:
    """Per-symbol timeframe bars. Closed bars persist across restarts."""

    def __init__(self, timeframe_secs: int = 60, max_bars: int = 500,
                 persist_path: Optional[str] = None):
        if timeframe_secs <= 0:
            raise ValueError("timeframe_secs must be positive")
        self.timeframe = timeframe_secs
        self.max_bars = max_bars
        self._closed: dict[str, list[dict]] = {}
        self._open: dict[str, dict] = {}
        self._last_ltp: dict[str, float] = {}
        self._last_ts: dict[str, float] = {}
        self.rejected_ticks = 0
        self._persist_path = persist_path
        if persist_path:
            self._load()

    def update(self, symbol: str, ltp: float, ts: Optional[float] = None) -> Optional[dict]:
        """Fold one tick into its bar. Returns the closed bar when one rolls over.

        Stale-tick guard: a tick jumping further than the symbol's limit
        from the last ACCEPTED tick is dropped (reconnect replays) and
        counted in `rejected_ticks`. A fresh bar opened after a long idle
        carries `"gap": True` so the engine can distrust its OHLC.
        """
        sym = (symbol or "").upper().strip()
        if not sym or ltp is None or ltp <= 0:
            return None
        try:
            px = float(ltp)
        except (TypeError, ValueError):
            return None
        ts = ts if ts is not None else time.time()
        last = self._last_ltp.get(sym)
        prev_ts = self._last_ts.get(sym)
        # No comparison across long idles: overnight gaps are real, and
        # yesterday's close must never veto today's open tick.
        fresh_basis = (last and last > 0 and prev_ts is not None
                       and ts - prev_ts <= 15 * 60)
        if fresh_basis:
            if abs(px - last) / last > _jump_limit(sym):
                self.rejected_ticks += 1
                log.warning("stale tick rejected: %s %.2f vs last %.2f",
                            sym, px, last)
                return None
        self._last_ltp[sym] = px
        gap = False
        if prev_ts is not None and ts - prev_ts > _GAP_MULT * self.timeframe:
            gap = True
        self._last_ts[sym] = ts
        bucket = int(ts // self.timeframe) * self.timeframe
        cur = self._open.get(sym)
        if cur is None or cur["bucket"] != bucket:
            closed = None
            if cur is not None:
                closed = {k: cur[k] for k in ("t", "o", "h", "l", "c", "v")}
                self._closed.setdefault(sym, []).append(closed)
                del self._closed[sym][:-self.max_bars]
                self._save()
            self._open[sym] = {"bucket": bucket, "t": bucket, "o": px,
                               "h": px, "l": px, "c": px, "v": 1,
                               "gap": gap}
            return closed
        cur["h"] = max(cur["h"], px)
        cur["l"] = min(cur["l"], px)
        cur["c"] = px
        cur["v"] += 1
        return None

    def _save(self) -> None:
        """Atomic JSON dump of recent closed bars. Never raises."""
        if not self._persist_path:
            return
        try:
            data = {s: bars[-_KEEP_BARS:]
                    for s, bars in self._closed.items() if bars}
            tmp = self._persist_path + ".tmp"
            parent = os.path.dirname(self._persist_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._persist_path)
        except Exception:
            log.exception("candle persist save failed")

    def _load(self) -> None:
        """Reload same-session bars. Stale (>today IST) bars dropped. Never raises."""
        try:
            with open(self._persist_path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            cutoff = _today_ist_midnight_epoch()
            restored = 0
            for sym, bars in data.items():
                if not isinstance(bars, list):
                    continue
                clean = []
                for b in bars:
                    if not isinstance(b, dict):
                        continue
                    try:
                        t = float(b.get("t", 0))
                        row = {"t": t, "o": float(b["o"]), "h": float(b["h"]),
                               "l": float(b["l"]), "c": float(b["c"]),
                               "v": int(b.get("v", 0))}
                    except (KeyError, TypeError, ValueError):
                        continue
                    if t >= cutoff:
                        clean.append(row)
                if clean:
                    self._closed[str(sym).upper()] = clean[-self.max_bars:]
                    restored += len(clean)
            if restored:
                log.info("candle persist: restored %d same-session bars", restored)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("candle persist load failed")

    def get_candles(self, symbol: str, n: int = 60) -> List[dict]:
        """Last n bars, oldest first, including the forming bar."""
        sym = (symbol or "").upper().strip()
        bars = list(self._closed.get(sym, []))
        cur = self._open.get(sym)
        if cur is not None:
            bars.append({k: cur[k] for k in ("t", "o", "h", "l", "c", "v")})
        return bars[-n:]

    def bar_count(self, symbol: str) -> int:
        sym = (symbol or "").upper().strip()
        return len(self._closed.get(sym, [])) + (1 if sym in self._open else 0)

    def symbols(self) -> List[str]:
        return sorted(set(self._closed) | set(self._open))
