"""
Tests for RealTrader — all Kotak API calls are mocked,
no real broker interaction.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_kotak():
    """Mock KotakTrader with standard successful responses."""
    k = MagicMock()
    k.is_authenticated = True

    # place_order returns a dict with nOrdNo
    k.place_order.return_value = {"status": "ok", "data": {"nOrdNo": "ORD001"}}
    k.modify_order.return_value = {"status": "ok", "data": {"nOrdNo": "ORD001"}}
    k.cancel_order.return_value = {"status": "ok", "data": {}}
    k.order_history.return_value = {"status": "ok", "data": {}}

    k.search_scrip.return_value = {
        "data": [{"pTrdSymbol": "SENSEX2503282000CE"}]
    }

    k.get_order_book.return_value = {
        "data": [
            {"nOrdNo": "SL001", "ordSt": "trigger pending"},
        ]
    }
    return k


@pytest.fixture
def mock_market_feed():
    mf = MagicMock()
    mf._subscriptions = {}
    return mf


@pytest.fixture
def sample_signal():
    return {
        "strike":        "82000",
        "option_type":   "CE",
        "entry_low":     145,
        "entry_high":    155,
        "stoploss":      140,
        "contract_lot_size": 20,
        "entry_label":   "Buy above 145-155",
    }


@pytest.fixture
def sample_strategy():
    return {
        "lots":             1,
        "activationPoints": 5.0,
        "trailGap":         2.0,
        "bouncePoints":     5,
        "bufferEnabled":    False,
        "bufferPoints":     2.0,
        "entrySlippage":    1.0,
        "exitSlippage":     1.0,
    }


def make_trader(mock_kotak, mock_market_feed):
    """Create a RealTrader instance with mocked deps and DB patches."""
    from backend.real_trader import RealTrader
    rt = RealTrader(kotak_trader=mock_kotak, market_feed=mock_market_feed)
    rt._ws_broadcast = AsyncMock()
    return rt


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPlaceOrder:
    """Tests for RealTrader.place_order() — pending order creation."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_place_order_saves_to_db(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)

        rt = make_trader(mock_kotak, mock_market_feed)
        result = await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        assert result["status"] == "pending"
        assert result["trade_id"] == 1
        mock_db.save_trade.assert_called_once()
        mock_db.save_pending_order.assert_called_once()
        assert len(rt._pending_orders) == 1

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_pending_order_has_correct_fields(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        order = rt._pending_orders[0]
        assert order["mode"] == "real"
        assert order["entry_low"] == 145
        assert order["entry_high"] == 155
        assert order["bounce_points"] == 5
        assert order["quantity"] == 20  # 1 lot * 20 multiplier
        assert order["sl_mode"] == "signal_trail"


class TestBounceBackEntry:
    """Tests for bounce-back logic in on_tick()."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_bounce_back_triggers_buy(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()
        mock_db.save_position = AsyncMock(return_value=100)
        mock_db.delete_pending_order = AsyncMock()
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        # Price enters range and sets min_ltp
        await rt.on_tick("12345", 150.0, tick)
        assert rt._pending_orders[0]["min_ltp"] == 150.0

        # Price drops further
        await rt.on_tick("12345", 148.0, tick)
        assert rt._pending_orders[0]["min_ltp"] == 148.0

        # Price bounces up by 5 pts (148 + 5 = 153) → fill
        await rt.on_tick("12345", 153.0, tick)

        # Kotak place_order should have been called (the BUY)
        assert mock_kotak.place_order.called
        call_kwargs = mock_kotak.place_order.call_args
        assert call_kwargs is not None

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_entry_slippage_applied(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()
        mock_db.save_position = AsyncMock(return_value=100)
        mock_db.delete_pending_order = AsyncMock()
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        # Enter range → bounce
        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)

        # Check that the BUY price includes slippage (+1)
        # First call is the BUY, second is the SL
        buy_call = mock_kotak.place_order.call_args_list[0]
        buy_price = buy_call.kwargs.get("price", 0)
        assert float(buy_price) == 154.0  # 153 + 1 slippage

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_no_fill_when_no_bounce(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        # Price enters range but doesn't bounce enough (only +3, need +5)
        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 151.0, tick)  # 148 + 3 = not enough

        assert len(rt._pending_orders) == 1  # still pending
        assert len(rt._open_positions) == 0
        assert not mock_kotak.place_order.called


class TestSLOrder:
    """Tests for exchange-level SL order placement and trailing."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_sl_order_placed_on_fill(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()
        mock_db.save_position = AsyncMock(return_value=100)
        mock_db.delete_pending_order = AsyncMock()
        mock_db.update_position = AsyncMock()

        # SL order returns different order ID
        sl_call_count = [0]
        def mock_place(*, exchange_segment, trading_symbol, transaction_type, order_type, quantity, price, **kwargs):
            sl_call_count[0] += 1
            if order_type == "SL":
                return {"status": "ok", "data": {"nOrdNo": "SL001"}}
            return {"status": "ok", "data": {"nOrdNo": "ORD001"}}

        mock_kotak.place_order.side_effect = mock_place

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)  # bounce → fill

        # Should have called place_order twice: once for BUY, once for SL
        assert mock_kotak.place_order.call_count == 2
        sl_call = mock_kotak.place_order.call_args_list[1]
        assert sl_call.kwargs["order_type"] == "SL"
        assert float(sl_call.kwargs["price"]) == 0.05
        assert float(sl_call.kwargs["trigger_price"]) == 140.0  # signal stoploss

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_trailing_sl_activation(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)

        # Manually add an open position
        pos = {
            "id": 100,
            "trade_id": 1,
            "trading_symbol": "SENSEX82000CE",
            "entry_price": 150.0,
            "current_price": 150.0,
            "quantity": 20,
            "pnl": 0,
            "max_ltp": 150.0,
            "trailing_sl": 140.0,
            "sl_activated": False,
            "activation_points": 5.0,
            "trail_gap": 2.0,
            "sl_order_id": "SL001",
            "exit_timer_mins": 10,
            "opened_at": datetime.now(timezone.utc),
            "exit_slippage": 1.0,
            "status": "open",
        }
        rt._open_positions.append(pos)

        tick = {"symbol": "SENSEX82000CE", "tk": "12345"}

        # LTP crosses activation (150 + 5 = 155)
        await rt.on_tick("12345", 155.0, tick)

        assert pos["sl_activated"] is True
        # modify_order should be called to update SL
        assert mock_kotak.modify_order.called

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_trailing_sl_trail_on_new_high(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)

        pos = {
            "id": 100,
            "trade_id": 1,
            "trading_symbol": "SENSEX82000CE",
            "entry_price": 150.0,
            "current_price": 155.0,
            "quantity": 20,
            "pnl": 100.0,
            "max_ltp": 155.0,
            "trailing_sl": 155.0,
            "sl_activated": True,
            "activation_points": 5.0,
            "trail_gap": 2.0,
            "sl_order_id": "SL001",
            "exit_timer_mins": 10,
            "opened_at": datetime.now(timezone.utc),
            "exit_slippage": 1.0,
            "status": "open",
        }
        rt._open_positions.append(pos)

        tick = {"symbol": "SENSEX82000CE", "tk": "12345"}

        # New high at 160 → SL trails to 160 - 2 = 158
        mock_kotak.modify_order.reset_mock()
        await rt.on_tick("12345", 160.0, tick)

        assert pos["trailing_sl"] == 158.0
        assert mock_kotak.modify_order.called
        mod_call = mock_kotak.modify_order.call_args
        assert float(mod_call.kwargs["trigger_price"]) == 158.0


