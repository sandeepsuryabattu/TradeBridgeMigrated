"""Unit tests for day_regime: classifier, squeeze, reclaim, recheck."""
import math
import sys

sys.path.insert(0, ".")

from backend import day_regime as dr


def _bar(o, h, l, c, v=10):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def _trend_bars(n=40, start=22600.0, step=2.0):
    bars, px = [], start
    for _ in range(n):
        o = px
        c = px + step
        bars.append(_bar(o, max(o, c) + 1, min(o, c) - 1, c))
        px = c
    return bars


def _chop_bars(n=40, start=22600.0, amp=12.0):
    bars = []
    for i in range(n):
        c = start + amp * math.sin(i / 3.0)
        o = start + amp * math.sin((i - 1) / 3.0) if i else start
        bars.append(_bar(o, max(o, c) + 3, min(o, c) - 3, c))
    return bars


def test_classify_trend_day():
    out = dr.classify(_trend_bars())
    assert out["label"] == "TREND-DAY-CANDIDATE", out
    assert out["parts"]["direction"] == "UP"


def test_classify_chop_day():
    out = dr.classify(_chop_bars())
    assert out["label"] == "CHOP", out


def test_classify_too_early():
    out = dr.classify(_trend_bars(n=5))
    assert out["label"] == "UNDECIDED"


def test_squeeze_coiled():
    wide = [_bar(100, 110, 90, 100 + (i % 3)) for i in range(20)]
    tight = [_bar(100, 101, 99, 100) for _ in range(20)]
    out = dr.squeeze(wide + tight)
    assert out["coiled"] is True, out


def test_squeeze_not_coiled():
    bars = [_bar(100, 110, 90, 100 + (j % 5)) for j in range(40)]
    out = dr.squeeze(bars)
    assert out["coiled"] is False, out


def test_reclaim_bullish():
    below = [_bar(100, 101, 98, 99) for _ in range(15)]
    above = [_bar(100, 103, 100, 102.5) for _ in range(3)]
    out = dr.reclaim(below + above)
    assert out["setup"] is True and out["side"] == "BULLISH", out


def test_reclaim_none_in_trend():
    out = dr.reclaim(_trend_bars())
    assert out["setup"] is False  # never lost vwap: no TEST to buy


def test_recheck_flip():
    up = _trend_bars(n=30)
    # slam back under vwap
    down = [_bar(22600, 22601, 22580, 22585) for _ in range(5)]
    out = dr.recheck("TREND-DAY-CANDIDATE", "UP", up + down)
    assert out["flip"] is True and out["label"] == "CHOP", out


def test_recheck_alive():
    out = dr.recheck("TREND-DAY-CANDIDATE", "UP", _trend_bars(n=30))
    assert out["flip"] is False, out


def test_reentry_bullish_reclaim_and_hold():
    dip = [_bar(100, 100.5, 98, 99) for _ in range(10)]
    back = [_bar(101, 102, 100.5, 101.5) for _ in range(3)]
    out = dr.reentry_setup(dip + back, 100.0, "BULLISH")
    assert out["setup"] is True, out


def test_reentry_no_hold():
    dip = [_bar(100, 100.5, 98, 99) for _ in range(10)]
    wobble = [_bar(100, 101, 99.5, 100.2),
              _bar(100, 100.5, 99.8, 99.9),
              _bar(100, 101, 100, 100.6)]
    out = dr.reentry_setup(dip + wobble, 100.0, "BULLISH")
    assert out["setup"] is False, out  # middle bar lost the level


def test_reentry_never_lost():
    up = [_bar(100 + i, 101 + i, 100 + i, 100.5 + i) for i in range(15)]
    out = dr.reentry_setup(up, 100.0, "BULLISH")
    assert out["setup"] is False, out
