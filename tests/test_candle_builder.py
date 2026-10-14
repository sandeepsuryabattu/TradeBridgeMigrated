"""Unit tests for backend/candle_builder.py (no I/O, no market feed)."""
import json
import time

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


def test_persist_reloads_same_session(tmp_path):
    path = str(tmp_path / "candles.json")
    b = CandleBuilder(timeframe_secs=60, persist_path=path)
    now = time.time()
    base = int(now // 60) * 60
    b.update("NIFTY", 100, ts=base - 120)
    b.update("NIFTY", 105, ts=base - 60)   # closes first bar → saves
    b.update("NIFTY", 107, ts=base - 30)
    b2 = CandleBuilder(timeframe_secs=60, persist_path=path)
    bars = b2.get_candles("NIFTY")
    assert len(bars) == 1 and bars[0]["c"] == 100
    # File holds plain JSON rows.
    saved = json.loads(open(path).read())
    assert saved["NIFTY"][0]["o"] == 100


def test_persist_drops_stale_sessions(tmp_path):
    path = str(tmp_path / "candles.json")
    stale = time.time() - 3 * 24 * 3600
    with open(path, "w") as f:
        json.dump({"NIFTY": [{"t": stale, "o": 1, "h": 2, "l": 1,
                              "c": 2, "v": 9}]}, f)
    b = CandleBuilder(timeframe_secs=60, persist_path=path)
    assert b.bar_count("NIFTY") == 0  # yesterday's tape never loads


def test_no_path_means_memory_only():
    b = CandleBuilder(timeframe_secs=60)
    b.update("NIFTY", 100, ts=970.0)
    b.update("NIFTY", 110, ts=1100.0)
    assert b.bar_count("NIFTY") == 2
