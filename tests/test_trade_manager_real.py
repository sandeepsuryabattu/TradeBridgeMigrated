"""
Tests for TradeManager real-mode integration with RealTrader.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock


@pytest.fixture
def trade_manager():
    """Create a TradeManager with mocked deps."""
    with patch("backend.trade_manager.KotakTrader") as MockKotak, \
         patch("backend.trade_manager.MarketFeed") as MockMF, \
         patch("backend.trade_manager.PaperTrader") as MockPT, \
         patch("backend.trade_manager.RealTrader") as MockRT, \
         patch("backend.trade_manager.ContractMaster") as MockCM:

        mock_kotak = MockKotak.return_value
        mock_kotak.is_authenticated = False
        mock_kotak.get_status.return_value = {"authenticated": False}

        mock_mf = MockMF.return_value
        mock_mf.is_running = False
        mock_mf._subscriptions = {}
        mock_mf.add_tick_callback = MagicMock()
        mock_mf.remove_tick_callback = MagicMock()

        mock_pt = MockPT.return_value
        mock_pt.get_pnl_summary.return_value = {"open_positions": 0}
        mock_pt.on_tick = AsyncMock()

        mock_rt = MockRT.return_value
        mock_rt.get_pnl_summary.return_value = {"open_positions": 0}
        mock_rt.place_order = AsyncMock(return_value={"status": "pending", "trade_id": 1})
        mock_rt.on_tick = AsyncMock()
        mock_rt.set_ws_broadcast = MagicMock()

        from backend.trade_manager import TradeManager
        tm = TradeManager()
        # Re-assign mocks (constructor creates new instances)
        tm.kotak = mock_kotak
        tm.market_feed = mock_mf
        tm.paper_trader = mock_pt
        tm.real_trader = mock_rt

        yield tm


class TestModeSwitch:
    def test_mode_switch_to_real(self, trade_manager):
        trade_manager.mode = "paper"
        result = trade_manager.set_mode("real")

        assert result["new_mode"] == "real"
        assert trade_manager.mode == "real"
        # Should swap tick callbacks
        trade_manager.market_feed.remove_tick_callback.assert_called_with(
            trade_manager.paper_trader.on_tick
        )
        trade_manager.market_feed.add_tick_callback.assert_called_with(
            trade_manager.real_trader.on_tick
        )

    def test_mode_switch_back_to_paper(self, trade_manager):
        trade_manager.mode = "real"
        result = trade_manager.set_mode("paper")

        assert result["new_mode"] == "paper"
        trade_manager.market_feed.remove_tick_callback.assert_called_with(
            trade_manager.real_trader.on_tick
        )

    def test_invalid_mode_rejected(self, trade_manager):
        result = trade_manager.set_mode("invalid")
        assert result["status"] == "error"


class TestExecuteReal:
    @pytest.mark.asyncio
    async def test_execute_real_delegates(self, trade_manager):
        trade_manager.mode = "real"
        trade_manager.kotak.is_authenticated = True

        signal = {"strike": "82000", "option_type": "CE", "entry_low": 145, "entry_high": 155}
        result = await trade_manager._execute_real(signal, signal_id=42)

        trade_manager.real_trader.place_order.assert_called_once_with(
            signal, 42,
            lot_size=trade_manager.lot_size,
            strategy=trade_manager.strategy,
        )

    @pytest.mark.asyncio
    async def test_real_mode_requires_auth(self, trade_manager):
        trade_manager.mode = "real"
        trade_manager.kotak.is_authenticated = False

        signal = {"strike": "82000", "option_type": "CE"}
        result = await trade_manager._execute_real(signal, signal_id=42)

        assert result["status"] == "error"
        assert "not authenticated" in result["message"].lower()
        trade_manager.real_trader.place_order.assert_not_called()


class TestGetStatus:
    def test_status_includes_real_trader(self, trade_manager):
        status = trade_manager.get_status()
        assert "real_trader" in status
        assert "paper_trader" in status
