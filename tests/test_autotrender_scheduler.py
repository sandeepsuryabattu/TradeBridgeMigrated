"""Tests for the autonomous scheduler (fakes only — no network, no Kotak creds)."""
import asyncio
import json
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
                     instrument_type=None, count=None, expiry=None):
        def leg(s, opt, ltp, oi, prev):
            return {"inst": {"neoSymbol": f"nse_fo|{s}{opt}",
                             "symbol": f"NIFTY{s}{opt}",
                             "optType": opt, "strkPrc": str(s)},
                    "quote": {"ltp": str(ltp)},
                    "oi": {"cur": oi, "prev": prev, "chg": oi - prev}}
        strikes = (25700, 25750, 25800, 25850, 25900)
        # Near-term expiry + parity-consistent LTPs (C-P≈30 at spot≈25708)
        # keep IV-skew neutral so the fixture tests plumbing, not models.
        return {"common_data": {"exSeg": "nse_fo", "expiryDt": "2026-10-19"},
                "call": [leg(s, "CE", 130.0, 1000 + s, 950 + s) for s in strikes],
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
    """Realistic tape v2 likes: chop base (value), gentle drift with a mid
    pullback, then a moderate push into a HIGH breakout. Straight-line
    moonshots are correctly vetoed by v2 (exhaust-chase/extended)."""
    import random
    rnd = random.Random(3)
    b = CandleBuilder(timeframe_secs=60)
    t0 = 1_700_000_000 - (n * 60)
    px = base
    closes = []
    for _ in range(20):
        c = px + rnd.uniform(-2, 2)
        closes.append(c)
    px = closes[-1]
    for stp in (1, 1.2, 0.8, 1.5, -2.5, -1.5, 1, 1.2, 1.5, 2):
        px += stp + rnd.uniform(-0.8, 0.8)
        closes.append(px)
    assert len(closes) == n
    for i, c in enumerate(closes):
        b._closed.setdefault("NIFTY", []).append(
            {"t": t0 + i * 60, "o": c - 1, "h": c + 2, "l": c - 2,
             "c": c, "v": 1000})
    return b


IST_10AM = datetime(2026, 10, 14, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
IST_8AM = datetime(2026, 10, 14, 2, 30, tzinfo=timezone.utc)    # 08:00 IST
IST_320PM = datetime(2026, 10, 14, 9, 50, tzinfo=timezone.utc)  # 15:20 IST
IST_335PM = datetime(2026, 10, 14, 10, 5, tzinfo=timezone.utc)  # 15:35 IST
IST_345PM = datetime(2026, 10, 14, 10, 15, tzinfo=timezone.utc)  # 15:45 IST
IST_6PM = datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc)  # 18:00 IST


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    mgr = _FakeManager(quotes=_quotes_for((25700, 25750, 25800, 25850, 25900)))
    monkeypatch.setattr(at, "_manager", mgr)
    monkeypatch.setattr(at, "_broadcast", None)
    # Keep dashboard-state persistence out of the real data dir in tests.
    monkeypatch.setattr(at, "_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(at.db, "save_message",
                        lambda *a, **k: asyncio.sleep(0, result=777))
    at._reset_scheduler_state()
    at._calls.clear()
    return mgr


def test_state_roundtrip_persists_calls_and_snapshot(wired, tmp_path,
                                                     monkeypatch):
    res = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=_bullish_builder()))
    assert res["action"] == "generated", res
    saved = json.loads(open(at._STATE_FILE).read())
    assert len(saved["calls"]) == 1 and saved["calls"][0]["side"] == "BUY CE"
    assert saved["live"]["engine"]["outcome"].startswith("generated")
    # Simulate a restart: clear memory, restore from disk.
    # Pin "today" to the fixture day: restore is same-session only and the
    # suite must not depend on the wall-clock date.
    import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    monkeypatch.setattr(at, "_ist_now",
                        lambda now=None: _dt.datetime(
                            2026, 10, 14, 10, 5, tzinfo=_ZI("Asia/Kolkata")))
    at._calls.clear()
    at._last_live.clear()
    at._tick_log.clear()
    at._restore_state()
    assert len(at._calls) == 1 and at._calls[0]["side"] == "BUY CE"
    assert at._last_live["engine"]["outcome"].startswith("generated")
    assert len(at._last_live["decisions"]) >= 1


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
    at._st("NIFTY").update(last_bias="BULLISH", last_ts=IST_10AM.timestamp(),
                           last_breakout=False)
    res = asyncio.run(at.scheduler_tick(now=IST_10AM, builder=b))
    assert res["action"] == "generated", res
    assert at._st("NIFTY")["last_breakout"] is True
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


