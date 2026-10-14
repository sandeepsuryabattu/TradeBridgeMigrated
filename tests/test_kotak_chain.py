"""Fixture tests for backend/kotak_chain.py (no creds, no network).

Uses synthetic contract-master rows + a fake Kotak client returning
Kotak-plausible quote shapes.
"""
import pytest

from backend import kotak_chain as kc
from backend.contract_master import ContractMaster


def _row(strike, opt, token, expiry="2027-12-30"):
    return {"pTrdSymbol": f"NIFTY{strike}{opt}", "pSymbol": token,
            "pStrikePrice": str(strike), "pOptionType": opt,
            "pExpiryDate": expiry}


def _cm(strikes=(25700, 25750, 25800, 25850, 25900)):
    cm = ContractMaster()
    rows = []
    for s in strikes:
        rows.append(_row(s, "CE", f"TOK-CE-{s}"))
        rows.append(_row(s, "PE", f"TOK-PE-{s}"))
    cm._segments = {"nse_fo": rows}
    return cm


class _FakeClient:
    def __init__(self, quotes, chain=None):
        self._quotes = quotes
        self._chain = chain

    def quotes(self, instrument_tokens=None, quote_type=None):
        assert quote_type == "all"
        out = []
        for t in instrument_tokens or []:
            tok = t["instrument_token"]
            q = dict(self._quotes.get(tok, {}))
            q.setdefault("instrument_token", tok)
            out.append(q)
        return out

    def option_chain(self, exchange=None, underlying=None,
                     instrument_type=None, count=None):
        if self._chain is None:
            raise AttributeError("no option_chain on this client")
        return self._chain


def _std_chain(strikes=(25700, 25750, 25800, 25850, 25900)):
    def leg(s, opt, ltp, oi, chg):
        return {"instrument": {"neoSymbol": f"nse_fo|{s}{opt}",
                               "symbol": f"NIFTY{s}{opt}",
                               "optionType": opt, "strikePrice": str(s),
                               "moneyness": "ATM" if s == 25800 else "OTM"},
                "quote": {"ltp": str(ltp), "open": "90", "high": "110",
                          "low": "80", "prevClose": "95", "close": None,
                          "volume": 100000},
                "openInterest": {"current": oi, "previous": oi - chg,
                                 "change": chg, "changePct": 5.0}}
    return {"data": {
        "common_data": {"mktLot": "65", "multiplier": "1",
                        "unlSymbol": "NIFTY", "exSeg": "nse_fo",
                        "expiryDt": "2027-12-30"},
        "call": [leg(s, "CE", 100.0 + s % 50, 1000 + s, 111 + s % 7)
                 for s in strikes],
        "put": [leg(s, "PE", 90.0, 2000 + s, -(222 + s % 9))
                for s in strikes]}}


def test_option_chain_source_carries_real_chgoi():
    snap = kc.chain_snapshot(_FakeClient({}, chain=_std_chain()),
                             _cm(), spot=25810.0, width=1)
    assert snap["source"] == "option_chain"
    assert [r["strike"] for r in snap["rows"]] == [25750, 25800, 25850]
    assert snap["atm"] == 25800
    atm_row = next(r for r in snap["rows"] if r["atm"])
    assert atm_row["callChgOI"] == 111 + 25800 % 7
    assert atm_row["putChgOI"] == -(222 + 25800 % 9)
    assert atm_row["callLTP"] == 100.0 + 25800 % 50
    assert "openInterest.change" in snap["quote_keys"]
    assert snap["expiry"] == "2027-12-30"
    assert snap["warnings"] == []


def test_quotes_fallback_when_no_option_chain():
    snap = kc.chain_snapshot(_FakeClient(_std_quotes()), _cm(),
                             spot=25810.0, width=1)
    assert snap["source"] == "quotes"


def _std_quotes():
    return {f"TOK-CE-{s}": {"instrument_token": f"TOK-CE-{s}",
                            "last_traded_price": 100.0 + s % 50,
                            "open_interest": 1000 + s,
                            "open_price": 99.0, "high_price": 102.0,
                            "low_price": 98.0, "close_price": 99.5,
                            "volume_traded_today": 5000}
            for s in (25700, 25750, 25800, 25850, 25900)} | \
           {f"TOK-PE-{s}": {"instrument_token": f"TOK-PE-{s}",
                            "last_traded_price": 90.0,
                            "open_interest": 2000 + s,
                            "open_price": 89.0, "high_price": 92.0,
                            "low_price": 88.0, "close_price": 89.5,
                            "volume_traded_today": 6000}
            for s in (25700, 25750, 25800, 25850, 25900)}


class _NoChainClient(_FakeClient):
    """Client without the native endpoint — forces the quotes fallback."""
    option_chain = None

    def __init__(self, quotes):
        super().__init__(quotes)


def test_snapshot_window_and_engine_rows():
    snap = kc.chain_snapshot(_NoChainClient(_std_quotes()), _cm(), spot=25810.0, width=1)
    assert [r["strike"] for r in snap["rows"]] == [25750, 25800, 25850]
    assert snap["atm"] == 25800
    atm_row = next(r for r in snap["rows"] if r["atm"])
    assert atm_row["callLTP"] == 100.0 + 25800 % 50
    assert atm_row["callChgOI"] == 0 and atm_row["putChgOI"] == 0  # documented
    assert "last_traded_price" in snap["quote_keys"]
    assert "open_interest" in snap["quote_keys"]
    assert snap["warnings"] == []


def test_unknown_keys_zero_gracefully_but_recorded():
    weird = {"TOK-CE-25800": {"instrument_token": "TOK-CE-25800", "LTP": 11, "OI2": 22},
             "TOK-PE-25800": {"instrument_token": "TOK-PE-25800"}}
    snap = kc.chain_snapshot(_FakeClient(weird), _cm(strikes=(25800,)), spot=25800.0, width=0)
    row = snap["rows"][0]
    assert row["callLTP"] == 0 and row["callOI"] == 0
    assert "LTP" in snap["quote_keys"]  # calibration capture works


def test_quotes_error_raises():
    class _Err:
        def quotes(self, instrument_tokens=None, quote_type=None):
            return {"error": [{"message": "bad"}]}
    with pytest.raises(ValueError):
        kc.chain_snapshot(_Err(), _cm(), spot=25800.0)


def test_no_pairs_raises():
    cm = ContractMaster()
    cm._segments = {"nse_fo": [_row(25800, "CE", "TOK-CE-25800")]}
    with pytest.raises(ValueError):
        kc.chain_snapshot(_FakeClient({}), cm, spot=25800.0)
