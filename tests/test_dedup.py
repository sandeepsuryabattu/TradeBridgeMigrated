"""Regression tests: dedup must match full contracts, never strike+type alone."""
from backend.trade_manager import same_contract


def test_exact_symbol_blocks():
    pos = {"trading_symbol": "SENSEX26O1571400PE", "strike": "71400", "option_type": "PE"}
    assert same_contract("SENSEX26O1571400PE", "OPT", "71400", "PE", "SENSEX", pos)


def test_cross_underlying_same_strike_does_not_block():
    # NIFTY 22500CE must not be blocked by an open SENSEX 22500CE position.
    pos = {"trading_symbol": "SENSEX26O1522500CE", "strike": "22500", "option_type": "CE"}
    assert not same_contract("NIFTY26O1322500CE", "OPT", "22500", "CE", "NIFTY", pos)


def test_different_expiry_same_strike_does_not_block():
    pos = {"trading_symbol": "NIFTY26O1322450PE", "strike": "22450", "option_type": "PE"}
    assert not same_contract("NIFTY26O2022450PE", "OPT", "22450", "PE", "NIFTY", pos)


def test_unresolved_signal_same_underlying_blocks():
    # Display-symbol fallback still blocks the same underlying/strike/type.
    pos = {"trading_symbol": "SENSEX26O1571400PE", "strike": "71400", "option_type": "PE"}
    assert same_contract("SENSEX 71400 PE", "OPT", "71400", "PE", "SENSEX", pos)


def test_eq_matches_full_symbol_only():
    pos = {"trading_symbol": "RELIANCE-EQ", "strike": "", "option_type": ""}
    assert same_contract("RELIANCE-EQ", "EQ", "", "", "RELIANCE", pos)
    assert not same_contract("TCS-EQ", "EQ", "", "", "TCS", pos)