def test_entry_cutoff_blocks_late_fires_but_keeps_data(wired):
    # Same firing tape at 10:00 generates; at 15:20 the read publishes but
    # no fresh entry goes out (auction zone = exit-only).
    assert asyncio.run(
        at.scheduler_tick(now=IST_10AM, builder=_bullish_builder()))["action"] == "generated"
    at._reset_scheduler_state()
    n_before = len(wired.process_calls)
    # Locked trend-day state: the regime gate passes on merit and the cutoff
    # is what's actually tested (a 30-bar 15:20 tape would trip thin-tape).
    at._st("NIFTY").update(day="2026-10-14", regime="TREND-DAY-CANDIDATE",
                           regime_dir="BULLISH", regime_locked=True,
                           regime_checked_ts=IST_320PM.timestamp() + 3600)
    res = asyncio.run(at.scheduler_tick(now=IST_320PM, builder=_bullish_builder()))
    assert res["action"] == "skip" and res["reason"] == "entry cutoff (exit-only zone)"
    assert len(wired.process_calls) == n_before
    eng = at._last_live.get("engine", {})
    assert eng.get("fusion", {}).get("fire") is True  # read still published


def test_extended_close_still_in_hours(wired):
    # Derivatives now trade to 15:40: 15:35 ticks evaluate, 15:45 skips.
    assert at._in_market_hours(IST_335PM) is True
    assert at._in_market_hours(IST_345PM) is False
    res = asyncio.run(at.scheduler_tick(now=IST_345PM, builder=_bullish_builder()))
    assert res["reason"] == "outside market hours"


def test_mcx_book_runs_evenings_with_own_state(wired):
    from backend.autotrender import CRUDE_CFG
    empty = CandleBuilder()
    res = asyncio.run(at.scheduler_tick(now=IST_6PM, builder=empty, cfg=CRUDE_CFG))
    assert res["reason"].startswith("warming up"), res  # MCX in-session at 18:00
    # NSE book is closed then; per-symbol state stays independent.
    assert at._st("CRUDEOIL")["day"] == "2026-10-14"
    assert at._st("NIFTY").get("day") is None


def test_session_gate_routes_ticks():
    assert at._in_session("CRUDEOIL15OCT269000PE", IST_6PM) is True
    assert at._in_session("NIFTY26O1922650CE", IST_6PM) is False
    assert at._in_session("NIFTY", IST_10AM) is True
    # Mini books trade MCX hours, not NSE hours.
    assert at._in_session("NATGASMINI", IST_6PM) is True
    assert at._in_session("GOLDM", IST_6PM) is True
    assert at._in_session("NATGASMINI", IST_10AM) is True


def test_mini_cfg_uses_standard_future_for_tape():
    assert at.NATGAS_CFG["fut"] == "NATURALGAS"
    assert at.GOLDM_CFG["fut"] == "GOLD"
    assert at.NATGAS_CFG["symbol"] == "NATGASMINI"
    assert [c["symbol"] for c in at.SYMBOL_CFGS] == [
        "NIFTY", "CRUDEOIL", "NATGASMINI", "GOLDM",
        "CRUDEOILM", "SILVERM"]
    assert at.CRUDEMINI_CFG["fut"] == "CRUDEOIL"
    assert at.SILVERM_CFG["fut"] == "SILVER"


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


def _chop_reclaim_bars():
    """15 bars pinned below vwap, then 3 holding above -> BULLISH reclaim."""
    bars = [{"t": 1_700_000_000 + i * 60, "o": 99, "h": 100, "l": 98,
             "c": 99, "v": 1000} for i in range(15)]
    bars += [{"t": 1_700_000_000 + (15 + i) * 60, "o": 101, "h": 102,
              "l": 100.5, "c": 101.5, "v": 1000} for i in range(3)]
    return bars


def _fresh_state(**kw):
    S = {"day": "2026-10-14", "count": 0, "last_ts": 0.0, "last_bias": None,
         "last_breakout": False, "chain_keys": [], "prev_rows": [],
         "prev_vix": None, "regime": "EARLY", "regime_dir": None,
         "regime_locked": False, "regime_checked_ts": 0.0,
         "scratches": 0, "silent": False, "fires": []}
    S.update(kw)
    return S


def test_gate_early_session_allows():
    g = at._regime_gate(_fresh_state(),
                        {"fire": True, "direction": "BULLISH"}, [])
    assert g["allowed"] is True and "early" in g["reason"]


def _ts(hour, minute, day=14):
    return datetime(2026, 10, day, hour, minute,
                    tzinfo=timezone.utc).timestamp()