class TestClosePosition:
    """Tests for position closing (timeout, kill, manual)."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_timeout_exit(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()
        mock_db.update_trade = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        pos = {
            "id": 100, "trade_id": 1,
            "trading_symbol": "SENSEX82000CE",
            "entry_price": 150.0, "current_price": 148.0,
            "quantity": 20, "pnl": -40.0,
            "max_ltp": 150.0, "trailing_sl": 140.0,
            "sl_activated": False, "activation_points": 5.0,
            "trail_gap": 2.0, "sl_order_id": "SL001",
            "exit_timer_mins": 10, "exit_slippage": 1.0,
            "opened_at": datetime.now(timezone.utc) - timedelta(minutes=15),
            "status": "open",
        }
        rt._open_positions.append(pos)

        result = await rt.close_position(100, exit_price=148.0, exit_reason="timer")

        assert result["status"] == "closed"
        assert result["exit_reason"] == "timer"
        # SL should be cancelled
        mock_kotak.cancel_order.assert_called_with(order_id="SL001")
        # Exit SELL should be placed
        sell_calls = [c for c in mock_kotak.place_order.call_args_list
                      if c.kwargs.get("transaction_type") == "S"]
        assert len(sell_calls) == 1
        assert sell_calls[0].kwargs["validity"] == "IOC"

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_kill_switch(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()
        mock_db.update_trade = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        for i in range(3):
            rt._open_positions.append({
                "id": 100 + i, "trade_id": i + 1,
                "trading_symbol": f"SENSEX8200{i}CE",
                "entry_price": 150.0, "current_price": 148.0,
                "quantity": 20, "pnl": -40.0,
                "max_ltp": 150.0, "trailing_sl": 140.0,
                "sl_activated": False, "activation_points": 5.0,
                "trail_gap": 2.0, "sl_order_id": f"SL00{i}",
                "exit_timer_mins": 10, "exit_slippage": 1.0,
                "opened_at": datetime.now(timezone.utc),
                "status": "open",
            })

        result = await rt.square_off_all()

        assert result["positions_closed"] == 3
        assert len(rt._open_positions) == 0


class TestOrderFeed:
    """Tests for order feed WebSocket event handling."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_sl_triggered_closes_position(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()
        mock_db.update_trade = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        pos = {
            "id": 100, "trade_id": 1,
            "trading_symbol": "SENSEX82000CE",
            "entry_price": 150.0, "current_price": 140.0,
            "quantity": 20, "pnl": -200.0,
            "max_ltp": 155.0, "trailing_sl": 140.0,
            "sl_activated": True, "activation_points": 5.0,
            "trail_gap": 2.0, "sl_order_id": "SL001",
            "exit_timer_mins": 10, "exit_slippage": 1.0,
            "opened_at": datetime.now(timezone.utc),
            "status": "open",
        }
        rt._open_positions.append(pos)

        # Simulate exchange SL triggered
        await rt.handle_order_feed({
            "data": {"nOrdNo": "SL001", "ordSt": "traded", "flPrc": "139.50"}
        })

        assert len(rt._open_positions) == 0
        mock_db.update_position.assert_called()

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_sl_rejected_replaces(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        pos = {
            "id": 100, "trade_id": 1,
            "trading_symbol": "SENSEX82000CE",
            "entry_price": 150.0, "current_price": 145.0,
            "quantity": 20, "pnl": -100.0,
            "max_ltp": 155.0, "trailing_sl": 140.0,
            "sl_activated": True, "activation_points": 5.0,
            "trail_gap": 2.0, "sl_order_id": "SL001",
            "exit_timer_mins": 10, "exit_slippage": 1.0,
            "opened_at": datetime.now(timezone.utc),
            "status": "open",
        }
        rt._open_positions.append(pos)

        # SL order rejected → should attempt re-place
        mock_kotak.place_order.return_value = {"status": "ok", "data": {"nOrdNo": "SL002"}}
        await rt.handle_order_feed({
            "data": {"nOrdNo": "SL001", "ordSt": "rejected", "rejRsn": "Insufficient margin"}
        })

        # Should have attempted to re-place SL
        assert mock_kotak.place_order.called


class TestReconciliation:
    """Tests for periodic order reconciliation."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_reconcile_missing_sl(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)

        # Position has SL order that's NOT in the order book
        pos = {
            "id": 100, "trade_id": 1,
            "trading_symbol": "SENSEX82000CE",
            "entry_price": 150.0, "current_price": 148.0,
            "quantity": 20, "trailing_sl": 140.0,
            "sl_order_id": "MISSING_SL",
            "status": "open",
        }
        rt._open_positions.append(pos)

        # Order book returns SL001 but NOT MISSING_SL
        mock_kotak.get_order_book.return_value = {
            "data": [{"nOrdNo": "SL001", "ordSt": "trigger pending"}]
        }
        mock_kotak.place_order.return_value = {"status": "ok", "data": {"nOrdNo": "SL_NEW"}}

        await rt.reconcile_orders()

        # Should have re-placed the SL
        assert mock_kotak.place_order.called
        assert pos["sl_order_id"] == "SL_NEW"


class TestRehydrate:
    """Tests for state restoration from DB."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_rehydrate_restores_state(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.get_pending_orders = AsyncMock(return_value=[
            {"id": 10, "trade_id": 1}
        ])
        mock_db.get_trades = AsyncMock(return_value=[
            {"id": 1, "signal_id": 42, "status": "pending",
             "trading_symbol": "SENSEX82000CE",
             "notes": "Real BUY 82000 CE @ 145-155",
             "quantity": 20, "price": 155.0}
        ])
        mock_db.get_positions = AsyncMock(return_value=[
            {"id": 100, "trade_id": 2, "trading_symbol": "SENSEX81500PE",
             "entry_price": 120.0, "current_price": 125.0, "pnl": 100.0,
             "quantity": 20, "max_ltp": 125.0, "trailing_sl": 115.0,
             "status": "open", "sl_activated": 0, "sl_order_id": "SL001"}
        ])

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.rehydrate_from_db()

        assert len(rt._pending_orders) == 1
        assert len(rt._open_positions) == 1
        assert rt._open_positions[0]["sl_order_id"] == "SL001"


class TestLotSize:
    """Test lot size multiplier."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_lot_size_multiplier(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=5, strategy=sample_strategy)

        order = rt._pending_orders[0]
        assert order["quantity"] == 100  # 5 lots * 20 multiplier


class TestRetryLogic:
    """Test order retry on failure."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_retry_on_order_failure(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)

        # First call fails, second succeeds
        mock_kotak.place_order.side_effect = [
            Exception("Network error"),
            {"status": "ok", "data": {"nOrdNo": "SL001"}},
        ]

        pos = {
            "id": 100, "trading_symbol": "SENSEX82000CE",
            "quantity": 20, "trailing_sl": 140.0,
        }

        sl_id = await rt._place_sl_order(pos, 140.0)
        assert sl_id == "SL001"
        assert mock_kotak.place_order.call_count == 2
