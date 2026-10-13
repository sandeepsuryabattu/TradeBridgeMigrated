"""Tests for the autonomous scheduler (fakes only — no network, no Kotak creds)."""
import asyncio
from datetime import datetime, timezone

import pytest

from backend import autotrender as at
from backend.candle_builder import CandleBuilder
from backend.contract_master import ContractMaster


def _cm(strikes=(25700, 25750, 25800, 25850, 25900)):
    cm = ContractMaster()
    rows = []
    for s in strikes:
        for opt in ("CE", "PE"):
            rows.append({"pTrdSymbol": f"NIFTY{s}{opt}", "pSymbol": f"TOK-{opt}-{s}",
                         "pStrikePrice": str(s), "pOptionType": opt,
                         "pExpiryDate": "2027-12-30"})
    cm._segments = {"nse_fo": rows}
    return cm


def _quotes_for(strikes):
    d = {}
    for s in strikes:
        d[f"TOK-CE-{s}"] = {"instrument_token": f"TOK-CE-{s}",
                            "last_traded_price": 120.0, "open_interest": 1000 + s,
                            "open_price": 118.0, "high_price": 122.0,
                            "low_price": 117.0, "close_price": 119.0,
                            "volume_traded_today": 5000}
        d[f"TOK-PE-{s}"] = {"instrument_token": f"TOK-PE-{s}",
                            "last_traded_price": 100.0, "open_interest": 2000 + s,
                            "open_price": 99.0, "high_price": 102.0,
                            "low_price": 98.0, "close_price": 99.5,
                            "volume_traded_today": 6000}
    return d


class _FakeKotak:
    is_authenticated = True

    def __init__(self, quotes):
        self.client = self
        self._quotes = quotes
        self.quote_calls = 0

    def quotes(self, instrument_tokens=None, quote_type=None):
        self.quote_calls += 1
        return [dict(self._quotes[t["instrument_token"]],
                     instrument_token=t["instrument_token"])
                for t in instrument_tokens]


class _FakeFeed:
    def __init__(self):
        self.callbacks = []

    def add_tick_callback(self, cb):
        if cb not in self.callbacks:
            self.callbacks.append(cb)


class _FakeManager:
    def __init__(self, mode="paper", quotes=None):
        self.mode = mode
        self.stop_trading = False
        self.market_feed = _FakeFeed()
        self.kotak = _FakeKotak(quotes or {})
        self.contract_master = _cm()
        self.process_calls = []

    async def process_message(self, text, sender="", timestamp=""):
        self.process_calls.append({"text": text, "sender": sender})
        return {"message_id": 100 + len(self.process_calls)}


def _bullish_builder(base=25700.0, n=30):
    b = CandleBuilder(timeframe_secs=60)
    t0 = 1_700_000_000 - (n * 60)
    for i in range(n):
        c = base + i * 8
        # write directly to keep exact OHLC shape
        b._closed.setdefault("NIFTY", []).append(
            {"t": t0 + i * 60, "o": c - 2, "h": c + 3, "l": c - 3, "c": c, "v": 1000})
    return b


IST_10AM = datetime(2026, 10, 14, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
IST_8AM = datetime(2026, 10, 14, 2, 30, tzinfo=timezone.utc)    # 08:00 IST


@pytest.fixture()
def wired(monkeypatch):
    mgr = _FakeManager(quotes=_quotes_for((25700, 25750, 25800, 25850, 25900)))
    monkeypatch.setattr(at, "_manager", mgr)
    monkeypatch.setattr(at, "_broadcast", None)
    monkeypatch.setattr(at.db, "save_message",
                        lambda *a, **k: asyncio.sleep(0, result=777))
    at._reset_scheduler_state()
    at._calls.clear()
    return mgr


def test_generates_call_end_to_end(wired):
    res = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=_bullish_builder()))
    assert res["action"] == "generated", res
    call = res["call"]
    assert call["side"] == "BUY CE"
    assert call["status"] == "sent_to_paper"
    assert len(wired.process_calls) == 1
    assert "NIFTY" in wired.process_calls[0]["text"]
    assert wired.process_calls[0]["sender"] == "Autotrender"


def test_cooldown_suppresses_repeat(wired):
    b = _bullish_builder()
    assert asyncio.run(at.scheduler_tick(now=IST_10AM, builder=b))["action"] == "generated"
    res2 = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=b))
    assert res2 == {"action": "skip", "reason": "cooldown"}
    assert len(wired.process_calls) == 1


def test_real_mode_never_trades(wired):
    wired.mode = "real"
    res = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=_bullish_builder()))
    assert res["reason"] == "not paper mode"
    assert wired.process_calls == []


def test_stop_trading_blocks(wired):
    wired.stop_trading = True
    res = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=_bullish_builder()))
    assert res["reason"] == "stop trading on"
    assert wired.process_calls == []


def test_outside_hours_and_warmup(wired):
    res = asyncio.run(at.scheduler_tick(now=IST_8AM, builder=_bullish_builder()))
    assert res["reason"] == "outside market hours"
    thin = CandleBuilder()
    thin.update("NIFTY", 25700, ts=1_700_000_000)
    res2 = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=thin))
    assert res2["reason"].startswith("warming up")
    assert wired.process_calls == []


def test_neutral_bias_skips(wired):
    b = CandleBuilder()
    t0 = 1_700_000_000 - 30 * 60
    for i in range(30):  # flat market → NEUTRAL
        b._closed.setdefault("NIFTY", []).append(
            {"t": t0 + i * 60, "o": 25800, "h": 25802, "l": 25798, "c": 25800, "v": 1000})
    res = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=b))
    assert res["action"] == "skip" and res["reason"].startswith("no trigger")
    assert wired.process_calls == []
