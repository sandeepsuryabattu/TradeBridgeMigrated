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
    """Tests for software SL placement and trailing."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_sl_order_placed_on_fill(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        """After entry fill, a software SL sentinel is set — no exchange order."""
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()
        mock_db.save_position = AsyncMock(return_value=100)
        mock_db.delete_pending_order = AsyncMock()
        mock_db.update_position = AsyncMock()

        mock_kotak.place_order.return_value = {"status": "ok", "data": {"nOrdNo": "ORD001"}}
        _set_order_history_fill(mock_kotak, price=153.0, qty=20)

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)  # bounce → fill

        # Only BUY placed — no exchange SL order
        sl_calls = [c for c in mock_kotak.place_order.call_args_list
                    if c.kwargs.get("order_type") == "SL"]
        assert len(sl_calls) == 0
        assert len(rt._open_positions) == 1
        assert rt._open_positions[0]["sl_order_id"].startswith("SW:")

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_trailing_sl_activation(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)

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
            "sl_order_id": "SW:100",
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
        # Software SL: no modify_order call
        assert not mock_kotak.modify_order.called
        assert pos["trailing_sl"] == 155.0  # anchored at entry + activation

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
            "sl_order_id": "SW:100",
            "exit_timer_mins": 10,
            "opened_at": datetime.now(timezone.utc),
            "exit_slippage": 1.0,
            "status": "open",
        }
        rt._open_positions.append(pos)

        tick = {"symbol": "SENSEX82000CE", "tk": "12345"}

        # New high at 160 → SL trails to 160 - 2 = 158 in memory
        await rt.on_tick("12345", 160.0, tick)

        assert pos["trailing_sl"] == 158.0
        # Software SL: no exchange call
        assert not mock_kotak.modify_order.called


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
        # Software SL: cancel_order is NEVER called (no exchange SL order exists)
        mock_kotak.cancel_order.assert_not_called()
        # Exit SELL IOC should still be placed
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




    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_close_position_unverified_exit_keeps_open(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()
        mock_db.update_trade = AsyncMock()

        # Exit order placed but never confirms traded
        mock_kotak.order_history.return_value = {
            "status": "ok",
            "data": {"data": [{"nOrdNo": "ORD001", "ordSt": "pending"}]},
        }

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
            "opened_at": datetime.now(timezone.utc),
            "status": "open",
        }
        rt._open_positions.append(pos)

        result = await rt.close_position(100, exit_price=148.0, exit_reason="timer")

        assert result["status"] == "error"
        assert any(p["id"] == 100 for p in rt._open_positions)

class TestOrderFeed:
    """Tests for order feed WebSocket event handling."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_sl_triggered_closes_position(self, mock_db, mock_kotak, mock_market_feed):
        """Software SL: tick at/below trailing_sl fires IOC SELL and closes position."""
        mock_db.update_position = AsyncMock()
        mock_db.update_trade = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        pos = {
            "id": 100, "trade_id": 1,
            "trading_symbol": "SENSEX82000CE",
            "entry_price": 150.0, "current_price": 141.0,
            "quantity": 20, "pnl": -200.0,
            "max_ltp": 155.0, "trailing_sl": 140.0,
            "sl_activated": True, "activation_points": 5.0,
            "trail_gap": 2.0, "sl_order_id": "SW:100",
            "exit_timer_mins": 10, "exit_slippage": 1.0,
            "opened_at": datetime.now(timezone.utc),
            "status": "open",
        }
        rt._open_positions.append(pos)

        # Exit SELL fill mock
        mock_kotak.order_history.return_value = {
            "status": "ok",
            "data": {"data": [{"nOrdNo": "SELL001", "ordSt": "complete",
                               "avgPrc": "139.50", "fldQty": 20}]},
        }
        mock_kotak.place_order.return_value = {"status": "ok", "data": {"nOrdNo": "SELL001"}}

        # Tick at SL level → software SL fires IOC SELL
        tick = {"symbol": "SENSEX82000CE", "tk": "12345"}
        await rt.on_tick("12345", 140.0, tick)

        assert len(rt._open_positions) == 0
        mock_db.update_position.assert_called()

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_sl_rejected_replaces(self, mock_db, mock_kotak, mock_market_feed):
        """Software SL: order_feed rejected events for sl_order_id are ignored (no exchange SL)."""
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        pos = {
            "id": 100, "trade_id": 1,
            "trading_symbol": "SENSEX82000CE",
            "entry_price": 150.0, "current_price": 145.0,
            "quantity": 20, "pnl": -100.0,
            "max_ltp": 155.0, "trailing_sl": 140.0,
            "sl_activated": True, "activation_points": 5.0,
            "trail_gap": 2.0, "sl_order_id": "SW:100",
            "exit_timer_mins": 10, "exit_slippage": 1.0,
            "opened_at": datetime.now(timezone.utc),
            "status": "open",
        }
        rt._open_positions.append(pos)

        # Order feed "rejected" on BUY entry order — should be resolved via waiter
        await rt.handle_order_feed({
            "data": {"nOrdNo": "BUY001", "ordSt": "rejected", "rejRsn": "Insufficient margin"}
        })

        # Position remains open (software SL unaffected by order feed)
        assert len(rt._open_positions) == 1
        # No new exchange SL order placed
        assert not mock_kotak.place_order.called


