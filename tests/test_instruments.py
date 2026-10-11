"""Tests for Phase-1 multi-instrument parser (backend/instruments.py)."""
from backend.instruments import parse_instrument_signal


def test_sensex_option_unchanged():
    s = parse_instrument_signal(
        "RISKY JACKPOT\nSENSEX\n📈📉 78500PE\n📊 PRICE @ 270-280\n"
        "STOPLOSS\n260\n🎯 TARGETS\n300/500/650\n23rd APRIL EXPIRY"
    )
    assert s.status == "valid" and s.kind == "OPT"
    assert s.underlying == "SENSEX" and s.exchange_segment == "bse_fo"
    assert s.strike == "78500" and s.option_type == "PE"
    assert (s.entry_low, s.entry_high) == (270.0, 280.0)
    assert s.stoploss == 260.0 and s.targets == [300.0, 500.0, 650.0]
    assert s.display_symbol == "SENSEX78500PE"


def test_nifty_option():
    s = parse_instrument_signal(
        "NIFTY\n25600CE\nPRICE @ 120-128\nSL 115\nTARGETS 150/200/260"
    )
    assert s.status == "valid" and s.kind == "OPT"
    assert s.underlying == "NIFTY" and s.exchange_segment == "nse_fo"
    assert s.display_symbol == "NIFTY25600CE"
    assert s.stoploss == 115.0 and s.targets == [150.0, 200.0, 260.0]


def test_banknifty_option_wide_range_allowed():
    s = parse_instrument_signal(
        "BANKNIFTY\n58000PE\nPRICE @ 300-380\nSTOPLOSS 290\nTARGETS 450/600"
    )
    assert s.status == "valid" and s.kind == "OPT"
    assert s.underlying == "BANKNIFTY" and s.exchange_segment == "nse_fo"


def test_nifty_future():
    s = parse_instrument_signal("NIFTY FUT\nPRICE @ 25600-25650\nSL 25550\nTARGETS 25750/25850")
    assert s.status == "valid" and s.kind == "FUT"
    assert s.underlying == "NIFTY" and s.exchange_segment == "nse_fo"
    assert s.strike is None and s.display_symbol == "NIFTYFUT"


def test_equity_intraday():
    s = parse_instrument_signal("RELIANCE\nBUY 2950-2960\nSL 2930\nTARGETS 2990/3020")
    assert s.status == "valid" and s.kind == "EQ"
    assert s.underlying == "RELIANCE" and s.exchange_segment == "nse_cm"
    assert s.stoploss == 2930.0 and s.display_symbol == "RELIANCE"


def test_unknown_equity_ignored():
    s = parse_instrument_signal("SOMECOIN\nBUY 100-110\nSL 95\nTARGETS 120")
    assert s.status == "ignored"


def test_option_without_underlying_ignored():
    s = parse_instrument_signal("78500PE\nPRICE @ 270-280\nSL 260\nTARGETS 300")
    assert s.status == "ignored"


def test_chatter_ignored():
    assert parse_instrument_signal("GOOD MORNING TRADERS").status == "ignored"
    assert parse_instrument_signal("").status == "empty"


def test_sl_above_entry_dropped():
    s = parse_instrument_signal("NIFTY\n25600CE\nPRICE @ 120-128\nSL 500\nTARGETS 150/200")
    assert s.status == "valid" and s.stoploss is None
    assert s.targets == [150.0, 200.0]
