"""
Candle builder — accumulates live Kotak LTP ticks into OHLC bars.

Ticks carry LTP but no reliable per-tick volume, so `v` counts ticks in the
bar (activity proxy). The engine's volume-spike check therefore means an
activity spike — documented, not silent.
"""
import time
from typing import Dict, List, Optional


class CandleBuilder:
    """Per-symbol timeframe bars. All state in memory (VM restart reseeds)."""

    def __init__(self, timeframe_secs: int = 60, max_bars: int = 500):
        if timeframe_secs <= 0:
            raise ValueError("timeframe_secs must be positive")
        self.timeframe = timeframe_secs
        self.max_bars = max_bars
        self._closed: dict[str, list[dict]] = {}
        self._open: dict[str, dict] = {}

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
            self._open[sym] = {"bucket": bucket, "t": bucket, "o": ltp,
                               "h": ltp, "l": ltp, "c": ltp, "v": 1}
            return closed
        cur["h"] = max(cur["h"], ltp)
        cur["l"] = min(cur["l"], ltp)
        cur["c"] = ltp
        cur["v"] += 1
        return None

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
