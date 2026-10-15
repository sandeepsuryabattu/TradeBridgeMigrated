"""Parity tests: backend/autotrender_engine.py must match the JS originals.

Reference values generated from autotrender-personal
(src/services/patterns.js + marketData.js) — see /tmp/js_reference.txt.
"""
import math

import pytest

from backend import autotrender_engine as eng

CLOSES = [100, 101, 102, 101, 103, 105, 104, 106, 108, 107, 109, 110, 112,
          111, 113, 115, 114, 116, 118, 117, 119, 120, 119, 121, 122, 121,
          123, 124, 125, 124]


def _candles():
    cs = [{"o": c - 0.5, "h": c + 0.6, "l": c - 0.7, "c": c, "v": 1000 + i * 10}
          for i, c in enumerate(CLOSES)]
    cs[29]["h"] = 130
    cs[29]["c"] = 129
    return cs


def test_ema_parity():
    assert eng.ema(CLOSES, 5)[-1] == pytest.approx(123.41599179081791)
    assert eng.ema(CLOSES, 20)[-1] == pytest.approx(117.29050282391871)
    # Warm-up bars are None; seeding bar holds the SMA.
    assert eng.ema(CLOSES, 5)[:4] == [None, None, None, None]
    assert eng.ema([], 5) == []


def test_rsi_parity():
    assert eng.rsi(CLOSES, 14) == pytest.approx(76.81586103772642)
    assert eng.rsi([100, 101], 14) == 50.0  # too short


def test_detect_parity():
    d = eng.detect(_candles())
    assert d["breakout"] == "HIGH"
    assert d["vol_spike"] is False
    assert d["bias"] == "BULLISH"
    assert d["rsi"] == pytest.approx(84.36293429548849)
    assert d["ema20"][-1] == pytest.approx(117.76669330010918)
    with pytest.raises(ValueError):
        eng.detect(_candles()[:10])


def test_atr_parity():
    assert eng.atr(_candles(), 14) == pytest.approx(2.3625154165654965)


def test_entry_exit_parity():
    assert eng.entry_exit({"c": 105}, 2, "BULLISH") == {
        "entry": 105, "sl": 102, "tgt": 111, "rr": 2}
    assert eng.entry_exit({"c": 105}, 2, "BEARISH", rr=3) == {
        "entry": 105, "sl": 108, "tgt": 96, "rr": 3}
    assert eng.entry_exit({"c": 105}, 0, "BULLISH")["entry"] is None
    assert eng.entry_exit({"c": 105}, 2, "NEUTRAL")["entry"] is None


def test_derivatives_parity():
    rows = [
        {"strike": 100, "callOI": 10, "putOI": 5, "callChgOI": 4,
         "putChgOI": 6, "callLTP": 5, "putLTP": 5},
        {"strike": 110, "callOI": 5, "putOI": 10, "callChgOI": 2,
         "putChgOI": 8, "callLTP": 2, "putLTP": 9, "atm": True},
    ]
    assert eng.calc_pcr(rows) == 1.0
    assert eng.calc_pcr_chg(rows) == 2.33
    assert eng.max_pain(rows) == 100
    assert eng.straddle(rows) == 11
    assert eng.get_support_resistance(rows) == {
        "support": 110, "supportOI": 10, "resistance": 100, "resistanceOI": 10}
    assert eng.calc_pcr([]) == 0.0


def test_option_call_parity():
    rows = [
        {"strike": 100, "callOI": 10, "putOI": 5, "callLTP": 5, "putLTP": 5},
        {"strike": 110, "callOI": 5, "putOI": 10, "callLTP": 2, "putLTP": 9, "atm": True},
    ]
    assert eng.option_call_details(rows, 105, "BULLISH", 105, 102, 111) == {
        "side": "BUY CE", "strike": 100, "premium": 5, "entryLow": 4.9,
        "entryHigh": 5.1, "sl": 3.5, "tgt": 8, "lot": 75}
    assert eng.option_call_details(rows, 105, "NEUTRAL", 105, 102, 111)["side"] == "NO TRADE"