def test_gate_thin_tape_demands_reclaim():
    # 20 bars in 109 session minutes (crude morning): unreadable -> chop rules.
    S = _fresh_state()
    flat = [{"t": 1_700_000_000 + i * 60, "o": 100, "h": 101, "l": 99,
             "c": 100, "v": 100} for i in range(20)]
    g = at._regime_gate(S, {"fire": True, "direction": "BULLISH"}, flat,
                        _ts(5, 19), (9, 0))
    assert g["allowed"] is False and "thin tape" in g["reason"], g


def test_gate_thin_tape_reclaim_passes():
    S = _fresh_state()
    g = at._regime_gate(S, {"fire": True, "direction": "BULLISH"},
                        _chop_reclaim_bars(), _ts(5, 19), (9, 0))
    assert g["allowed"] is True and "thin tape A+" in g["reason"], g


def test_gate_dense_early_keeps_old_behavior():
    # 30 bars in 45 minutes (NIFTY morning): a bar a minute, day readable soon.
    S = _fresh_state()
    bars = [{"t": 1_700_000_000 + i * 60, "o": 100, "h": 101, "l": 99,
             "c": 100, "v": 100} for i in range(30)]
    g = at._regime_gate(S, {"fire": True, "direction": "BULLISH"}, bars,
                        _ts(4, 30), (9, 15))
    assert g["allowed"] is True and "early session" in g["reason"], g


def test_regime_survives_restart(wired, monkeypatch):
    import json as _json
    import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    asyncio.run(at.scheduler_tick(now=IST_10AM, builder=_bullish_builder()))
    at._st("NIFTY").update(regime="TREND-DAY-CANDIDATE", regime_dir="BULLISH",
                           regime_locked=True, scratches=1)
    at._save_state()
    saved = _json.loads(open(at._STATE_FILE).read())
    assert saved["sched"]["NIFTY"]["regime"] == "TREND-DAY-CANDIDATE"
    assert saved["sched"]["NIFTY"]["scratches"] == 1
    at._reset_scheduler_state()
    assert at._st("NIFTY").get("regime") == "EARLY"  # wiped
    monkeypatch.setattr(at, "_ist_now",
                        lambda now=None: _dt.datetime(
                            2026, 10, 14, 10, 5, tzinfo=_ZI("Asia/Kolkata")))
    at._restore_state()
    S = at._st("NIFTY")
    assert S["regime"] == "TREND-DAY-CANDIDATE" and S["scratches"] == 1, S


def test_gate_trend_day_locks_direction():
    S = _fresh_state(regime="TREND-DAY-CANDIDATE", regime_dir="BULLISH",
                     regime_locked=True)
    bad = at._regime_gate(S, {"fire": True, "direction": "BEARISH"}, [])
    assert bad["allowed"] is False and "against day regime" in bad["reason"]
    good = at._regime_gate(S, {"fire": True, "direction": "BULLISH"}, [])
    assert good["allowed"] is True


def test_gate_chop_needs_matching_reclaim():
    S = _fresh_state(regime="CHOP", regime_locked=True)
    bars = _chop_reclaim_bars()
    ok = at._regime_gate(S, {"fire": True, "direction": "BULLISH"}, bars)
    assert ok["allowed"] is True and "reclaim" in ok["reason"].lower()
    wrong = at._regime_gate(S, {"fire": True, "direction": "BEARISH"}, bars)
    assert wrong["allowed"] is False and "no BEARISH reclaim" in wrong["reason"]


def test_gate_chop_without_reclaim_blocks():
    S = _fresh_state(regime="CHOP", regime_locked=True)
    flat = [{"t": 1_700_000_000 + i * 60, "o": 100, "h": 101, "l": 99,
             "c": 100, "v": 1000} for i in range(20)]
    g = at._regime_gate(S, {"fire": True, "direction": "BULLISH"}, flat)
    assert g["allowed"] is False and g["reason"].startswith("chop:")


def test_gate_silent_circuit_breaker():
    g = at._regime_gate(_fresh_state(silent=True, scratches=2),
                        {"fire": True, "direction": "BULLISH"}, [])
    assert g["allowed"] is False and "silent" in g["reason"]


def test_no_count_cap_high_scores_always_eligible():
    # 10 prior fires: the bar is score, not quota.
    g = at._regime_gate(_fresh_state(count=10),
                        {"fire": True, "direction": "BULLISH"}, [])
    assert g["allowed"] is True, g


