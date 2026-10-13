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

    def test_crude_terse_range_formats(self):
        from backend.instruments import parse_instrument_signal
        for text in ("CRUDEOIL 9000 PE 180-190",
                     "CRUDEOIL 9000 PE 180/190",
                     "CRUDEOIL 9000 PE 180 190",
                     "CRUDEOIL 9000 PE 190-180"):
            s = parse_instrument_signal(text)
            assert s.status == "valid", (text, s.reason)
            assert (s.entry_low, s.entry_high) == (180, 190), text

    def test_crude_terse_entry_plus_sl(self):
        from backend.instruments import parse_instrument_signal
        s = parse_instrument_signal("CRUDEOIL 9000 PE 186 SL 150")
        assert s.status == "valid", s.reason
        assert (s.entry_low, s.entry_high) == (186, 186)
        assert s.stoploss == 150
        r = parse_instrument_signal("CRUDEOIL 9000 PE 180-190 SL 150")
        assert r.status == "valid" and (r.entry_low, r.entry_high) == (180, 190)
        assert r.stoploss == 150

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

    def test_mcx_symbol_expiry_day_first(self):
        from backend.contract_master import ContractMaster
        assert ContractMaster._symbol_expiry("GOLD30OCT26143500CE", "GOLD") == (2026, 10, 30)
        assert ContractMaster._symbol_expiry("CRUDEOILM15OCT264300PE", "CRUDEOIL") == (2026, 10, 15)
        # Index parsing untouched
        assert ContractMaster._symbol_expiry("NIFTY26OCT22450CE", "NIFTY") == (2026, 10, None)
        assert ContractMaster._symbol_expiry("NIFTY26O1322450CE", "NIFTY") == (2026, 10, 13)

    def test_mini_keyword_selects_mini(self):
        from backend.instruments import parse_instrument_signal
        s = parse_instrument_signal("GOLD MINI 143500 CE price 1500-1550 SL 1200")
        assert s.status == "valid" and s.variant == "mini"
        assert s.display_symbol == "GOLDM143500CE"
        std = parse_instrument_signal("GOLD 143500 CE price 1500-1550 SL 1200")
        assert std.variant == "standard"
        assert std.display_symbol == "GOLD143500CE"

    def test_standard_preferred_over_mini(self):
        from backend.contract_master import ContractMaster
        cm = ContractMaster()
        rows = [
            {"pTrdSymbol": "GOLDM29OCT26143500CE", "pOptionType": "CE",
             "dStrikePrice;": "14350000", "pExpiryDate": "1795823999", "lLotSize": "100"},
            {"pTrdSymbol": "GOLD30OCT26143500CE", "pOptionType": "CE",
             "dStrikePrice;": "14350000", "pExpiryDate": "1793404799", "lLotSize": "1"},
        ]
        cm._segments["mcx_fo"] = rows
        out = cm.resolve({"kind": "OPT", "underlying": "GOLD", "strike": "143500",
                          "option_type": "CE", "exchange_segment": "mcx_fo", "expiry": None})
        assert out["trading_symbol"] == "GOLD30OCT26143500CE"
        assert out["lot_size"] == "1"
        # Mini-only universe still resolves (fallback, never None)
        cm._segments["mcx_fo"] = [rows[0]]
        out = cm.resolve({"kind": "OPT", "underlying": "GOLD", "strike": "143500",
                          "option_type": "CE", "exchange_segment": "mcx_fo", "expiry": None})
        assert out["trading_symbol"] == "GOLDM29OCT26143500CE"

    def test_find_by_symbol_exact(self):
        from backend.contract_master import ContractMaster
        cm = ContractMaster()
        cm._segments = {"mcx_fo": [
            {"pTrdSymbol": "CRUDEOILM15OCT269000PE", "pSymbol": "TKN1",
             "pStrikePrice": "900000", "pOptionType": "PE",
             "pExpiryDate": "1793404799", "lLotSize": "10"},
            {"pTrdSymbol": "CRUDEOIL15OCT269000PE", "pSymbol": "TKN2",
             "pStrikePrice": "900000", "pOptionType": "PE",
             "pExpiryDate": "1793404799", "lLotSize": "100"},
        ]}
        found = cm.find_by_symbol("CRUDEOILM15OCT269000PE", segment="mcx_fo")
        assert found and found["instrument_token"] == "TKN1"
        assert found["exchange_segment"] == "mcx_fo"
        assert cm.find_by_symbol("CRUDEOIL15OCT269000PE")["instrument_token"] == "TKN2"
        assert cm.find_by_symbol("NOPE123") is None
        assert cm.find_by_symbol("") is None

    def test_variant_mini_pins_mini(self):
        from backend.contract_master import ContractMaster
        cm = ContractMaster()
        rows = [
            {"pTrdSymbol": "GOLDM29OCT26143500CE", "pOptionType": "CE",
             "dStrikePrice;": "14350000", "pExpiryDate": "1795823999", "lLotSize": "100"},
            {"pTrdSymbol": "GOLD30OCT26143500CE", "pOptionType": "CE",
             "dStrikePrice;": "14350000", "pExpiryDate": "1793404799", "lLotSize": "1"},
        ]
        cm._segments["mcx_fo"] = rows
        out = cm.resolve({"kind": "OPT", "underlying": "GOLD", "strike": "143500",
                          "option_type": "CE", "exchange_segment": "mcx_fo",
                          "expiry": None, "variant": "mini"})
        assert out["trading_symbol"] == "GOLDM29OCT26143500CE"

    def test_mcx_in_multi_segments(self):
        from backend.contract_master import ContractMaster
        assert "mcx_fo" in ContractMaster.MULTI_SEGMENTS

    def test_guinea_never_wins_standard_fut(self):
        """Live bug 2026-10-13: GOLDM FUT resolved to GOLDGUINEA30OCT26FUT.
        Micro products (Guinea/Petal) must lose to standard even with a
        nearer expiry; minis beat micros as fallback."""
        from backend.contract_master import ContractMaster
        cm = ContractMaster()
        rows = [
            {"pTrdSymbol": "GOLDGUINEA30OCT26FUT", "pSymbol": "T Guinea",
             "pOptionType": "", "pExpiryDate": "1793404799", "lLotSize": "8"},
            {"pTrdSymbol": "GOLDM05NOV26FUT", "pSymbol": "T Mini",
             "pOptionType": "", "pExpiryDate": "1793404799", "lLotSize": "100"},
            {"pTrdSymbol": "GOLD05DEC26FUT", "pSymbol": "T Std",
             "pOptionType": "", "pExpiryDate": "1796428799", "lLotSize": "1000"},
        ]
        cm._segments["mcx_fo"] = rows
        out = cm.resolve({"kind": "FUT", "underlying": "GOLD",
                          "exchange_segment": "mcx_fo", "expiry": None})
        assert out["trading_symbol"] == "GOLD05DEC26FUT", out["trading_symbol"]
        # No standard product: mini wins over Guinea
        cm._segments["mcx_fo"] = rows[:2]
        out = cm.resolve({"kind": "FUT", "underlying": "GOLD",
                          "exchange_segment": "mcx_fo", "expiry": None})
        assert out["trading_symbol"] == "GOLDM05NOV26FUT", out["trading_symbol"]

    def test_fut_expiry_judged_over_futs_only(self):
        """Option expiries must not poison FUT resolution: Oct-30 standard
        options exist but the nearest standard FUT is Dec — resolve the FUT,
        never None (which degrades to the 'GOLDFUT' display placeholder)."""
        from backend.contract_master import ContractMaster
        cm = ContractMaster()
        cm._segments["mcx_fo"] = [
            {"pTrdSymbol": "GOLD30OCT26143500CE", "pSymbol": "T Opt",
             "pOptionType": "CE", "dStrikePrice;": "14350000",
             "pExpiryDate": "1793404799", "lLotSize": "1"},
            {"pTrdSymbol": "GOLDGUINEA30OCT26FUT", "pSymbol": "T Guinea",
             "pOptionType": "", "pExpiryDate": "1793404799", "lLotSize": "8"},
            {"pTrdSymbol": "GOLD04DEC26FUT", "pSymbol": "T Std",
             "pOptionType": "", "pExpiryDate": "1796428799", "lLotSize": "1000"},
        ]
        out = cm.resolve({"kind": "FUT", "underlying": "GOLD",
                          "exchange_segment": "mcx_fo", "expiry": None})
        assert out is not None and out["trading_symbol"] == "GOLD04DEC26FUT", out

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
