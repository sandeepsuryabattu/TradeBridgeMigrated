"""
Tests for MCX commodity support: parser, contract matching, routing, hours.
"""
import pytest
from datetime import time


class TestCommodityParser:
    def test_crude_full_format(self):
        from backend.instruments import parse_instrument_signal
        s = parse_instrument_signal("CRUDEOIL 9000 PE price 180-190 SL 150 targets 220/250")
        assert s.status == "valid"
        assert s.kind == "OPT"
        assert s.underlying == "CRUDEOIL"
        assert s.exchange_segment == "mcx_fo"
        assert s.strike == "9000" and s.option_type == "PE"
        assert s.entry_low == 180 and s.entry_high == 190
        assert s.stoploss == 150

    def test_crude_terse_format(self):
        from backend.instruments import parse_instrument_signal
        s = parse_instrument_signal("Crudeoil 9000 PE 186")
        assert s.status == "valid", s.reason
        assert s.underlying == "CRUDEOIL"
        assert s.exchange_segment == "mcx_fo"
        assert s.entry_low == 186 and s.entry_high == 186

    def test_gold_and_naturalgas(self):
        from backend.instruments import parse_instrument_signal
        g = parse_instrument_signal("GOLD 98000 CE price 500-520 SL 400")
        assert g.status == "valid" and g.underlying == "GOLD" and g.exchange_segment == "mcx_fo"
        n = parse_instrument_signal("Natural Gas 220 PE price 8-10 SL 5")
        assert n.status == "valid", n.reason
        assert n.underlying == "NATURALGAS" and n.exchange_segment == "mcx_fo"

    def test_index_still_requires_price_keyword(self):
        from backend.instruments import parse_instrument_signal
        s = parse_instrument_signal("Nifty 25700 CE 190")
        assert s.status == "ignored"

    def test_index_priority_over_commodity_word(self):
        from backend.instruments import parse_instrument_signal
        s = parse_instrument_signal("NIFTY 25700 CE price 180-190 SL 150")
        assert s.status == "valid" and s.underlying == "NIFTY"

    def test_commodity_never_claimed_as_equity(self):
        from backend.instruments import parse_instrument_signal
        s = parse_instrument_signal("BUY CRUDEOIL 186")
        assert not (s.status == "valid" and s.kind == "EQ" and s.underlying == "CRUDEOIL")


class TestCommodityRouting:
    def test_und_matches_commodity(self):
        from backend.contract_master import ContractMaster
        assert ContractMaster._und_matches("CRUDEOIL25NOV9000PE", "CRUDEOIL")
        assert not ContractMaster._und_matches("CRUDEOIL25NOV9000PE", "NIFTY")

    def test_mcx_in_multi_segments(self):
        from backend.contract_master import ContractMaster
        assert "mcx_fo" in ContractMaster.MULTI_SEGMENTS

    def test_market_hours(self):
        from backend.trade_manager import TradeManager
        from unittest.mock import patch
        with patch("backend.trade_manager.KotakTrader"), \
             patch("backend.trade_manager.MarketFeed"), \
             patch("backend.trade_manager.PaperTrader"), \
             patch("backend.trade_manager.RealTrader"), \
             patch("backend.trade_manager.ContractMaster"):
            tm = TradeManager()
        from unittest.mock import MagicMock
        with patch("backend.trade_manager.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = time(20, 0)
            assert tm._is_market_open("CRUDEOIL") is True
            assert tm._is_market_open("SENSEX") is False
        with patch("backend.trade_manager.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = time(10, 0)
            assert tm._is_market_open("CRUDEOIL") is True
            assert tm._is_market_open("SENSEX") is True

    def test_strategy_falls_back_to_default(self):
        from backend.trade_manager import TradeManager
        from unittest.mock import patch
        with patch("backend.trade_manager.KotakTrader"), \
             patch("backend.trade_manager.MarketFeed"), \
             patch("backend.trade_manager.PaperTrader"), \
             patch("backend.trade_manager.RealTrader"), \
             patch("backend.trade_manager.ContractMaster"):
            tm = TradeManager()
        tm.strategies = {"default": {"lots": 50}, "NIFTY": {"lots": 5}}
        assert tm.get_strategy("CRUDEOIL") == {"lots": 50}
