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
