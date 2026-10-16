"""Tick-grid enforcement: every trade price must be a multiple of 0.05."""
import pytest

from backend.instruments import parse_instrument_signal, round_tick


def _on_tick(v):
    return abs(v / 0.05 - round(v / 0.05)) < 1e-9


def test_round_tick_basics():
    assert round_tick(28.71) == 28.7
    assert round_tick(29.89) == 29.9
    assert round_tick(27.98) == 28.0
    assert round_tick(31.94) == 31.95
    assert round_tick(100) == 100.0
    assert round_tick(0.03) == 0.05


def test_parsed_opt_sits_on_grid():
    s = parse_instrument_signal(
        "AUTOTRENDER\nINFY\n1160CE\nPRICE @ 28.71-29.89\n"
        "STOPLOSS\n27.98\nTARGETS\n31.94"
    )
    assert s.status == "valid"
    assert (s.entry_low, s.entry_high) == (28.7, 29.9)
    assert s.stoploss == 28.0
    assert s.targets == [31.95]
    for v in [s.entry_low, s.entry_high, s.stoploss, *s.targets]:
        assert _on_tick(v), v


def test_parsed_eq_and_fut_sit_on_grid():
    e = parse_instrument_signal(
        "RELIANCE\nBUY 2950.03-2960.07\nSL 2930.02\nTARGETS 2990.06"
    )
    assert e.status == "valid" and e.kind == "EQ"
    for v in [e.entry_low, e.entry_high, e.stoploss, *e.targets]:
        assert _on_tick(v), v
    f = parse_instrument_signal(
        "NIFTY FUT\nPRICE @ 25600.03-25650.07\nSL 25550.02\nTARGETS 25750.06"
    )
    assert f.status == "valid" and f.kind == "FUT"
    for v in [f.entry_low, f.entry_high, f.stoploss, *f.targets]:
        assert _on_tick(v), v


@pytest.mark.asyncio
async def test_paper_place_order_rounds_to_tick(monkeypatch):
    from backend import paper_trader as pt_mod
    from backend.paper_trader import PaperTrader

    async def fake_save_trade(signal_id, order):
        return 1

    async def fake_save_pending(signal_id, trade_id, order):
        return 1

    monkeypatch.setattr(pt_mod.db, "save_trade", fake_save_trade)
    monkeypatch.setattr(pt_mod.db, "save_pending_order", fake_save_pending)

    tr = PaperTrader()
    sig = {"underlying": "INFY", "strike": "1160", "option_type": "CE",
           "entry_low": 28.71, "entry_high": 29.89, "stoploss": 27.98,
           "trading_symbol": "INFY1160CE", "display_symbol": "INFY1160CE",
           "exchange_segment": "nse_fo"}
    res = await tr.place_order(sig, signal_id=1, lot_size=1, strategy={})
    o = res["order"]
    assert (o["entry_low"], o["entry_high"]) == (28.7, 29.9)
    assert o["price"] == 29.9
    assert o["signal_stoploss"] == 28.0
    for v in [o["price"], o["entry_low"], o["entry_high"], o["signal_stoploss"]]:
        assert _on_tick(v), v
