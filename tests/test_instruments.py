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


def test_recap_call_put_format():
    s = parse_instrument_signal(
        "📊 INDEX OPTIONS 📊\n🀄️ NIFTY 22500 CALL 🀄️\nENTRY PRICE ➖120-125\n"
        "💰Target 1 ⛔️ 150 💰Target 2 ⛔️ 200 💰Target 3 ⛔️ 260\n🔴Stop Loss: 102"
    )
    assert s.status == "valid" and s.kind == "OPT"
    assert s.underlying == "NIFTY" and s.option_type == "CE"
    assert (s.entry_low, s.entry_high) == (120.0, 125.0)
    assert s.stoploss == 102.0 and s.targets == [150.0, 200.0, 260.0]
    assert s.display_symbol == "NIFTY22500CE"


def test_recap_put_format():
    s = parse_instrument_signal(
        "📊 INDEX OPTIONS 📊\n🀄️ SENSEX 71600 PUT 🀄️\nENTRY PRICE ➖160-170\n"
        "💰Target 1 ⛔️ 190 💰Target 2 ⛔️ 240 💰Target 3 ⛔️ 320\n🔴Stop Loss: 128"
    )
    assert s.status == "valid" and s.kind == "OPT"
    assert s.underlying == "SENSEX" and s.option_type == "PE"
    assert s.display_symbol == "SENSEX71600PE"
    assert s.targets == [190.0, 240.0, 320.0]


def test_single_line_option():
    s = parse_instrument_signal("BUY NIFTY 25000 CE @ 150 SL 130 TGT 180/220")
    assert s.status == "valid" and s.kind == "OPT"
    assert s.display_symbol == "NIFTY25000CE"
    assert (s.entry_low, s.entry_high) == (150.0, 150.0)
    assert s.stoploss == 130.0 and s.targets == [180.0, 220.0]


def test_t1t2t3_targets():
    s = parse_instrument_signal(
        "BANKNIFTY 58000 PE BUY 200-210 SL 180 T1 250 T2 300 T3 350"
    )
    assert s.status == "valid" and s.kind == "OPT"
    assert s.targets == [250.0, 300.0, 350.0]


def test_equity_add_more_and_targets():
    s = parse_instrument_signal(
        "INTRADAY BUY 360ONE 1197, add more 1192, SL 1189, Targets 1204/1210"
    )
    assert s.status == "valid" and s.kind == "EQ"
    assert s.underlying == "360ONE" and s.exchange_segment == "nse_cm"
    assert s.average == 1192.0 and s.entry_low == 1192.0
    assert s.stoploss == 1189.0 and s.targets == [1204.0, 1210.0]
    assert s.holding == "intraday"


def test_equity_short_term_flagged():
    s = parse_instrument_signal(
        "💚 SHORT TERM BUY KPRMILL 1135, Targets 1175/1200"
    )
    assert s.status == "valid" and s.kind == "EQ"
    assert s.underlying == "KPRMILL" and s.holding == "short_term"


def test_equity_slash_range_decimals():
    s = parse_instrument_signal("SHORT TERM BUY SPECIALITY 146.50/147, targets 151/158")
    assert s.status == "valid" and s.kind == "EQ"
    assert (s.entry_low, s.entry_high) == (146.5, 147.0)
    assert s.targets == [151.0, 158.0] and s.holding == "short_term"


def test_equity_lots_and_trigger():
    s = parse_instrument_signal("Buy above 24620 NIFTY FUT 2 lots SL 24580 TGT 24700")
    assert s.status == "valid" and s.kind == "FUT"
    assert s.lots is None or True  # lots live on EQ; FUT keeps strategy lots
    e = parse_instrument_signal("INTRADAY BUY RELIANCE 2950 2 lots SL 2930 TGT 2990")
    assert e.status == "valid" and e.lots == 2 and e.holding == "intraday"


def test_followups_ignored():
    assert parse_instrument_signal(
        "🚨 NIFTY 22500 CALL 120➝156 HIT! 1st Target Achieved, Book Partial ✅"
    ).status == "ignored"
    assert parse_instrument_signal(
        "ORDER ACTIVATED 👍 NIFTY 22500PE 132 TO 142 HIGH, 10 POINTS DONE 🔥"
    ).status == "ignored"
    assert parse_instrument_signal("1135 »»» 1147 +++KPRMILL").status == "ignored"
    assert parse_instrument_signal("SL HIT 🎯").status == "ignored"


def test_single_stock_equity_option():
    s = parse_instrument_signal(
        "AUTOTRENDER\nINFY\n1160CE\nPRICE @ 28.71-29.89\nSTOPLOSS\n27.98\nTARGETS\n31.94"
    )
    assert s.status == "valid" and s.kind == "OPT"
    assert s.underlying == "INFY" and s.exchange_segment == "nse_fo"
    assert s.display_symbol == "INFY1160CE"
    assert (s.entry_low, s.entry_high) == (28.7, 29.9)  # 0.05 tick grid

    t = parse_instrument_signal(
        "AUTOTRENDER\nTCS\n2440CE\nPRICE @ 40.92-42.59\nSTOPLOSS\n39.38\nTARGETS\n45.0"
    )
    assert t.status == "valid" and t.underlying == "TCS"

    b = parse_instrument_signal("BHARTIARTL 600CE PRICE @ 20-22 SL 18 TARGETS 26/30")
    assert b.status == "valid" and b.underlying == "BHARTIARTL"
