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

    # Default: order_history returns a successfully traded order
    # NOTE: kotak_trader.order_history() wraps the SDK result:
    # {"status": "ok", "data": {"data": [...]}}
    k.order_history.return_value = {
        "status": "ok",
        "data": {
            "data": [{
                "nOrdNo": "ORD001",
                "ordSt": "traded",
                "flPrc": "153.50",
                "flQty": "20",
            }],
        },
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


def _set_order_history_fill(mock_kotak, price=153.5, qty=20, status="traded"):
    """Helper to configure order_history mock for fill verification.
    Uses the real kotak_trader.order_history() wrapper format.
    """
    mock_kotak.order_history.return_value = {
        "status": "ok",
        "data": {
            "data": [{
                "nOrdNo": "ORD001",
                "ordSt": status,
                "flPrc": str(price),
                "flQty": str(qty),
            }],
        },
    }


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

        # Price bounces up by 5 pts (148 + 5 = 153) → triggers BUY + fill verification
        _set_order_history_fill(mock_kotak, price=153.5, qty=20)
        await rt.on_tick("12345", 153.0, tick)

        # Kotak place_order should have been called (the BUY)
        assert mock_kotak.place_order.called
        # order_history should have been called for fill verification
        assert mock_kotak.order_history.called
        # Position should be created with verified fill price
        assert len(rt._open_positions) == 1
        assert rt._open_positions[0]["entry_price"] == 153.5

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
        _set_order_history_fill(mock_kotak, price=153.5, qty=20)
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


class TestFillVerification:
    """Tests for IOC fill verification via order_history polling."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_rejected_buy_no_phantom_position(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        """When IOC BUY is rejected, no position should be created."""
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()
        mock_db.delete_pending_order = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        # Set order_history to return rejected
        _set_order_history_fill(mock_kotak, status="rejected")

        # Bounce triggers BUY → but fill verification sees rejected
        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)

        # BUY was placed on exchange
        assert mock_kotak.place_order.called
        # But NO position should have been created
        assert len(rt._open_positions) == 0
        # Trade should be marked expired
        mock_db.update_trade.assert_called()
        # Check the last update_trade call was status='expired'
        last_call = mock_db.update_trade.call_args_list[-1]
        assert last_call.args[1]["status"] == "expired"

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_verified_fill_uses_exchange_price(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        """Position should use the actual exchange fill price, not the bounce LTP."""
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

        # Exchange filled at 152.75 (different from bounce LTP of 153)
        _set_order_history_fill(mock_kotak, price=152.75, qty=20)

        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)  # bounce LTP = 153

        assert len(rt._open_positions) == 1
        # entry_price should be the verified exchange price, not the LTP
        assert rt._open_positions[0]["entry_price"] == 152.75

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_unconfirmed_fill_no_position(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        """When order_history never returns traded status, no position is created."""
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()
        mock_db.delete_pending_order = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        # order_history always returns 'pending' — IOC should not stay pending
        _set_order_history_fill(mock_kotak, status="pending")

        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)

        # No position created
        assert len(rt._open_positions) == 0
        # Trade marked expired
        last_call = mock_db.update_trade.call_args_list[-1]
        assert last_call.args[1]["status"] == "expired"


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
        _set_order_history_fill(mock_kotak, price=153.0, qty=20)

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


class TestDoubleNestedOrderHistory:
    """Regression tests for BUG 1: verify_fill with real kotak_trader wrapper format."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_verify_fill_with_wrapper_format(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        """_verify_fill must correctly unwrap {status:ok, data:{data:[...]}}."""
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()
        mock_db.save_position = AsyncMock(return_value=100)
        mock_db.delete_pending_order = AsyncMock()
        mock_db.update_position = AsyncMock()

        # Explicitly set the double-nested format (as kotak_trader.order_history returns)
        mock_kotak.order_history.return_value = {
            "status": "ok",
            "data": {
                "data": [{
                    "nOrdNo": "ORD001",
                    "ordSt": "traded",
                    "flPrc": "152.00",
                    "flQty": "20",
                }],
            },
        }

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)

        # Position must be created with the verified fill price
        assert len(rt._open_positions) == 1
        assert rt._open_positions[0]["entry_price"] == 152.00


class TestRehydrateCreatedAt:
    """Regression test for BUG 3: rehydrated orders must preserve DB created_at."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_rehydrate_preserves_created_at(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.get_pending_orders = AsyncMock(return_value=[
            {"id": 10, "trade_id": 1}
        ])
        mock_db.get_trades = AsyncMock(return_value=[
            {"id": 1, "signal_id": 42, "status": "pending",
             "trading_symbol": "SENSEX82000CE",
             "notes": "Real BUY 82000 CE @ 145-155",
             "quantity": 20, "price": 155.0,
             "created_at": "2026-03-25T05:00:00Z"}
        ])
        mock_db.get_positions = AsyncMock(return_value=[])

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.rehydrate_from_db()

        assert len(rt._pending_orders) == 1
        # Must use the DB timestamp, not datetime.now()
        assert rt._pending_orders[0]["created_at"] == "2026-03-25T05:00:00Z"


