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
        self._persist_path = persist_path
        if persist_path:
            self._load()

    def update(self, symbol: str, ltp: float, ts: Optional[float] = None) -> Optional[dict]:
        """Fold one tick into its bar. Returns the closed bar when one rolls over."""
        sym = (symbol or "").upper().strip()
        if not sym or ltp is None or ltp <= 0:
            return None
        ts = ts if ts is not None else time.time()
        bucket = int(ts // self.timeframe) * self.timeframe
        cur = self._open.get(sym)
        if cur is None or cur["bucket"] != bucket:
            closed = None
            if cur is not None:
                closed = {k: cur[k] for k in ("t", "o", "h", "l", "c", "v")}
                self._closed.setdefault(sym, []).append(closed)
                del self._closed[sym][:-self.max_bars]
                self._save()
            self._open[sym] = {"bucket": bucket, "t": bucket, "o": ltp,
                               "h": ltp, "l": ltp, "c": ltp, "v": 1}
            return closed
        cur["h"] = max(cur["h"], ltp)
        cur["l"] = min(cur["l"], ltp)
        cur["c"] = ltp
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