class TestClosePositionNoReason:
    """Tests for close_position when exit_reason is None."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_close_position_no_reason_still_sells(self, mock_db, mock_kotak, mock_market_feed):
        """close_position with exit_reason=None should still place a SELL order."""
        mock_db.update_position = AsyncMock()
        mock_db.update_trade = AsyncMock()

        # Exit fill verification
        mock_kotak.order_history.return_value = {
            "status": "ok",
            "data": {"data": [{"nOrdNo": "SELL001", "ordSt": "traded", "flPrc": "148.0", "flQty": "20"}]},
        }

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
            "opened_at": datetime.now(timezone.utc),
            "status": "open",
        }
        rt._open_positions.append(pos)

        result = await rt.close_position(100, exit_price=148.0)  # No exit_reason

        # Should still place SELL + close
        sell_calls = [c for c in mock_kotak.place_order.call_args_list
                      if c.kwargs.get("transaction_type") == "S"]
        assert len(sell_calls) == 1
        assert result["status"] == "closed"


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
             "status": "open", "sl_activated": 0, "sl_order_id": "SW:100"}
        ])

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.rehydrate_from_db()

        assert len(rt._pending_orders) == 1
        assert len(rt._open_positions) == 1
        # Software SL: sentinel preserved as-is on rehydration
        assert rt._open_positions[0]["sl_order_id"] == "SW:100"

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_rehydrate_restores_runtime_controls(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.get_pending_orders = AsyncMock(return_value=[
            {"id": 10, "trade_id": 1}
        ])
        mock_db.get_trades = AsyncMock(return_value=[
            {
                "id": 1, "signal_id": 42, "status": "pending",
                "trading_symbol": "SENSEX82000CE",
                "notes": "Real BUY 82000 CE @ 145-155",
                "quantity": 20, "price": 155.0,
                "entry_timer_mins": 3,
                "exit_timer_mins": 7,
                "entry_slippage": 2.5,
                "exit_slippage": 1.7,
            }
        ])
        mock_db.get_positions = AsyncMock(return_value=[
            {
                "id": 100, "trade_id": 2, "trading_symbol": "SENSEX81500PE",
                "entry_price": 120.0, "current_price": 125.0, "pnl": 100.0,
                "quantity": 20, "max_ltp": 125.0, "trailing_sl": 115.0,
                "status": "open", "sl_activated": 0, "sl_order_id": "SL001",
                "exit_timer_mins": 11, "exit_slippage": 0.9,
            }
        ])

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.rehydrate_from_db()

        assert rt._pending_orders[0]["entry_timer_mins"] == 3
        assert rt._pending_orders[0]["exit_timer_mins"] == 7
        assert rt._pending_orders[0]["entry_slippage"] == 2.5
        assert rt._pending_orders[0]["exit_slippage"] == 1.7
        assert rt._open_positions[0]["exit_timer_mins"] == 11
        assert rt._open_positions[0]["exit_slippage"] == 0.9


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
    """Test software SL sentinel creation."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_retry_on_order_failure(self, mock_db, mock_kotak, mock_market_feed):
        """Software SL: _place_sl_order returns SW: sentinel immediately (no Kotak call)."""
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)

        pos = {
            "id": 100, "trading_symbol": "SENSEX82000CE",
            "quantity": 20, "trailing_sl": 140.0,
        }

        sl_id = await rt._place_sl_order(pos, 140.0)
        assert sl_id == "SW:100"  # Sentinel = SW:<position_id>
        assert not mock_kotak.place_order.called  # No exchange order

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_sl_protect_retry_budget_5x_0_2(self, mock_db, mock_kotak, mock_market_feed):
        """Software SL: _place_sl_order always succeeds (no exchange to reject)."""
        mock_db.update_position = AsyncMock()
        rt = make_trader(mock_kotak, mock_market_feed)

        pos = {
            "id": 100, "trading_symbol": "SENSEX82000CE",
            "quantity": 20, "trailing_sl": 140.0,
        }

        sl_id = await rt._place_sl_order(pos, 140.0)
        assert sl_id == "SW:100"
        assert not mock_kotak.place_order.called


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


