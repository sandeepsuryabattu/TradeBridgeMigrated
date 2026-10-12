"""Phase 2/3 tests: multi-segment resolve + generic trade_manager pipeline."""
import pytest
from datetime import date, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from backend.contract_master import ContractMaster
from backend.instruments import parse_instrument_signal


def _expiry():
    return (date.today() + timedelta(days=2)).strftime("%d-%b-%Y")


def _opt_row(symbol, strike100, opt):
    return {
        "pTrdSymbol": symbol, "pSymbol": "TKN", "pStrikePrice": str(strike100),
        "pOptionType": opt, "pExpiryDate": _expiry(), "lLotSize": "65",
    }


@pytest.fixture
def master():
    c = ContractMaster()
    c._segments = {
        "nse_fo": [
            _opt_row("NIFTY13OCT2522500CE", 2250000, "CE"),
            _opt_row("NIFTY13OCT2522500PE", 2250000, "PE"),
            {"pTrdSymbol": "NIFTY13OCT25FUT", "pSymbol": "F1",
             "pStrikePrice": "0", "pOptionType": "", "pExpiryDate": _expiry()},
        ],
        "nse_cm": [
            {"pTrdSymbol": "RELIANCE", "pSymbol": "R1", "lLotSize": "1"},
        ],
        "bse_fo": [
            _opt_row("SENSEX13OCT2580000CE", 8000000, "CE"),
        ],
    }
    return c


def test_resolve_nifty_opt(master):
    r = master.resolve({"kind": "OPT", "underlying": "NIFTY", "strike": "22500",
                    "option_type": "CE", "exchange_segment": "nse_fo"})
    assert r and r["trading_symbol"] == "NIFTY13OCT2522500CE"
    assert r["exchange_segment"] == "nse_fo" and r["lot_size"] == "65"


def test_resolve_future(master):
    r = master.resolve({"kind": "FUT", "underlying": "NIFTY",
                    "exchange_segment": "nse_fo"})
    assert r and r["trading_symbol"] == "NIFTY13OCT25FUT"


def test_resolve_future_never_returns_option():
    c = ContractMaster()
    c._segments = {"nse_fo": [
        _opt_row("NIFTY13OCT2522500CE", 2250000, "CE"),
        _opt_row("NIFTY13OCT2522500PE", 2250000, "PE"),
    ]}
    assert c.resolve({"kind": "FUT", "underlying": "NIFTY",
                      "exchange_segment": "nse_fo"}) is None


def test_resolve_equity(master):
    r = master.resolve({"kind": "EQ", "underlying": "RELIANCE",
                    "exchange_segment": "nse_cm"})
    assert r and r["trading_symbol"] == "RELIANCE"


def test_resolve_unknown_returns_none(master):
    assert master.resolve({"kind": "EQ", "underlying": "NOPE",
                       "exchange_segment": "nse_cm"}) is None
    assert master.resolve({"kind": "OPT", "underlying": "NIFTY", "strike": "99999",
                       "option_type": "CE", "exchange_segment": "nse_fo"}) is None


def _manager(mock_resolve):
    with patch("backend.trade_manager.KotakTrader"), \
         patch("backend.trade_manager.MarketFeed") as MockMF, \
         patch("backend.trade_manager.PaperTrader") as MockPT, \
         patch("backend.trade_manager.RealTrader") as MockRT, \
         patch("backend.trade_manager.ContractMaster") as MockCM:
        from backend.trade_manager import TradeManager
        tm = TradeManager()
        tm.market_feed = MagicMock()
        tm.market_feed.is_running = True
        tm.market_feed._subscriptions = {}
        tm.market_feed.get_ltp.return_value = 0
        tm.contract_master = MagicMock()
        tm.contract_master.resolve.side_effect = mock_resolve
        tm.paper_trader = MagicMock()
        tm.paper_trader._open_positions = []
        tm.paper_trader._pending_orders = []
        tm.paper_trader.place_order = AsyncMock(
            return_value={"status": "pending", "trade_id": 7, "order": {}})
        tm.real_trader = MagicMock()
        tm.real_trader._open_positions = []
        tm.real_trader._pending_orders = []
        tm.mode = "paper"
        return tm


def _db():
    p = patch("backend.trade_manager.db")
    m = p.start()
    m.save_message = AsyncMock(return_value=1)
    m.save_signal = AsyncMock(return_value=2)
    m.save_trade = AsyncMock(return_value=3)
    m.update_trade = AsyncMock()
    m.get_signals = AsyncMock(return_value=[])
    return p


@pytest.mark.asyncio
async def test_nifty_flows_to_paper_with_segment():
    tm = _manager(lambda sig: {"instrument_token": "T1", "trading_symbol": "NIFTY13OCT2522500CE",
                               "exchange_segment": "nse_fo", "lot_size": "65"})
    dbp = _db()
    try:
        out = await tm.process_message("NIFTY\n25600CE\nPRICE @ 120-128\nSL 115\nTARGETS 150/200", "T")
    finally:
        dbp.stop()
    assert out["trade"]["status"] == "pending"
    tm.market_feed.subscribe_instrument.assert_called_with("T1", "NIFTY13OCT2522500CE", "nse_fo")
    sig, _ = tm.paper_trader.place_order.call_args[0]
    assert sig["trading_symbol"] == "NIFTY13OCT2522500CE"
    assert sig["exchange_segment"] == "nse_fo"


