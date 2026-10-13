"""Unit tests for backend/candle_builder.py (no I/O, no market feed)."""
from backend.candle_builder import CandleBuilder


def test_single_bar_ohlc():
    b = CandleBuilder(timeframe_secs=60)
    assert b.update("NIFTY", 100, ts=970.0) is None
    assert b.update("NIFTY", 105, ts=980.0) is None
    assert b.update("NIFTY", 98, ts=990.0) is None
    bars = b.get_candles("NIFTY")
    assert len(bars) == 1
    assert bars[0] == {"t": 960, "o": 100, "h": 105, "l": 98, "c": 98, "v": 3}


def test_rollover_closes_bar():
    b = CandleBuilder(timeframe_secs=60)
    b.update("NIFTY", 100, ts=970.0)
    closed = b.update("NIFTY", 110, ts=1100.0)
    assert closed == {"t": 960, "o": 100, "h": 100, "l": 100, "c": 100, "v": 1}
    bars = b.get_candles("NIFTY")
    assert len(bars) == 2
    assert bars[-1]["o"] == 110


def test_guards_and_symbols():
    b = CandleBuilder(timeframe_secs=60)
    assert b.update("", 100, ts=1000.0) is None
    assert b.update("NIFTY", 0, ts=1000.0) is None
    assert b.update("NIFTY", -5, ts=1000.0) is None
    assert b.bar_count("NIFTY") == 0
    b.update("nifty", 100, ts=970.0)  # case-insensitive
    assert b.symbols() == ["NIFTY"]
    assert b.get_candles("NIFTY", n=5)[0]["c"] == 100
