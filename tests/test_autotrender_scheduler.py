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

    def option_chain(self, exchange=None, underlying=None,
                     instrument_type=None, count=None):
        def leg(s, opt, ltp, oi, prev):
            return {"inst": {"neoSymbol": f"nse_fo|{s}{opt}",
                             "symbol": f"NIFTY{s}{opt}",
                             "optType": opt, "strkPrc": str(s)},
                    "quote": {"ltp": str(ltp)},
                    "oi": {"cur": oi, "prev": prev, "chg": oi - prev}}
        strikes = (25700, 25750, 25800, 25850, 25900)
        return {"common_data": {"exSeg": "nse_fo", "expiryDt": "2027-12-30"},
                "call": [leg(s, "CE", 120.0, 1000 + s, 950 + s) for s in strikes],
                "put": [leg(s, "PE", 100.0, 2000 + s, 2060 + s) for s in strikes]}


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


def test_fresh_breakout_jumps_queue_once(wired):
    b = _bullish_builder()  # ends in a HIGH breakout
    # Pretend the last fired call was same-bias but NOT breakout-driven.
    at._sched_last_bias = "BULLISH"
    at._sched_last_ts = IST_10AM.timestamp()
    at._sched_last_breakout = False
    res = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=b))
    assert res["action"] == "generated", res
    assert at._sched_last_breakout is True
    # Same episode persisting: suppressed again.
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


def test_quiet_tick_still_publishes_chain_and_engine(wired):
    b = CandleBuilder()
    t0 = 1_700_000_000 - 30 * 60
    for i in range(30):  # flat market → NEUTRAL, no trigger
        b._closed.setdefault("NIFTY", []).append(
            {"t": t0 + i * 60, "o": 25800, "h": 25802, "l": 25798, "c": 25800, "v": 1000})
    res = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=b))
    assert res["action"] == "skip"
    # Dashboard lights up even though nothing fired.
    assert at._last_live.get("ready") is True
    assert len(at._last_live.get("rows", [])) > 0
    eng = at._last_live.get("engine", {})
    assert eng["outcome"].startswith("no trigger")
    assert eng["bars"] == 30
    assert eng["auto_today"] == 0


def test_generated_tick_publishes_engine_verdict(wired):
    res = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=_bullish_builder()))
    assert res["action"] == "generated"
    eng = at._last_live.get("engine", {})
    assert eng["outcome"].startswith("generated")
    assert eng["fusion"]["fire"] is True
    assert eng["trigger"]["fire"] is True
    assert eng["auto_today"] == 1


def test_ticks_endpoint_lists_feed_ltp(wired):
    wired.market_feed._subscriptions = {
        "IDX:NIFTY": {"symbol": "NIFTY", "ltp": 22714.5,
                      "last_update": "2026-10-14T10:00:00"},
        "IDX:BANKNIFTY": {"symbol": "BANKNIFTY", "ltp": 0},  # skipped
    }
    res = asyncio.run(at.live_ticks())
    assert res["ticks"] == [{"key": "IDX:NIFTY", "symbol": "NIFTY",
                             "ltp": 22714.5,
                             "last_update": "2026-10-14T10:00:00"}]


def test_native_chain_source_flows_to_live_payload(wired):
    res = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=_bullish_builder()))
    assert res["action"] == "generated"
    assert at._last_live.get("chain_source") == "option_chain"
    atm_row = next(r for r in at._last_live["rows"] if r["atm"])
    assert atm_row["callChgOI"] == 50 and atm_row["putChgOI"] == -60
    assert wired.kotak.quote_calls == 0  # native endpoint, no per-leg quotes