@pytest.mark.asyncio
async def test_short_term_holding_skipped():
    tm = _manager(lambda sig: None)
    dbp = _db()
    try:
        out = await tm.process_message("SHORT TERM BUY KPRMILL 1135, Targets 1175/1200", "T")
    finally:
        dbp.stop()
    assert out["skipped"] == "non_intraday"
    tm.paper_trader.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_equity_by_symbol():
    tm = _manager(lambda sig: {"instrument_token": "R1", "trading_symbol": "RELIANCE",
                               "exchange_segment": "nse_cm", "lot_size": "1"})
    tm.paper_trader._open_positions = [{"trading_symbol": "RELIANCE", "strike": "",
                                        "option_type": ""}]
    dbp = _db()
    try:
        out = await tm.process_message("INTRADAY BUY RELIANCE 2950 SL 2930 TGT 2990", "T")
    finally:
        dbp.stop()
    assert out["skipped"] == "position_open"
    tm.paper_trader.place_order.assert_not_called()


def test_update_signal_ltp_matches_broker_symbols():
    """LIKE prefix+suffix matcher hits expiry-affixed symbols, exact never did."""
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, idx TEXT, strike TEXT, option_type TEXT, status TEXT, last_ltp REAL)")
    c.executemany("INSERT INTO signals (idx, strike, option_type, status) VALUES (?,?,?,?)", [
        ("SENSEX", "71500", "CE", "valid"),
        ("NIFTY", "22500", "PE", "valid"),
        ("RELIANCE", "", "", "valid"),
        ("SENSEX", "71500", "CE", "ignored"),
    ])
    def upd(sym, ltp):
        c.execute("""UPDATE signals SET last_ltp = ?
           WHERE status = 'valid'
           AND ? LIKE (idx || '%' || strike || option_type)""", (ltp, sym))
    upd("SENSEX26O1571500CE", 100.0)
    upd("NIFTY26O1322500PE", 150.0)
    upd("RELIANCE-EQ", 2955.0)
    rows = dict((r[0], r[1]) for r in c.execute("SELECT id, last_ltp FROM signals"))
    assert rows == {1: 100.0, 2: 150.0, 3: 2955.0, 4: None}


def test_resolve_never_confuses_nifty_with_finnifty():
    """Live bug 2026-10-12: NIFTY 22600PE resolved to FINNIFTY26NOV22600PE."""
    from backend.contract_master import ContractMaster
    c = ContractMaster()
    c._segments = {"nse_fo": [
        {"pTrdSymbol": "FINNIFTY26NOV22600PE", "pSymbol": "F1",
         "pStrikePrice": "2260000", "pOptionType": "PE", "pExpiryDate": "13-Oct-2026"},
        {"pTrdSymbol": "NIFTY26O1322600PE", "pSymbol": "N1",
         "pStrikePrice": "2260000", "pOptionType": "PE", "pExpiryDate": "13-Oct-2026"},
        {"pTrdSymbol": "BANKNIFTY26OCT22600PE", "pSymbol": "B1",
         "pStrikePrice": "2260000", "pOptionType": "PE", "pExpiryDate": "13-Oct-2026"},
    ]}
    r = c.resolve({"kind": "OPT", "underlying": "NIFTY", "strike": "22600",
                   "option_type": "PE", "exchange_segment": "nse_fo"})
    assert r and r["trading_symbol"] == "NIFTY26O1322600PE"
    r = c.resolve({"kind": "OPT", "underlying": "BANKNIFTY", "strike": "22600",
                   "option_type": "PE", "exchange_segment": "nse_fo"})
    assert r and r["trading_symbol"] == "BANKNIFTY26OCT22600PE"


def _dated_row(symbol, strike100, opt, exp):
    return {"pTrdSymbol": symbol, "pSymbol": "T", "pStrikePrice": str(strike100),
            "pOptionType": opt, "pExpiryDate": exp}

def test_resolve_honours_signal_expiry():
    """Live case 2026-10-12: 13-Oct NIFTY signal must not take the Oct-monthly row."""
    from backend.contract_master import ContractMaster
    c = ContractMaster()
    c._segments = {"nse_fo": [
        _dated_row("NIFTY26OCT22450CE", 2245000, "CE", "30-Oct-2026"),
        _dated_row("NIFTY26O1322450CE", 2245000, "CE", "13-Oct-2026"),
    ]}
    r = c.resolve({"kind": "OPT", "underlying": "NIFTY", "strike": "22450",
                   "option_type": "CE", "exchange_segment": "nse_fo",
                   "expiry": "13 october expiry"})
    assert r and r["trading_symbol"] == "NIFTY26O1322450CE"