class TestFeedFirstEntryConfirmation:
    """Feed-first fill confirmation with polling fallback."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_confirm_entry_fill_prefers_order_feed(self, mock_db, mock_kotak, mock_market_feed):
        rt = make_trader(mock_kotak, mock_market_feed)

        async def slow_poll(order_id, order):
            await asyncio.sleep(0.3)
            return {"status": "traded", "fill_price": 999.0, "fill_qty": 1}

        rt._poll_order_fill = AsyncMock(side_effect=slow_poll)

        order = {"trading_symbol": "SENSEX26MAR76000PE", "quantity": 20, "price": 355.5}
        task = asyncio.create_task(rt._confirm_entry_fill("ORD-FEED-1", order))
        await asyncio.sleep(0)

        await rt.handle_order_feed({
            "data": {"nOrdNo": "ORD-FEED-1", "ordSt": "complete", "avgPrc": "349.75", "fldQty": 20}
        })

        result = await task
        assert result is not None
        assert result["status"] == "traded"
        assert result["fill_price"] == pytest.approx(349.75)
        assert result["fill_qty"] == 20
        assert result["confirm_source"] == "feed"

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_confirm_entry_fill_falls_back_to_polling(self, mock_db, mock_kotak, mock_market_feed):
        rt = make_trader(mock_kotak, mock_market_feed)
        rt._poll_order_fill = AsyncMock(return_value={"status": "traded", "fill_price": 152.0, "fill_qty": 20})

        order = {"trading_symbol": "SENSEX26MAR76000PE", "quantity": 20, "price": 355.5}
        result = await rt._confirm_entry_fill("ORD-POLL-1", order)

        assert result is not None
        assert result["status"] == "traded"
        assert result["fill_price"] == pytest.approx(152.0)
        assert result["fill_qty"] == 20
        assert result["confirm_source"] == "poll"
        rt._poll_order_fill.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_confirm_entry_fill_handles_feed_rejected(self, mock_db, mock_kotak, mock_market_feed):
        rt = make_trader(mock_kotak, mock_market_feed)

        async def slow_poll(order_id, order):
            await asyncio.sleep(0.3)
            return {"status": "traded", "fill_price": 999.0, "fill_qty": 1}

        rt._poll_order_fill = AsyncMock(side_effect=slow_poll)

        order = {"trading_symbol": "SENSEX26MAR76000PE", "quantity": 20, "price": 355.5}
        task = asyncio.create_task(rt._confirm_entry_fill("ORD-REJ-1", order))
        await asyncio.sleep(0)

        await rt.handle_order_feed({
            "data": {"nOrdNo": "ORD-REJ-1", "ordSt": "rejected", "rejRsn": "price out of range"}
        })

        result = await task
        assert result is None


    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_confirm_entry_fill_timeout_returns_none(self, mock_db, mock_kotak, mock_market_feed):
        import backend.real_trader as rt_mod
        rt = make_trader(mock_kotak, mock_market_feed)

        async def very_slow_poll(order_id, order, **kwargs):
            await asyncio.sleep(1.0)
            return {"status": "traded", "fill_price": 111.0, "fill_qty": 20}

        rt._poll_order_fill = AsyncMock(side_effect=very_slow_poll)

        old_timeout = rt_mod.FILL_CONFIRM_TIMEOUT_S
        rt_mod.FILL_CONFIRM_TIMEOUT_S = 0.05
        try:
            order = {"trading_symbol": "SENSEX26MAR76000PE", "quantity": 20, "price": 355.5}
            result = await rt._confirm_entry_fill("ORD-TIMEOUT-1", order)
            assert result is None
        finally:
            rt_mod.FILL_CONFIRM_TIMEOUT_S = old_timeout



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
        result = await rt._poll_order_fill("260325000470191", order, label="entry")

        assert result is not None
        assert result["status"] == "traded"
        assert result["fill_price"] == 349.75
        assert result["fill_qty"] == 20


class TestRealResponseSafety:
    """Additional regressions for nested broker responses and safety paths."""

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_place_order_missing_nordno_is_treated_as_failure(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()

        # BUY accept response missing nOrdNo
        mock_kotak.place_order.return_value = {"status": "ok", "data": {"stat": "Ok"}}

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)

        # No position must be created when broker order id is missing
        assert len(rt._open_positions) == 0

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_cancel_not_ok_not_treated_as_cancelled(self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy):
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()

        # BUY placement ok with order id
        mock_kotak.place_order.return_value = {"status": "ok", "data": {"nOrdNo": "ORD001"}}
        # Fill remains pending so cancel path triggers
        mock_kotak.order_history.return_value = {
            "status": "ok",
            "data": {"data": [{"nOrdNo": "ORD001", "ordSt": "pending"}]},
        }
        # Cancel wrapper ok but broker says Not_Ok
        mock_kotak.cancel_order.return_value = {
            "status": "ok",
            "data": {"stat": "Not_Ok", "errMsg": "Order already executed"},
        }

        rt = make_trader(mock_kotak, mock_market_feed)
        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)

        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)

        # Should NOT mark expired via false "cancelled" assumption
        expired_calls = [c for c in mock_db.update_trade.call_args_list if c.args[1].get("status") == "expired"]
        assert len(expired_calls) == 0

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_order_feed_complete_closes_sl_position(self, mock_db, mock_kotak, mock_market_feed):
        """Software SL: tick at/below trailing_sl fires IOC SELL and closes position."""
        mock_db.update_position = AsyncMock()
        mock_db.update_trade = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        pos = {
            "id": 100, "trade_id": 1,
            "trading_symbol": "SENSEX82000CE",
            "entry_price": 150.0, "current_price": 141.0,
            "quantity": 20, "pnl": -200.0,
            "max_ltp": 155.0, "trailing_sl": 140.0,
            "sl_activated": True, "activation_points": 5.0,
            "trail_gap": 2.0, "sl_order_id": "SW:100",
            "exit_timer_mins": 10, "exit_slippage": 1.0,
            "opened_at": datetime.now(timezone.utc),
            "status": "open",
        }
        rt._open_positions.append(pos)

        mock_kotak.place_order.return_value = {"status": "ok", "data": {"nOrdNo": "SELL001"}}
        mock_kotak.order_history.return_value = {
            "status": "ok",
            "data": {"data": [{"nOrdNo": "SELL001", "ordSt": "complete",
                               "avgPrc": "139.50", "fldQty": 20}]},
        }
        tick = {"symbol": "SENSEX82000CE", "tk": "12345"}
        await rt.on_tick("12345", 140.0, tick)

        assert len(rt._open_positions) == 0

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_close_position_marks_trade_closed(self, mock_db, mock_kotak, mock_market_feed):
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
            "opened_at": datetime.now(timezone.utc),
            "status": "open",
        }
        rt._open_positions.append(pos)

        await rt.close_position(100, exit_price=148.0, exit_reason="timer")

        assert mock_db.update_trade.called
        payload = mock_db.update_trade.call_args_list[-1].args[1]
        assert payload.get("status") == "closed"


class TestSLSafety:
    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_fill_order_sl_fail_attempts_emergency_exit(self, mock_db, mock_kotak, mock_market_feed):
        """Software SL: _fill_order always succeeds (sentinel never fails), no emergency exit needed."""
        mock_db.update_trade = AsyncMock()
        mock_db.save_position = AsyncMock(return_value=100)
        mock_db.delete_pending_order = AsyncMock()
        mock_db.update_position = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        rt.close_position = AsyncMock(return_value={"status": "closed"})

        order = {
            "trade_id": 1,
            "pending_order_id": 10,
            "trading_symbol": "SENSEX82000CE",
            "quantity": 20,
            "signal_id": 42,
            "activation_points": 5.0,
            "trail_gap": 2.0,
            "exit_timer_mins": 10,
            "exit_slippage": 1.0,
            "signal_trail_initial_sl": "telegram",
            "signal_stoploss": 140.0,
        }

        await rt._fill_order(order, 150.0, filled_qty=20)

        # Software SL always sets SW: sentinel — no emergency exit
        assert len(rt._open_positions) == 1
        assert rt._open_positions[0]["sl_order_id"].startswith("SW:")
        rt.close_position.assert_not_awaited()


class TestClosePositionSafety:
    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_close_position_is_idempotent_during_inflight_close(self, mock_db, mock_kotak, mock_market_feed):
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
            "opened_at": datetime.now(timezone.utc),
            "status": "open",
        }
        rt._open_positions.append(pos)

        async def slow_verify(*args, **kwargs):
            await asyncio.sleep(0.05)
            return {"status": "traded", "fill_price": 148.0, "fill_qty": 20}

        rt._verify_exit_fill = AsyncMock(side_effect=slow_verify)

        r1, r2 = await asyncio.gather(
            rt.close_position(100, exit_price=148.0, exit_reason="timer"),
            rt.close_position(100, exit_price=148.0, exit_reason="timer"),
        )

        statuses = {r1.get("status"), r2.get("status")}
        assert "closed" in statuses
        assert "error" in statuses

        sell_calls = [
            c for c in mock_kotak.place_order.call_args_list
            if c.kwargs.get("transaction_type") == "S"
        ]
        assert len(sell_calls) == 1

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_close_position_db_failure_keeps_not_closed_state(self, mock_db, mock_kotak, mock_market_feed):
        mock_db.update_position = AsyncMock()
        mock_db.update_trade = AsyncMock(side_effect=Exception("db down"))

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
            "opened_at": datetime.now(timezone.utc),
            "status": "open",
        }
        rt._open_positions.append(pos)
        rt._verify_exit_fill = AsyncMock(return_value={"status": "traded", "fill_price": 148.0, "fill_qty": 20})

        result = await rt.close_position(100, exit_price=148.0, exit_reason="timer")

        assert result["status"] == "error"
        assert any(p["id"] == 100 for p in rt._open_positions)
        assert rt._open_positions[0]["status"] == "closing"


# ── Regression: mode-guard and cross-mode dedup (2026-03-27) ─────────────────

class TestModeGuard:
    """
    Regression tests for the mode-guard on on_tick().
    real_trader.on_tick() must be a no-op when mode is 'paper'.
    paper_trader.on_tick() must be a no-op when mode is 'real'.
    """

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_real_trader_on_tick_blocked_in_paper_mode(
        self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy
    ):
        """
        REGRESSION: real_trader.on_tick must return immediately in paper mode.
        Before fix: both traders received ticks regardless of current mode.
        """
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()

        rt = make_trader(mock_kotak, mock_market_feed)
        rt.set_active_mode_fn(lambda: "paper")  # mode guard: paper

        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)
        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)

        assert not mock_kotak.place_order.called
        assert len(rt._pending_orders) == 1
        assert len(rt._open_positions) == 0

    @pytest.mark.asyncio
    @patch("backend.paper_trader.db")
    async def test_paper_trader_on_tick_blocked_in_real_mode(self, mock_db):
        """
        REGRESSION: paper_trader.on_tick must return immediately in real mode.
        """
        from backend.paper_trader import PaperTrader

        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()

        pt = PaperTrader()
        pt.set_active_mode_fn(lambda: "real")  # mode guard: real

        signal = {
            "strike": "82000", "option_type": "CE",
            "entry_low": 145, "entry_high": 155,
            "stoploss": 140, "contract_lot_size": 20, "entry_label": "test",
        }
        await pt.place_order(signal, signal_id=99, lot_size=1, strategy={})
        symbol = pt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        await pt.on_tick("12345", 148.0, tick)
        await pt.on_tick("12345", 153.0, tick)

        assert len(pt._open_positions) == 0
        assert len(pt._pending_orders) == 1

    @pytest.mark.asyncio
    @patch("backend.real_trader.db")
    async def test_real_trader_on_tick_active_in_real_mode(
        self, mock_db, mock_kotak, mock_market_feed, sample_signal, sample_strategy
    ):
        """Mode guard must NOT block on_tick when mode matches ('real')."""
        mock_db.save_trade = AsyncMock(return_value=1)
        mock_db.save_pending_order = AsyncMock(return_value=10)
        mock_db.update_trade = AsyncMock()
        mock_db.save_position = AsyncMock(return_value=100)
        mock_db.delete_pending_order = AsyncMock()
        mock_db.update_position = AsyncMock()

        _set_order_history_fill(mock_kotak, price=153.5, qty=20)

        rt = make_trader(mock_kotak, mock_market_feed)
        rt.set_active_mode_fn(lambda: "real")

        await rt.place_order(sample_signal, signal_id=42, lot_size=1, strategy=sample_strategy)
        symbol = rt._pending_orders[0]["trading_symbol"]
        tick = {"symbol": symbol, "tk": "12345"}

        await rt.on_tick("12345", 148.0, tick)
        await rt.on_tick("12345", 153.0, tick)

        assert mock_kotak.place_order.called
        assert len(rt._open_positions) == 1