def test_bar_raised_to_70_with_witness_rule():
    from backend import at_models as m
    assert m.FIRE_THRESHOLD_V2 == 70.0
    assert m.MIN_WITNESSES == 2


def test_thin_agreement_vetoed_despite_score():
    # High score built on trend+trigger alone: no witness backs it.
    from backend import at_models as m
    f = m.fuse_v2({"score": 2}, {"fire": True, "direction": "BULLISH"},
                  {"score": 0, "veto": False}, {"ok": True},
                  {"trending": False}, {"bias": "FLAT"},
                  {"stretched": False}, {"vetoed": False, "ratio": 1.0},
                  {"rsi": None}, {"score": 0}, {"skew": 0},
                  {"expanding": None, "chg_pct": 0.0},
                  {"veto": False}, {"direction": None})
    assert f["fire"] is False and "thin-agreement" in f["vetoes"], f


def test_judge_fires_scratches_and_silences():
    S = _fresh_state(fires=[
        {"bar_idx": 0, "entry": 100.0, "direction": "BULLISH"},
        {"bar_idx": 1, "entry": 100.0, "direction": "BULLISH"}])
    bars = [{"t": 1_700_000_000 + i * 60, "o": 100, "h": 100, "l": 99,
             "c": 99.5 - i * 0.05, "v": 100} for i in range(35)]
    at._judge_fires(S, bars)
    assert S["scratches"] == 2 and S["silent"] is True


def test_trend_day_counter_trigger_blocked_live(wired):
    at._st("NIFTY").update(day="2026-10-14", regime="TREND-DAY-CANDIDATE",
                           regime_dir="BEARISH", regime_locked=True,
                           regime_checked_ts=IST_10AM.timestamp() + 3600)
    res = asyncio.run(at.scheduler_tick(now=IST_10AM,
                                        builder=_bullish_builder()))
    assert res["action"] == "skip" and "against day regime" in res["reason"]
    assert at._last_live["engine"]["regime"]["label"] == "TREND-DAY-CANDIDATE"


def _reentry_builder():
    """Chop (day high 25702) -> dip 25685 -> reclaim 25700.5 and hold.
    No new high, muted RSI: no spike trigger — only a re-entry may fire."""
    from backend.candle_builder import CandleBuilder
    b = CandleBuilder(timeframe_secs=60)
    t0 = 1_700_000_000 - 40 * 60
    closes = [25700 + (1 if i % 2 else -1) for i in range(20)]
    closes += [25696, 25692, 25688, 25685, 25687]
    closes += [25692, 25696, 25699, 25700.5, 25700.5, 25700.5]
    for i, c in enumerate(closes):
        b._closed.setdefault("NIFTY", []).append(
            {"t": t0 + i * 60, "o": c - 0.5, "h": c + 1, "l": c - 1,
             "c": c, "v": 1000})
    return b


def _scratched(entry=25687.0, direction="BULLISH"):
    return {"bar_idx": 20, "entry": entry, "direction": direction,
            "judged": True, "scratched": True, "reentered": False,
            "is_reentry": False}


def test_reentry_pick_second_chance_only():
    bars = _reentry_builder()._closed["NIFTY"]
    S = _fresh_state(fires=[_scratched()])
    pick = at._reentry_pick(S, bars)
    assert pick is not None and pick["direction"] == "BULLISH", pick
    S["fires"][0]["reentered"] = True
    assert at._reentry_pick(S, bars) is None  # consumed: never again


def test_reentry_pick_ignores_reentries():
    bars = _reentry_builder()._closed["NIFTY"]
    S = _fresh_state(fires=[_scratched() | {"is_reentry": True}])
    assert at._reentry_pick(S, bars) is None


def test_reentry_synthetic_trigger_reaches_fusion(wired):
    S = at._st("NIFTY")
    S.update(day="2026-10-14", fires=[_scratched()])
    res = asyncio.run(at.scheduler_tick(now=IST_10AM,
                                        builder=_reentry_builder()))
    trig_reason = at._last_live["engine"]["trigger"]["reason"]
    assert "re-entry" in trig_reason, (res, trig_reason)
    if res["action"] == "generated":
        assert S["fires"][0]["reentered"] is True
        assert S["fires"][-1]["is_reentry"] is True


def test_reentry_bypass_cooldown_but_counts_cap(wired):
    S = at._st("NIFTY")
    S.update(day="2026-10-14", last_bias="BULLISH",
             last_ts=IST_10AM.timestamp(), fires=[_scratched()])
    res = asyncio.run(at.scheduler_tick(now=IST_10AM,
                                        builder=_reentry_builder()))
    assert res["reason"] != "cooldown", res  # re-entry jumps the queue
