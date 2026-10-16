"""Tests for the session location layer (IB + developing value area)."""
from backend import day_location as dl


def _bar(px, v=100.0, t=1_700_000_000):
    return {"t": t, "o": px, "h": px + 0.5, "l": px - 0.5, "c": px, "v": v}


def _session(last_px, last_v=100.0, n=70, base=100.0):
    bars = [_bar(base + (i % 3 - 1), v=1000.0, t=1_700_000_000 + i * 60)
            for i in range(60)]
    bars += [_bar(last_px, v=last_v, t=1_700_000_000 + (60 + i) * 60)
             for i in range(n - 60)]
    return bars


def test_ib_captured_from_first_hour():
    loc = dl.locate(_session(104.0))
    assert loc["ready"] is True
    assert loc["ib_high"] == 101.5 and loc["ib_low"] == 98.5


def test_accepted_above_value_confirms_bullish():
    loc = dl.locate(_session(104.0))
    assert loc["location"] == "ABOVE_VALUE", loc
    v = dl.location_vote(loc, "BULLISH")
    assert v["score"] == 1 and "above value" in v["reason"]
    assert dl.location_vote(loc, "BEARISH")["score"] == 0


def test_accepted_below_value_confirms_bearish():
    loc = dl.locate(_session(96.0))
    assert loc["location"] == "BELOW_VALUE", loc
    assert dl.location_vote(loc, "BEARISH")["score"] == 1
    assert dl.location_vote(loc, "BULLISH")["score"] == 0


def test_inside_value_holding_poc_side_confirms():
    loc = dl.locate(_session(100.4))
    assert loc["location"] == "IN_VALUE", loc
    assert dl.location_vote(loc, "BULLISH")["score"] == 1
    assert dl.location_vote(loc, "BEARISH")["score"] == 0


def test_thin_tape_abstains_never_blocks():
    loc = dl.locate([_bar(100.0)] * 20)
    assert loc["ready"] is False
    for d in ("BULLISH", "BEARISH", None):
        assert dl.location_vote(loc, d)["score"] == 0
    assert dl.location_vote({}, "BULLISH")["score"] == 0


def test_value_area_holds_70pct_of_volume():
    bars = [_bar(100.0, v=700.0)] * 60 + [_bar(110.0, v=100.0)] * 10
    loc = dl.locate(bars)
    assert loc["ready"] is True
    assert loc["val"] <= 100.0 <= loc["vah"], loc
    assert loc["location"] == "ABOVE_VALUE", loc