class TestSLCancelSkip:
    """Regression test for BUG 4: close_position skips SL cancel when exit_reason='sl'."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_sl_exit_does_not_cancel_sl_order(self, mock_db, mock_kotak, mock_market_feed):
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

        result = await rt.close_position(100, exit_price=139.5, exit_reason="sl")

        assert result["status"] == "closed"
        # cancel_order should NOT be called when SL already triggered
        mock_kotak.cancel_order.assert_not_called()


class TestTripleNestedProductionFormat:
    """Regression test for the actual Kotak production response format.

    Real order_history returns TRIPLE-nested:
    {"status":"ok","data":{"data":{"stat":"Ok","data":[{ordSt:"complete",avgPrc:"349.75",fldQty:20,...}]}}}
    """

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_verify_fill_triple_nested_complete(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        """_verify_fill parses triple-nested response with ordSt='complete'."""
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()
        mock_db.save_position = AsyncMock(return_value=100)
        mock_db.delete_pending_order = AsyncMock()
        mock_db.update_position = AsyncMock()

        # Exact production format (trimmed to relevant fields)
        mock_kotak.order_history.return_value = {
            "status": "ok",
            "data": {
                "data": {
                    "stat": "Ok",
                    "stCode": 200,
                    "data": [
                        {
                            "trdSym": "SENSEX26MAR76000PE",
                            "prc": "355.50",
                            "qty": 20,
                            "ordSt": "complete",
                            "trnsTp": "B",
                            "nOrdNo": "260325000470191",
                            "avgPrc": "349.75",
                            "fldQty": 20,
                            "unFldSz": 0,
                        },
                        {
                            "trdSym": "SENSEX26MAR76000PE",
                            "ordSt": "open",
                            "avgPrc": "0.00",
                            "fldQty": 0,
                            "nOrdNo": "260325000470191",
                        },
                        {
                            "trdSym": "SENSEX26MAR76000PE",
                            "ordSt": "open pending",
                            "avgPrc": "0.00",
                            "fldQty": 0,
                            "nOrdNo": "260325000470191",
                        },
                    ],
                },
            },
        }

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)

        # Must create position with avgPrc from production response
        assert len(rt._open_positions) == 1
        assert rt._open_positions[0]["entry_price"] == 349.75

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_verify_fill_direct_triple_nested(self, mock_db, mock_kotak, mock_market_feed):
        """Direct _verify_fill call with triple-nested production format."""
        from backend.real_trader import RealTrader

        mock_kotak.order_history.return_value = {
            "status": "ok",
            "data": {
                "data": {
                    "stat": "Ok",
                    "data": [{
                        "nOrdNo": "260325000470191",
                        "ordSt": "complete",
                        "avgPrc": "349.75",
                        "fldQty": 20,
                    }],
                },
            },
        }

        rt = RealTrader(kotak_trader=mock_kotak)
        rt._ws_broadcast = AsyncMock()

        order = {"trading_symbol": "SENSEX76000PE", "quantity": 20, "price": 355.50}
        result = await rt._verify_fill("260325000470191", order)

        assert result is not None
        assert result["status"] == "traded"
        assert result["fill_price"] == 349.75
        assert result["fill_qty"] == 20
