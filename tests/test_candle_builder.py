"""Unit tests for backend/candle_builder.py (no I/O, no market feed)."""
import json
import time

from backend.candle_builder import CandleBuilder


def test_single_bar_ohlc():
    b = CandleBuilder(timeframe_secs=60)
    assert b.update("NIFTY", 22600, ts=970.0) is None
    assert b.update("NIFTY", 22603, ts=980.0) is None
    assert b.update("NIFTY", 22599, ts=990.0) is None
    bars = b.get_candles("NIFTY")
    assert len(bars) == 1
    assert bars[0] == {"t": 960, "o": 22600, "h": 22603, "l": 22599,
                       "c": 22599, "v": 3}


def test_rollover_closes_bar():
    b = CandleBuilder(timeframe_secs=60)
    b.update("NIFTY", 22600, ts=970.0)
    closed = b.update("NIFTY", 22604, ts=1100.0)
    assert closed == {"t": 960, "o": 22600, "h": 22600, "l": 22600,
                      "c": 22600, "v": 1}
    bars = b.get_candles("NIFTY")
    assert len(bars) == 2
    assert bars[-1]["o"] == 22604


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
    b.update("NIFTY", 22600, ts=base - 120)
    b.update("NIFTY", 22602, ts=base - 60)   # closes first bar → saves
    b.update("NIFTY", 22603, ts=base - 30)
    b2 = CandleBuilder(timeframe_secs=60, persist_path=path)
    bars = b2.get_candles("NIFTY")
    assert len(bars) == 1 and bars[0]["c"] == 22600
    # File holds plain JSON rows.
    saved = json.loads(open(path).read())
    assert saved["NIFTY"][0]["o"] == 22600


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
    b.update("NIFTY", 22600, ts=970.0)
    b.update("NIFTY", 22604, ts=1100.0)
    assert b.bar_count("NIFTY") == 2


def test_stale_tick_rejected_live_case():
    # 2026-10-14 reconnect replay: 22491 print inside a 22650 market.
    b = CandleBuilder(timeframe_secs=60)
    b.update("NIFTY", 22625.0, ts=1000.0)
    assert b.update("NIFTY", 22491.55, ts=1005.0) is None  # dropped
    assert b.rejected_ticks == 1
    b.update("NIFTY", 22630.0, ts=1010.0)
    bars = b.get_candles("NIFTY")
    assert bars[0]["l"] == 22625.0 and bars[0]["h"] == 22630.0


def test_derivative_legs_get_room_vix_gets_some():
    b = CandleBuilder(timeframe_secs=60)
    b.update("NIFTY26O1922650CE", 120.0, ts=1000.0)
    assert b.update("NIFTY26O1922650CE", 126.0, ts=1010.0) is None  # +5% ok
    assert b.update("NIFTY26O1922650CE", 140.0, ts=1020.0) is None  # +11% no
    assert b.rejected_ticks == 1
    b.update("INDIAVIX", 14.0, ts=1000.0)
    assert b.update("INDIAVIX", 14.5, ts=1010.0) is None  # +3.6% ok


def test_gap_flag_after_idle_and_overnight_bypass():
    b = CandleBuilder(timeframe_secs=60)
    b.update("NIFTY", 22600, ts=1000.0)
    b.update("NIFTY", 22602, ts=1000.0 + 180)  # 3-min idle → gapped bar
    assert b._open["NIFTY"].get("gap") is True
    # Overnight-size idle with a real gap move: accepted, not rejected.
    b2 = CandleBuilder(timeframe_secs=60)
    b2.update("NIFTY", 22600, ts=1000.0)
    b2.update("NIFTY", 23000, ts=1000.0 + 20 * 3600)  # returns closed bar
    assert b2.rejected_ticks == 0
    assert b2._open["NIFTY"]["c"] == 23000