def test_resolve_refuses_unmatched_expiry():
    from backend.contract_master import ContractMaster
    c = ContractMaster()
    c._segments = {"nse_fo": [_dated_row("NIFTY26OCT22450CE", 2245000, "CE", "30-Oct-2026")]}
    assert c.resolve({"kind": "OPT", "underlying": "NIFTY", "strike": "22450",
                      "option_type": "CE", "exchange_segment": "nse_fo",
                      "expiry": "13 october expiry"}) is None

def test_resolve_no_hint_falls_back_to_nearest():
    from backend.contract_master import ContractMaster
    c = ContractMaster()
    c._segments = {"nse_fo": [
        _dated_row("NIFTY26OCT22450CE", 2245000, "CE", "30-Oct-2026"),
        _dated_row("NIFTY26O1322450CE", 2245000, "CE", "13-Oct-2026"),
    ]}
    r = c.resolve({"kind": "OPT", "underlying": "NIFTY", "strike": "22450",
                   "option_type": "CE", "exchange_segment": "nse_fo"})
    assert r and r["trading_symbol"] == "NIFTY26O1322450CE"


def test_symbol_embedded_expiry_beats_stale_row_dates():
    """Live case: rows carry 2016 timestamps; symbols carry the true date."""
    from backend.contract_master import ContractMaster
    c = ContractMaster()
    c._segments = {"nse_fo": [
        {"pTrdSymbol": "NIFTY26OCT22450CE", "pSymbol": "M",
         "pStrikePrice": "2245000", "pOptionType": "CE", "pExpiryDate": "1478701800"},
        {"pTrdSymbol": "NIFTY26O1322450CE", "pSymbol": "W",
         "pStrikePrice": "2245000", "pOptionType": "CE", "pExpiryDate": "1476369000"},
    ]}
    r = c.resolve({"kind": "OPT", "underlying": "NIFTY", "strike": "22450",
                   "option_type": "CE", "exchange_segment": "nse_fo",
                   "expiry": "13 october expiry"})
    assert r and r["trading_symbol"] == "NIFTY26O1322450CE"
    assert r["instrument_token"] == "W"

def test_symbol_expiry_parsing():
    from backend.contract_master import ContractMaster as CM
    assert CM._symbol_expiry("NIFTY26OCT22450CE", "NIFTY") == (2026, 10, None)
    assert CM._symbol_expiry("NIFTY26O1322450CE", "NIFTY") == (2026, 10, 13)
    assert CM._symbol_expiry("BANKNIFTY26OCT58000PE", "BANKNIFTY") == (2026, 10, None)
    assert CM._symbol_expiry("SENSEX26O1580000CE", "SENSEX") == (2026, 10, 15)


def test_update_signal_ltp_scoped_to_traded_contract():
    """Two expiries of one strike must not overwrite each other's cards."""
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, idx TEXT, strike TEXT, option_type TEXT, status TEXT, last_ltp REAL)")
    c.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, signal_id INTEGER, trading_symbol TEXT, status TEXT)")
    c.executemany("INSERT INTO signals (idx, strike, option_type, status) VALUES (?,?,?,?)", [
        ("NIFTY", "22450", "CE", "valid"),
        ("NIFTY", "22450", "CE", "valid"),
    ])
    c.executemany("INSERT INTO trades (signal_id, trading_symbol, status) VALUES (?,?,?)", [
        (1, "NIFTY26O1322450CE", "pending"),
        (2, "NIFTY26OCT22450CE", "pending"),
    ])
    def upd(sym, ltp):
        c.execute("""UPDATE signals SET last_ltp = ?
           WHERE status = 'valid' AND id IN (
               SELECT signal_id FROM trades WHERE trading_symbol = ?
           )""", (ltp, sym))
    upd("NIFTY26O1322450CE", 60.85)
    upd("NIFTY26OCT22450CE", 257.0)
    rows = dict((r[0], r[1]) for r in c.execute("SELECT id, last_ltp FROM signals"))
    assert rows == {1: 60.85, 2: 257.0}


def test_no_hint_prefers_symbol_nearest_expiry():
    """No-hint fallback reads dates from symbols, not stale row fields."""
    from backend.contract_master import ContractMaster
    c = ContractMaster()
    c._segments = {"nse_fo": [
        {"pTrdSymbol": "NIFTY26NOV22450CE", "pSymbol": "M",
         "pStrikePrice": "2245000", "pOptionType": "CE", "pExpiryDate": "1478701800"},
        {"pTrdSymbol": "NIFTY26O1322450CE", "pSymbol": "W",
         "pStrikePrice": "2245000", "pOptionType": "CE", "pExpiryDate": "1476369000"},
    ]}
    r = c.resolve({"kind": "OPT", "underlying": "NIFTY", "strike": "22450",
                   "option_type": "CE", "exchange_segment": "nse_fo"})
    assert r and r["trading_symbol"] == "NIFTY26O1322450CE"
